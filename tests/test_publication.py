import json
from datetime import timedelta

import pytest

from ai_news.drafts import TextCounter, make_drafts
from ai_news.models import Digest, SourceCache
from ai_news.pipeline import cutoff_for, generate, scheduled_issue
from ai_news.site import audit_site, build_site
from ai_news.store import Store


def test_official_counting_and_both_drafts(digest):
    counter = TextCounter()
    assert counter.weights(["中文", "👨‍👩‍👧‍👦", "https://example.com/" + "a" * 100]) == [4, 2, 23]
    drafts = make_drafts(digest, "https://reader.github.io/news/issues/2026-10-10-am.html")
    assert drafts.short_weight <= 280 and drafts.long_weight <= 8000
    assert digest.stories[0].summary in drafts.long
    assert "https://reader.github.io/news/issues/2026-10-10-am.html" in drafts.short


def test_fifteen_stories_all_retained(digest):
    stories = [
        digest.stories[0].model_copy(update={"id": f"event-{i}", "title": f"演示新闻 {i}"}) for i in range(15)
    ]
    long_digest = digest.model_copy(update={"stories": stories})
    drafts = make_drafts(long_digest, "https://reader.github.io/news/issues/2026-10-10-am.html")
    assert drafts.short_weight <= 280 and drafts.long_weight <= 8000
    assert all(f"演示新闻 {i}" in drafts.long for i in range(15))


def test_empty_digest(digest):
    empty = digest.model_copy(update={"stories": [], "mode": "rules"})
    drafts = make_drafts(empty, "https://reader.github.io/news")
    assert drafts.short_weight <= 280 and "暂无" in drafts.long


