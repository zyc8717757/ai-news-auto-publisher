"""Public discovery routes resolve back to original publishers before becoming evidence.

No paid search, authenticated X API, browser sessions or aggregator timestamps as facts.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .collector import article_metadata, iso_date, item, parse_feed, plain
from .models import Source
from .selection import features

# Explicit ownership map. Unknown hosts remain leads, not automatic official evidence.
PUBLISHERS = {
    "openai.com": ("OpenAI", "openai"),
    "learn.chatgpt.com": ("OpenAI", "openai"),
    "help.openai.com": ("OpenAI", "openai"),
    "developers.openai.com": ("OpenAI", "openai"),
    "alignment.openai.com": ("OpenAI", "openai"),
    "anthropic.com": ("Anthropic", "anthropic"),
    "claude.com": ("Anthropic", "anthropic"),
    "support.claude.com": ("Anthropic", "anthropic"),
    "platform.claude.com": ("Anthropic", "anthropic"),
    "claude.dev": ("Anthropic", "anthropic"),
    "blog.google": ("Google", "google"),
    "deepmind.google": ("Google DeepMind", "google"),
    "developers.googleblog.com": ("Google Developers", "google"),
    "cloud.google.com": ("Google Cloud", "google"),
    "api-docs.deepseek.com": ("DeepSeek", "deepseek"),
    "deepseek.com": ("DeepSeek", "deepseek"),
    "qwen.ai": ("Qwen", "qwen"),
    "qwenlm.github.io": ("Qwen", "qwen"),
    "minimax.io": ("MiniMax", "minimax"),
    "moonshot.ai": ("Moonshot", "moonshot"),
    "kimi.com": ("Kimi", "moonshot"),
    "z.ai": ("Z.ai", "zhipu"),
    "mistral.ai": ("Mistral", "mistral"),
    "x.ai": ("xAI", "xai"),
    "blog.cloudflare.com": ("Cloudflare", "cloudflare"),
    "deno.com": ("Deno", "deno"),
    "typesafe.ai": ("TypeSafe AI", "typesafe"),
    "a16z.com": ("a16z", "a16z"),
    "commandline.microsoft.com": ("Microsoft", "microsoft"),
    "microsoft.com": ("Microsoft", "microsoft"),
    "blogs.microsoft.com": ("Microsoft", "microsoft"),
    "elevenlabs.io": ("ElevenLabs", "elevenlabs"),
    "vercel.com": ("Vercel", "vercel"),
    "lmstudio.ai": ("LM Studio", "lmstudio"),
    "docs.qoder.com": ("Qoder", "qwen"),
    "docs.trae.cn": ("TRAE", "bytedance"),
    "epoch.ai": ("Epoch AI", "epoch"),
    "artificialanalysis.ai": ("Artificial Analysis", "artificialanalysis"),
    "baseten.co": ("Baseten", "baseten"),
    "goodfire.com": ("Goodfire", "goodfire"),
}
MEDIA = {
    "techcrunch.com": "techcrunch",
    "the-decoder.com": "decoder",
    "businessinsider.com": "businessinsider",
    "reuters.com": "reuters",
    "axios.com": "axios",
    "arstechnica.com": "conde-nast",
    "ithome.com": "ithome",
}
HF_ORGS = {
    "Qwen": "qwen",
    "deepseek-ai": "deepseek",
    "tencent": "tencent",
    "microsoft": "microsoft",
    "google": "google",
    "moonshotai": "moonshot",
    "MiniMaxAI": "minimax",
    "zai-org": "zhipu",
    "stepfun-ai": "stepfun",
    "XiaomiMiMo": "xiaomi",
    "mistralai": "mistral",
}


def publisher(url: str):
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    if host in PUBLISHERS:
        name, group = PUBLISHERS[host]
        return Source(id="primary-" + group, name=name, url=url, trust="official", group=group, ai_only=False)
    if host in MEDIA:
        return Source(
            id="media-" + MEDIA[host], name=host, url=url, trust="media", group=MEDIA[host], ai_only=False
        )
    return None


async def resolve(collector, url, now):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").removeprefix("www.")
    try:
        if host == "huggingface.co":
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] not in HF_ORGS or not re.fullmatch(r"[\w.-]+", parts[1]):
                return None
            model_id = "/".join(parts)
            page = await collector.fetch("https://huggingface.co/api/models/" + model_id)
            data = json.loads(page.text)
            date = iso_date(data.get("createdAt"))
            if not date or date < now - timedelta(hours=72):
                return None  # lastModified is not a release date.
            readme = await collector.fetch("https://huggingface.co/" + model_id + "/raw/main/README.md")
            group = HF_ORGS[parts[0]]
            source = Source(
                id="hf-" + group, name=parts[0] + " 官方模型仓库", url=url, trust="official", group=group
            )
            return item(source, url, model_id + " 模型仓库上线", plain(readme.text), date, now)
        source = publisher(url)
        if source is None:
            return None
        page = await collector.fetch(url)
        # Redirects still obey PublicHTTP network safety, and may not change publisher identity.
        final = publisher(page.url)
        if final is None or final.group != source.group:
            return None
        title, excerpt, date, precision, label = article_metadata(page.text)
        return item(
            source,
            page.url,
            title,
            excerpt,
            date,
            now,
            precision if date else "unknown",
            label,
            allow_undated=source.trust == "official",
        )
    except Exception:
        return None


async def discover(collector, source, body, now):
    issues = [x for x in parse_feed(source, body, now, 3) if x.published_at >= now - timedelta(hours=72)]
    links = []
    pairs = []
    for issue in issues[:2]:
        page = await collector.fetch(issue.url)
        if urlsplit(page.url).hostname != urlsplit(source.url).hostname:
            continue
        soup = BeautifulSoup(page.text, "html.parser")
        # Each candidate is resolved from its own original URL; no author summary is evidence.
        for section in soup.select("section.news-item"):
            candidates = [a["href"] for a in section.select(".event-related-links a[href]")]
            heading = section.find("h3")
            if heading:
                candidates = [
                    a["href"] for a in heading.find_all("a", href=True) if a["href"].startswith("https://")
                ] + candidates
            suitable = [u for u in candidates if publisher(u) or urlsplit(u).hostname == "huggingface.co"]
            pair = list(dict.fromkeys(suitable))[:2]
            pairs.append(pair)
            links.extend(pair)
    links = list(dict.fromkeys(links))[:48]
    values = await asyncio.gather(*(resolve(collector, url, now) for url in links))
    resolved = dict(zip(links, values, strict=True))
    dated = {x.url: x for x in values if x and x.published_at is not None}
    for pair in pairs:
        members = [resolved[u] for u in pair if resolved.get(u)]
        primary = next((x for x in members if x.published_at is not None), None)
        if primary is None:
            continue
        entities = set(features(primary.title)[0])
        supports = [
            x
            for x in members
            if x.published_at is None
            and x.trust == "official"
            and entities
            and entities.intersection(features(x.title)[0])
        ]
        if supports:
            # Undated original statements can corroborate a dated announcement,
            # but never establish freshness by themselves or acquire an invented time.
            primary.supporting = supports[:2]
    return list(dated.values())


async def discourse(collector, source, body, now):
    entries = parse_feed(source, body, now, min(12, collector.settings.source_limit))
    result = []
    for entry in entries:
        match = re.search(r"/t/(?:[^/]+/)?(\d+)", entry.url)
        if not match or urlsplit(entry.url).hostname != urlsplit(source.url).hostname:
            continue
        try:
            page = await collector.fetch("https://community.openai.com/t/" + match[1] + ".json")
            data = json.loads(page.text)
            first = data["post_stream"]["posts"][0]
            # Public announcement category plus a known company author, not all community posts.
            if first.get("username") not in {"VeitB", "OpenAI", "OpenAI_Support", "OpenAIDevs"}:
                continue
            value = item(
                source,
                entry.url,
                data["title"],
                plain(first.get("cooked", "")),
                iso_date(first.get("created_at")),
                now,
            )
            if value:
                result.append(value)
        except Exception:
            continue
    return result
