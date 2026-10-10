import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from google import genai
from google.genai import types

from ai_news.ai import AIUnavailable, Budget, json_output
from ai_news.collector import Collector, article_metadata
from ai_news.discovery import discover
from ai_news.editorial import EditorialEnhancer
from ai_news.models import AIResponse, AIStory, NewsEvent, Source, SourceCache
from ai_news.network import Page
from ai_news.pipeline import ai_stories
from ai_news.selection import select_events
from ai_news.store import Store


def test_arxiv_cannot_win_by_freshness_or_model_keyword(source_item, official, now):
    source = official.model_copy(update={"id": "arxiv-ai", "trust": "research"})
    paper = source_item.model_copy(
        update={"source_id": "arxiv-ai", "trust": "research", "title": "A new AI model benchmark paper"}
    )
    assert not select_events([paper], [source], now - timedelta(hours=48), now, [])


def test_json_enclosing_fence_is_removed_without_rewriting_content():
    assert json_output('```json\n{"ok":true}\n```') == '{"ok":true}'
    assert json_output('preface {"ok":true}') == 'preface {"ok":true}'


def test_single_media_is_observation_and_community_is_rejected(source_item, official, now):
    source = official.model_copy(update={"trust": "media"})
    value = source_item.model_copy(update={"trust": "media"})
    events = select_events([value], [source], now - timedelta(hours=48), now, [])
    assert events[0].observation
    assert not select_events(
        [value.model_copy(update={"trust": "community"})], [source], now - timedelta(hours=48), now, []
    )


def test_candidate_cap_applies_after_window_and_fairness(settings, official, source_item, now):
    settings.max_candidates = 10
    noisy = official.model_copy(update={"id": "noisy", "group": "noisy"})

    class Many(Collector):
        async def collect_source(self, source, cache, moment):
            if source.id == "noisy":
                values = [
                    source_item.model_copy(
                        update={"id": str(i), "group": "noisy", "published_at": now + timedelta(hours=1)}
                    )
                    for i in range(50)
                ]
                values += [
                    source_item.model_copy(update={"id": "past" + str(i), "group": "noisy"})
                    for i in range(50)
                ]
            else:
                values = [source_item]
            return values, SourceCache()

    values, _ = asyncio.run(Many(settings).collect([noisy, official], {}, now))
    assert source_item in values and len(values) == 10
    assert all(x.published_at <= now for x in values)


def test_date_only_is_explicit_not_a_fake_exact_time():
    _, _, date, precision, label = article_metadata(
        '<main><h1>AI news</h1><time datetime="2026-10-09"></time><p>Company announces AI.</p></main>'
    )
    assert precision == "day" and label == "2026-10-09" and date.tzinfo
    assert article_metadata("<h1>Unknown time</h1>")[2] is None


def test_old_cache_304_is_refetched(settings, official, now):
    calls = []

    class HTTP:
        async def get(self, url, headers=None):
            calls.append(headers)
            return (
                Page(304, "", {}, url)
                if headers
                else Page(200, '<rss version="2.0"><channel><title>AI</title></channel></rss>', {}, url)
            )

    _, cache = asyncio.run(Collector(settings, HTTP()).collect_source(official, SourceCache(etag="old"), now))
    assert calls == [{"If-None-Match": "old"}, None] and cache.status == "ok"


