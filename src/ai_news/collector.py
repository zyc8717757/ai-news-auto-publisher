from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
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
    source: Source,
    url: str,
    title: str,
    excerpt: str,
    date: datetime | None,
    now: datetime,
    time_precision: str = "exact",
    source_date: str = "",
    allow_undated: bool = False,
) -> SourceItem | None:
    if (not date and not allow_undated) or not title.strip() or (date is not None and date > now):
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
            time_precision=time_precision,
            source_date=source_date,
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


def article_metadata(body: str) -> tuple[str, str, datetime | None, str, str]:
    soup = BeautifulSoup(body, "html.parser")
    title_node = soup.find("h1") or soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    date = None
    raw_dates = []
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"name": "publication_date"}):
        node = soup.find("meta", attrs=attrs)
        if node:
            raw_dates.append(node.get("content"))
            date = iso_date(node.get("content"))
            if date:
                break
    if not date:
        node = soup.find("time", datetime=True)
        if node:
            raw_dates.append(node.get("datetime"))
        date = iso_date(node.get("datetime")) if node else None
    if not date:
        for node in soup.find_all("script", type="application/ld+json"):
            try:
                objects = json.loads(node.string or "{}")
                objects = objects if isinstance(objects, list) else [objects]
                for obj in objects:
                    for candidate in obj.get("@graph", [obj]):
                        raw_dates.append(candidate.get("datePublished"))
                        date = iso_date(candidate.get("datePublished"))
                        if date:
                            break
            except (ValueError, AttributeError, TypeError):
                continue
            if date:
                break
    precision, label = "exact", ""
    if not date:
        for raw in raw_dates:
            if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                try:
                    date = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
                    precision, label = "day", raw
                    break
                except ValueError:
                    continue
    if not date:
        # A visible calendar date is usable at day precision, never an invented time.
        date_nodes = soup.find_all("time") + soup.find_all(
            class_=re.compile(r"publish|posted|article.?date", re.I)
        )
        text = " ".join(
            n.get_text(" ", strip=True)
            for n in date_nodes
            if not re.search(r"updated|modified|更新", n.get_text(" ", strip=True), re.I)
        )
        found = re.search(
            r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+20\d{2}\b",
            text,
        )
        if found:
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    date = datetime.strptime(found[0], fmt).replace(tzinfo=UTC)
                    precision, label = "day", date.date().isoformat()
                    break
                except ValueError:
                    continue
    article = soup.find("article") or soup.find("main") or soup.body
    description = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    text = plain(str(article or ""), 100000)
    # Financial details often live in a footnote; retain whole public paragraphs,
    # rather than a mid-number tail or an unrelated navigation/footer fragment.
    financial = []
    actions = []
    for node in article.find_all(["p", "h5", "h6", "li"]) if article else []:
        paragraph = plain(str(node), 600)
        if (
            re.search(r"[$€£]\s*\d|\d\s*(?:million|billion|亿美元|亿元)", paragraph, re.I)
            and paragraph not in financial
        ):
            financial.append(paragraph)
        if re.search(r"expand.*all internal|paused? .*internet|suspend.*access", paragraph, re.I):
            actions.append(paragraph)
    excerpt = (
        plain(description.get("content", "") if description else "", 250)
        + " "
        + " ".join(financial[:3])[:1000]
        + " "
        + " ".join(actions[:1])[:500]
        + " "
        + text[:740]
    )
    return title, plain(excerpt), date, precision, label


def article_fields(body: str) -> tuple[str, str, datetime | None]:
    return article_metadata(body)[:3]


class Collector:
    def __init__(self, settings: Settings, http: PublicHTTP | None = None, cutoff: datetime | None = None):
        self.settings = settings
        self.http = http or PublicHTTP(settings.timeout_seconds)
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.cutoff = cutoff

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> Page:
        async with self.semaphore:
            for attempt in range(self.settings.source_retries + 1):
                try:
                    return await self.http.get(url, headers)
                except UnsafeURL:
                    raise
                except httpx.HTTPStatusError as exc:
                    if (
                        exc.response.status_code in {400, 401, 403, 404, 410}
                        or attempt == self.settings.source_retries
                    ):
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)
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
                if cache and cache.items:
                    retained = [
                        x
                        for x in cache.items
                        if x.published_at is not None and x.published_at >= now - timedelta(hours=72)
                    ]
                    return retained, cache.model_copy(
                        update={"status": "unchanged", "error_code": "", "items": retained}
                    )
                page = await self.fetch(source.url)
                if page.status == 304:
                    raise ValueError("304 without cached candidates")
            if source.kind == "github":
                values = parse_releases(source, page.text, now, self.settings.source_limit)
            elif source.kind in {"discovery", "discourse"}:
                from .discovery import discourse, discover

                values = await (
                    discover(self, source, page.text, now)
                    if source.kind == "discovery"
                    else discourse(self, source, page.text, now)
                )
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
                        title, excerpt, date, precision, label = article_metadata(detail.text)
                        value = item(source, link, title, excerpt, date, now, precision, label)
                        if value:
                            values.append(value)
                    except Exception:
                        continue
                if not values:
                    raise ValueError("No dated HTML articles found")
            else:
                values = parse_feed(source, page.text, now, self.settings.source_limit)
            return values, SourceCache(
                etag=page.headers.get("etag", "")[:500],
                modified=page.headers.get("last-modified", "")[:100],
                items=[
                    x.model_copy(
                        update={
                            "excerpt": x.excerpt[:400],
                            "supporting": [
                                v.model_copy(update={"excerpt": v.excerpt[:400], "supporting": []})
                                for v in x.supporting
                            ],
                        }
                    )
                    for x in sorted(values, key=lambda x: x.published_at, reverse=True)
                    if x.published_at is not None and x.published_at >= now - timedelta(hours=72)
                ][:12],
                discovered=len(values),
                verified=len(values),
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
            retained = (
                [
                    x
                    for x in (cache.items if cache else [])
                    if x.published_at is not None and x.published_at >= now - timedelta(hours=72)
                ]
                if code
                in {
                    "timeout",
                    "connection_failed",
                    "http_429",
                    "http_500",
                    "http_502",
                    "http_503",
                    "http_504",
                }
                else []
            )
            return retained, (cache or SourceCache()).model_copy(
                update={"status": "error", "error_code": code}
            )

    async def collect(self, sources: list[Source], caches: dict[str, SourceCache], now: datetime):
        enabled = [source for source in sources if source.enabled]
        results = await asyncio.gather(*(self.collect_source(s, caches.get(s.id), now) for s in enabled))
        values = []
        next_cache = {}
        for source, (items, cache) in zip(enabled, results, strict=True):
            values.extend(items)
            next_cache[source.id] = cache
        # Apply the report window before the global cap, then round-robin source groups.
        # A large research batch or future-dated feed must not crowd out vendor news.
        cutoff = self.cutoff or now
        buckets = {}
        for value in values:
            if (
                value.published_at is not None
                and cutoff - timedelta(hours=48) <= value.published_at <= cutoff
            ):
                buckets.setdefault(value.group, []).append(value)
        for bucket in buckets.values():
            bucket.sort(key=lambda x: x.published_at, reverse=True)
        balanced = []
        while buckets and len(balanced) < self.settings.max_candidates:
            for group in list(buckets):
                balanced.append(buckets[group].pop(0))
                if not buckets[group]:
                    del buckets[group]
                if len(balanced) >= self.settings.max_candidates:
                    break
        return balanced, next_cache
