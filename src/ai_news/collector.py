from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import Settings, Source, SourceCache, SourceItem, web_url
from .network import Page, PublicHTTP, UnsafeURL


def canonical_url(url: str) -> str:
    web_url(url)
    parsed = urlsplit(url)
    tracking = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid"}
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in tracking
    ]
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(sorted(query)), "")
    )


def plain(value: str, limit: int = 1600) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return "".join(c for c in text if c.isprintable())[:limit]


def iso_date(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return result.astimezone(UTC) if result and result.tzinfo else None
    except (ValueError, TypeError):
        return None


def item(
    source: Source, url: str, title: str, excerpt: str, date: datetime | None, now: datetime
) -> SourceItem | None:
    if not date or not title.strip() or date > now:
        return None
    try:
        url = canonical_url(url)
        return SourceItem(
            id=hashlib.sha256(url.encode()).hexdigest()[:20],
            source_id=source.id,
            source_name=source.name,
            group=source.group,
            trust=source.trust,
            url=url,
            title=plain(title, 400),
            excerpt=plain(excerpt),
            published_at=date,
            collected_at=now,
        )
    except ValueError:
        return None


def parse_feed(source: Source, body: str, now: datetime, limit: int) -> list[SourceItem]:
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError("Invalid feed")
    if not parsed.version and not parsed.entries:
        raise ValueError("Response is not RSS or Atom")
    result = []
    for entry in parsed.entries[:limit]:
        # An updated timestamp alone cannot establish when an old article was first published.
        timestamp = entry.get("published_parsed")
        date = datetime.fromtimestamp(calendar.timegm(timestamp), UTC) if timestamp else None
        value = item(
            source, entry.get("link", ""), entry.get("title", ""), entry.get("summary", ""), date, now
        )
        if value:
            result.append(value)
    return result


def parse_releases(source: Source, body: str, now: datetime, limit: int) -> list[SourceItem]:
    releases = json.loads(body)
    if not isinstance(releases, list):
        raise ValueError("Expected GitHub releases array")
    result = []
    for release in releases[:limit]:
        if release.get("draft") or release.get("prerelease"):
            continue
        value = item(
            source,
            release.get("html_url", ""),
            f"{source.name}: {release.get('name') or release.get('tag_name', '')}",
            release.get("body") or "",
            iso_date(release.get("published_at")),
            now,
        )
        if value:
            result.append(value)
    return result


def article_fields(body: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(body, "html.parser")
    title_node = soup.find("h1") or soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    date = None
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"name": "publication_date"}):
        node = soup.find("meta", attrs=attrs)
        if node:
            date = iso_date(node.get("content"))
            if date:
                break
    if not date:
        node = soup.find("time", datetime=True)
        date = iso_date(node.get("datetime")) if node else None
    if not date:
        for node in soup.find_all("script", type="application/ld+json"):
            try:
                objects = json.loads(node.string or "{}")
                objects = objects if isinstance(objects, list) else [objects]
                for obj in objects:
                    for candidate in obj.get("@graph", [obj]):
                        date = iso_date(candidate.get("datePublished"))
                        if date:
                            break
            except (ValueError, AttributeError, TypeError):
                continue
            if date:
                break
    article = soup.find("article") or soup.find("main")
    return title, plain(str(article or "")), date


class Collector:
    def __init__(self, settings: Settings, http: PublicHTTP | None = None):
        self.settings = settings
        self.http = http or PublicHTTP(settings.timeout_seconds)
        self.semaphore = asyncio.Semaphore(settings.concurrency)

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> Page:
        async with self.semaphore:
            for attempt in range(self.settings.source_retries + 1):
                try:
                    return await self.http.get(url, headers)
                except UnsafeURL:
                    raise
                except Exception:
                    if attempt == self.settings.source_retries:
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError("Unreachable")

    async def collect_source(
        self, source: Source, cache: SourceCache | None, now: datetime
    ) -> tuple[list[SourceItem], SourceCache]:
        headers = {}
        if cache:
            if cache.etag:
                headers["If-None-Match"] = cache.etag
            if cache.modified:
                headers["If-Modified-Since"] = cache.modified
        try:
            page = await self.fetch(source.url, headers)
            if page.status == 304:
                return [], (cache or SourceCache()).model_copy(update={"status": "unchanged", "error_code": ""})
            if source.kind == "github":
                values = parse_releases(source, page.text, now, self.settings.source_limit)
            elif source.kind == "html":
                soup = BeautifulSoup(page.text, "html.parser")
                links = dict.fromkeys(
                    urljoin(page.url, a["href"])
                    for a in soup.find_all("a", href=True)
                    if source.link_pattern and re.search(source.link_pattern, a["href"])
                )
                values = []
                # Keep adapters intentionally small; individual article failure is isolated.
                for link in list(links)[: min(self.settings.source_limit, 10)]:
                    if urlsplit(link).hostname != urlsplit(source.url).hostname:
                        continue
                    try:
                        detail = await self.fetch(link)
                        title, excerpt, date = article_fields(detail.text)
                        value = item(source, link, title, excerpt, date, now)
                        if value:
                            values.append(value)
                    except Exception:
                        continue
                if not values:
                    raise ValueError("No dated HTML articles found")
            else:
                values = parse_feed(source, page.text, now, self.settings.source_limit)
            return values, SourceCache(
                etag=page.headers.get("etag", "")[:500], modified=page.headers.get("last-modified", "")[:100]
            )
        except Exception as exc:
            # Exception text can contain remote responses or credentials; publish only a fixed code.
            code = "source_unavailable"
            if isinstance(exc, UnsafeURL):
                code = "unsafe_target"
            elif isinstance(exc, httpx.HTTPStatusError):
                code = f"http_{exc.response.status_code}"
            elif isinstance(exc, httpx.TimeoutException):
                code = "timeout"
            elif isinstance(exc, (httpx.ConnectError, OSError)):
                code = "connection_failed"
            elif isinstance(exc, ValueError):
                code = "parse_failed"
            return [], (cache or SourceCache()).model_copy(update={"status": "error", "error_code": code})

    async def collect(self, sources: list[Source], caches: dict[str, SourceCache], now: datetime):
        enabled = [source for source in sources if source.enabled]
        results = await asyncio.gather(*(self.collect_source(s, caches.get(s.id), now) for s in enabled))
        values = []
        next_cache = {}
        for source, (items, cache) in zip(enabled, results, strict=True):
            values.extend(items)
            next_cache[source.id] = cache
        values.sort(key=lambda value: value.published_at, reverse=True)
        return values[: self.settings.max_candidates], next_cache