def test_editorial_rejects_low_value_and_final_review_vetoes(
    settings, tmp_path, source_item, now, monkeypatch
):
    settings.ai_enabled = True
    settings.model = "gemini-3.5-flash-lite"
    settings.ai_run_limit = 20
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [
        NewsEvent(id=str(i), rule_score=80, evidence=[source_item.model_copy(update={"id": "e" + str(i)})])
        for i in range(6)
    ]
    phases = []

    def handler(request):
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        if "阶段1：" in prompt:
            phases.append("screen")
            result = {
                "decisions": [
                    {
                        "event_id": e.id,
                        "include": i > 0,
                        "category": "产品",
                        "importance": 40 if i == 1 else 90,
                        "major_research": False,
                        "observation": i == 5,
                        "reason": "重要的产品功能更新",
                    }
                    for i, e in enumerate(events)
                ]
            }
        elif "阶段2：" in prompt:
            phases.append("rank")
            result = {"ordered_ids": ["2", "3", "4", "5"], "merges": []}
        elif "阶段4：" in prompt:
            phases.append("review")
            result = {
                "reviews": [
                    {"event_id": str(i), "approved": i != 3, "reason": "证据足够"} for i in range(2, 6)
                ]
            }
        else:
            phases.append("summary")
            data = json.loads(prompt.split("\n输入数据：\n")[1])
            result = {
                "stories": [
                    {
                        "event_id": x["event_id"],
                        "title": "模型版本更新",
                        "summary": "该公司宣布了新的模型版本，实际效果仍需独立验证。",
                        "impact": "可能为开发者提供更多选择。",
                        "category": "产品",
                        "value_score": 90,
                        "evidence_ids": [x["evidence"][0]["id"]],
                        "uncertainty": "",
                        "duplicate_of": None,
                    }
                    for x in data
                ]
            }
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(result, ensure_ascii=False)}]}}]},
        )

    client = genai.Client(
        api_key="DUMMY",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    store = Store(tmp_path)
    editor = EditorialEnhancer(
        settings, store, client=client, budget=Budget(settings, store, now=lambda: now, issue="2026-10-10-pm")
    )
    response, decisions, selected = editor.edit(events, now)
    assert phases == ["screen", "rank", "summary", "review"]
    assert [s.event_id for s in response.stories] == ["2", "4", "5"]
    assert len(Store(tmp_path).state.ai_attempts) == 4
    stories = ai_stories(response, selected, decisions)
    assert [s.section for s in stories] == ["news", "news", "observation"]


def test_twenty_request_cap_is_persistent(settings, tmp_path, now, monkeypatch):
    settings.ai_run_limit = 20
    settings.model = "gemini-3.5-flash-lite"
    monkeypatch.setenv("GEMINI_RPD", "500")
    issue = "2026-10-10-pm"
    for i in range(20):
        Budget(settings, Store(tmp_path), now=lambda i=i: now + timedelta(minutes=i), issue=issue).reserve(
            100
        )
    with pytest.raises(AIUnavailable, match="issue_budget"):
        Budget(settings, Store(tmp_path), now=lambda: now + timedelta(minutes=21), issue=issue).reserve(100)
    assert Store(tmp_path).state.ai_issue_attempts[issue] == 20


def test_money_translation_preserves_units_and_cited_evidence(source_item):
    from ai_news.ai import validate_response

    event = NewsEvent(
        id="fund",
        evidence=[
            source_item.model_copy(update={"excerpt": "Raised $870 million at a $7.5 billion valuation."})
        ],
    )
    story = AIStory(
        event_id="fund",
        title="公司宣布融资",
        summary="该公司宣布融资8.7亿美元，估值达到75亿美元。",
        impact="可能推动产品发展。",
        category="商业",
        value_score=90,
        evidence_ids=[source_item.id],
    )
    validate_response(AIResponse(stories=[story]), [event])
    story.summary = "该公司宣布融资9.7亿美元，估值达到75亿美元。"
    with pytest.raises(ValueError, match="numeric"):
        validate_response(AIResponse(stories=[story]), [event])


@pytest.mark.parametrize("dated", [True, False])
def test_discovery_never_promotes_aggregator_time_to_event_time(settings, now, dated):
    source = Source(
        id="leads",
        name="Leads",
        url="https://daily.juya.uk/rss.xml",
        kind="discovery",
        trust="community",
        group="leads",
    )
    feed = '<rss version="2.0"><channel><item><title>Today</title><link>https://daily.juya.uk/issues/2026-10-10/</link><pubDate>Fri, 09 Oct 2026 23:00:00 GMT</pubDate></item></channel></rss>'
    issue = '<section class="news-item"><h3>TypeSafe AI raises funds</h3><ul class="event-related-links"><li><a href="https://typesafe.ai/blog/series-ai">Company</a></li><li><a href="https://a16z.com/announcement/investing-in-typesafe-ai/">Investor</a></li></ul></section>'

    class Fake(Collector):
        async def fetch(self, url, headers=None):
            if "daily.juya.uk/issues" in url:
                body = issue
            elif "typesafe.ai" in url:
                body = "<html><body><h1>TypeSafe A raises Series AI</h1><p>We raised funding.</p><h5>$870 million at a $7.5B valuation.</h5></body></html>"
            else:
                meta = (
                    '<meta property="article:published_time" content="2026-10-09T20:00:00Z">' if dated else ""
                )
                body = (
                    meta
                    + "<main><h1>Investing in TypeSafe AI</h1><p>We announce an investment in TypeSafe AI.</p></main>"
                )
            return Page(200, body, {}, url)

    values = asyncio.run(discover(Fake(settings), source, feed, now))
    if not dated:
        assert values == []
    else:
        assert len(values) == 1 and values[0].published_at.hour == 20
        assert values[0].supporting[0].published_at is None
        assert values[0].supporting[0].time_precision == "unknown"
        assert "870" in values[0].supporting[0].excerpt
        events = select_events(values, [source], now - timedelta(hours=48), now, [])
        assert len(events) == 1 and len(events[0].evidence) == 2


def test_undated_source_and_observation_are_rendered_honestly(digest, tmp_path):
    from ai_news.drafts import make_drafts
    from ai_news.site import build_site

    first = digest.stories[0]
    observed = first.model_copy(
        update={
            "id": "preview",
            "title": "尚未确认的模型",
            "section": "observation",
            "sources": [
                first.sources[0].model_copy(update={"published_at": None, "time_precision": "unknown"})
            ],
        }
    )
    revised = digest.model_copy(update={"editorial_version": 2, "stories": [first, observed]})
    build_site([revised], tmp_path, "https://reader.github.io/news")
    text = (tmp_path / "issues" / f"{digest.issue}.html").read_text("utf-8")
    assert "原文未注明发布时间" in text and "前瞻观察" in text
    drafts = make_drafts(revised, "https://reader.github.io/news")
    assert "尚未确认的模型" not in drafts.short
    assert "前瞻观察·未经官方确认" in drafts.long
