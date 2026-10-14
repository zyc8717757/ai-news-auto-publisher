import asyncio
import json

import pytest

from ai_news.ai import AIUnavailable
from ai_news.models import NewsEvent
from ai_news.network import Page
from ai_news.pipeline import refresh_frozen_evidence


def test_empty_frozen_excerpt_is_replaced_from_the_same_cited_original(settings, source_item, monkeypatch):
    event = NewsEvent(
        id="frozen", evidence=[source_item.model_copy(update={"excerpt": "来源未提供可用摘要，请阅读原文。"})]
    )
    ref = event.evidence[0].model_dump()
    urls = []

    async def get(self, url):
        urls.append(url)
        return Page(
            200,
            "<html><main><h1>Original article</h1><p>Memory is 128GB. The announced product uses local AI.</p></main></html>",
            {},
            url,
        )

    monkeypatch.setattr("ai_news.pipeline.PublicHTTP.get", get)
    asyncio.run(refresh_frozen_evidence([event], settings))
    assert urls == [ref["url"]]
    assert "128GB" in event.evidence[0].excerpt
    assert {k: v for k, v in event.evidence[0].model_dump().items() if k != "excerpt"} == {
        k: v for k, v in ref.items() if k != "excerpt"
    }


def test_frozen_github_release_uses_its_exact_tag_not_latest(settings, source_item, monkeypatch):
    event = NewsEvent(
        id="frozen",
        evidence=[
            source_item.model_copy(
                update={"url": "https://github.com/lab/project/releases/tag/checkpoint%3D%3D4.3.0"}
            )
        ],
    )
    urls = []

    async def get(self, url):
        urls.append(url)
        return Page(200, json.dumps({"body": "Fixed version 4.3.0."}), {}, url)

    monkeypatch.setattr("ai_news.pipeline.PublicHTTP.get", get)
    asyncio.run(refresh_frozen_evidence([event], settings))
    assert urls == ["https://api.github.com/repos/lab/project/releases/tags/checkpoint%3D%3D4.3.0"]
    assert event.evidence[0].excerpt == "Fixed version 4.3.0."


def test_unavailable_original_stops_before_any_ai_request(settings, source_item, monkeypatch):
    event = NewsEvent(id="frozen", evidence=[source_item])

    async def get(self, url):
        raise RuntimeError("network offline")

    monkeypatch.setattr("ai_news.pipeline.PublicHTTP.get", get)
    with pytest.raises(AIUnavailable, match="evidence_unavailable"):
        asyncio.run(refresh_frozen_evidence([event], settings))