def test_public_site_escape_archive_and_privacy(digest, tmp_path):
    story = digest.stories[0].model_copy(update={"title": '<script>alert("x")</script>'})
    revised = digest.model_copy(update={"revision": 2, "stories": [story]})
    build_site([digest, revised], tmp_path, "https://reader.github.io/news", "reader/news")
    page = (tmp_path / "issues/2026-10-10-am.html").read_text("utf-8")
    assert '<script>alert("x")</script>' not in page
    assert "&lt;script&gt;" in page and 'data-digest="2026-10-10-am-r2"' in page
    assert "/news/assets/style.css" in page
    assert (tmp_path / "archive/2026-10.html").exists()
    assert (tmp_path / "revisions/2026-10-10-am-r1.html").exists()
    data = json.loads((tmp_path / "data/2026-10-10-am-r2.json").read_text("utf-8"))
    assert "short" not in data and "excerpt" not in data["stories"][0]["sources"][0]
    assert all("版本 A".encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    (tmp_path / "private.eml").write_text("PRIVATE_MAIL")
    with pytest.raises(ValueError):
        audit_site(tmp_path)


def test_unknown_public_fields_rejected(digest):
    raw = digest.model_dump()
    raw["x_draft"] = "PRIVATE"
    with pytest.raises(ValueError):
        Digest.model_validate(raw)


def test_schedule_late_and_future(now):
    assert scheduled_issue("0 8 * * *", now) == "2026-10-10-am"
    assert scheduled_issue("0 8 * * *", now + timedelta(hours=7)) is None
    assert cutoff_for("2026-10-10-pm").hour == 12
    with pytest.raises(ValueError):
        cutoff_for("../../etc/passwd")


def test_generation_idempotent_and_regeneration(settings, official, source_item, now, tmp_path):
    calls = []

    class Collector:
        async def collect(self, sources, caches, moment):
            calls.append(caches)
            return [source_item], {official.id: SourceCache(etag="new")}

    store = Store(tmp_path)
    digest, created = generate(settings, [official], store, "2026-10-10-am", now=now, collector=Collector())
    assert created and digest.mode == "rules" and len(digest.stories) == 1
    again, created = generate(settings, [official], store, "2026-10-10-am", now=now, collector=Collector())
    assert not created and again.key == digest.key and len(calls) == 1
    revised, _ = generate(settings, [official], store, "2026-10-10-am", True, now=now, collector=Collector())
    assert revised.revision == 2 and len(revised.stories) == 1 and calls[-1] == {}
    assert len(store.digests()) == 2


def test_total_source_failure_does_not_advance_coverage(settings, official, now, tmp_path):
    class Collector:
        async def collect(self, *args):
            return [], {official.id: SourceCache(status="error")}

    store = Store(tmp_path)
    with pytest.raises(RuntimeError, match="all_sources"):
        generate(settings, [official], store, "2026-10-10-am", now=now, collector=Collector())
    assert store.state.last_covered_at is None and not store.digests()


def test_unpublished_revision_does_not_swallow_unreported_news(
    settings, official, source_item, now, tmp_path
):
    class Collector:
        def __init__(self, items):
            self.items = items

        async def collect(self, *args):
            return self.items, {official.id: SourceCache()}

    store = Store(tmp_path)
    first, _ = generate(
        settings, [official], store, "2026-10-10-am", now=now, collector=Collector([source_item])
    )
    store.state.issues[first.key].deployment = "ready"
    store.save_state()
    new = source_item.model_copy(
        update={"id": "new", "url": "https://example.com/new-v3", "title": "Example AI model v3.0 released"}
    )
    pending, _ = generate(
        settings, [official], store, "2026-10-10-am", True, now=now, collector=Collector([new])
    )
    assert store.state.issues[pending.key].deployment == "pending"
    evening, _ = generate(
        settings,
        [official],
        store,
        "2026-10-10-pm",
        now=now + timedelta(hours=12),
        collector=Collector([source_item, new]),
    )
    assert [s.title for s in evening.stories] == [new.title]


def test_frozen_ai_upgrade_preserves_period_and_is_idempotent(digest, settings, tmp_path, now):
    from ai_news.models import AIResponse, AIStory
    from ai_news.pipeline import enhance_frozen

    store = Store(tmp_path)
    old = digest.model_copy(update={"mode": "rules", "warnings": ["ai_unavailable", "source_errors"]})
    store.state.last_covered_at = old.cutoff
    store.state.ai_attempts = [now]
    store.state.ai_issue_attempts[old.issue] = 4
    store.save_digest(old)
    calls = []

    class AI:
        def enhance(self, events):
            calls.append(events)
            return AIResponse(
                stories=[
                    AIStory(
                        event_id=e.id,
                        title="模型消息",
                        summary="依据已经冻结的来源信息生成中文摘要，保留事实归因。",
                        impact="便于读者理解消息的实际意义。",
                        category="模型",
                        value_score=80,
                        evidence_ids=[e.evidence[0].id],
                    )
                    for e in events
                ]
            )

    result, created = enhance_frozen(settings, store, old.issue, AI(), now)
    assert created and result.revision == 2 and result.mode == "ai"
    assert result.cutoff == old.cutoff and result.window_start == old.window_start
    assert store.state.last_covered_at == old.cutoff and store.state.ai_issue_attempts[old.issue] == 4
    assert result.warnings == ["source_errors"]
    again, created = enhance_frozen(settings, store, old.issue, AI(), now)
    assert not created and again.key == result.key and len(calls) == 1


@pytest.mark.parametrize("incomplete", [False, True])
def test_explicit_frozen_base_requires_all_events_and_keeps_revision_history(
    digest, settings, tmp_path, now, incomplete
):
    from ai_news.ai import AIUnavailable
    from ai_news.models import AIResponse, AIStory
    from ai_news.pipeline import enhance_frozen

    store = Store(tmp_path)
    base = digest.model_copy(update={"mode": "rules", "stories": [digest.stories[0],
                                    digest.stories[0].model_copy(update={"id": "second"})]})
    store.save_digest(base)
    store.save_digest(digest.model_copy(update={"revision": 2}))

    class AI:
        def enhance(self, events):
            return AIResponse(stories=[AIStory(event_id=e.id, title="已核实的新闻",
                summary="这是根据冻结证据生成的中文摘要，保留原始事实。", impact="便于读者理解已发布的消息。",
                category="模型", value_score=80, evidence_ids=[e.evidence[0].id])
                for e in (events[:1] if incomplete else events)])

    if incomplete:
        with pytest.raises(AIUnavailable, match="coverage_incomplete"):
            enhance_frozen(settings, store, base.issue, AI(), now, base_key=base.key)
        assert store.latest(base.issue).revision == 2
    else:
        result, created = enhance_frozen(settings, store, base.issue, AI(), now, base_key=base.key)
        assert created and result.revision == 3 and len(result.stories) == 2
        assert {s.id for s in result.stories} == {s.id for s in base.stories}
        again, created = enhance_frozen(settings, store, base.issue, AI(), now, base_key=base.key)
        assert not created and again.key == result.key
        assert len(store.digests()) == 3
