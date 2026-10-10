import asyncio
from datetime import timedelta

import pytest

from ai_news.collector import Collector, article_fields, canonical_url, parse_feed, parse_releases
from ai_news.models import SourceCache
from ai_news.network import Page


def test_url_normalization_preserves_meaning():
    assert (
        canonical_url("https://example.com/news?v=2&utm_source=x&lang=zh#part")
        == "https://example.com/news?lang=zh&v=2"
    )
    with pytest.raises(ValueError):
        canonical_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        canonical_url("https://user:password@example.com/")


def test_feed_dates_and_html(official, now):
    body = """<rss version="2.0"><channel><title>News</title>
    <item><title>AI update</title><link>https://example.com/a</link>
    <pubDate>Fri, 09 Oct 2026 20:00:00 +0000</pubDate><description>&lt;b&gt;Safe&lt;/b&gt;</description></item>
    <item><title>No date</title><link>https://example.com/b</link></item>
    <item><title>Future</title><link>https://example.com/c</link><pubDate>Fri, 09 Oct 2027 20:00:00 GMT</pubDate></item>
    </channel></rss>"""
    values = parse_feed(official, body, now, 50)
    assert len(values) == 1 and values[0].excerpt == "Safe"
    assert values[0].published_at < now
    with pytest.raises(ValueError):
        parse_feed(official, "<html>Blocked</html>", now, 50)


def test_html_date_requires_timezone():
    title, excerpt, date = article_fields("""<article><h1>AI update</h1><p>Useful text</p>
    <time datetime="2026-10-09T20:00:00+08:00"></time><script>secret</script></article>""")
    assert title == "AI update" and "secret" not in excerpt and date.tzinfo
    assert article_fields("<h1>No date</h1>")[2] is None


def test_releases_skip_prerelease(official, now):
    import json

    release = {
        "name": "v2.0",
        "html_url": "https://github.com/lab/model/releases/tag/v2.0",
        "published_at": (now - timedelta(hours=1)).isoformat(),
        "body": "AI model update",
    }
    values = parse_releases(official, json.dumps([release, {**release, "prerelease": True}]), now, 50)
    assert len(values) == 1


def test_source_failure_and_304_are_isolated(settings, official, now):
    class FakeHTTP:
        async def get(self, url, headers=None):
            assert headers["If-None-Match"] == "old"
            return Page(304, "", {}, url)

    values, cache = asyncio.run(
        Collector(settings, FakeHTTP()).collect_source(official, SourceCache(etag="old"), now)
    )
    assert values == [] and cache.status == "unchanged" and cache.etag == "old"

    class FailedHTTP:
        async def get(self, *args):
            raise RuntimeError("PRIVATE_PROVIDER_RESPONSE")

    values, cache = asyncio.run(Collector(settings, FailedHTTP()).collect_source(official, None, now))
    assert values == [] and cache.status == "error"
    assert "PRIVATE" not in cache.model_dump_json()
