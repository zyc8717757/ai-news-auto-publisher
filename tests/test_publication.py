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
    base = digest.model_copy(
        update={
            "mode": "rules",
            "stories": [digest.stories[0], digest.stories[0].model_copy(update={"id": "second"})],
        }
    )
    store.save_digest(base)
    store.save_digest(digest.model_copy(update={"revision": 2}))

    class AI:
        def enhance(self, events):
            return AIResponse(
                stories=[
                    AIStory(
                        event_id=e.id,
                        title="已核实的新闻",
                        summary="这是根据冻结证据生成的中文摘要，保留原始事实。",
                        impact="便于读者理解已发布的消息。",
                        category="模型",
                        value_score=80,
                        evidence_ids=[e.evidence[0].id],
                    )
                    for e in (events[:1] if incomplete else events)
                ]
            )

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


def test_low_budget_frozen_recovery_reserves_one_summary_and_one_review_request(
    digest, settings, tmp_path, now, monkeypatch
):
    import json
    from types import SimpleNamespace

    from ai_news.ai import Enhancer
    from ai_news.models import AIResponse, AIStory
    from ai_news.pipeline import enhance_frozen

    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    settings.ai_enabled = True
    settings.ai_run_limit = 20
    settings.model = "gemini-3.5-flash-lite"
    settings.backup_model = "gemini-3.1-flash-lite"
    settings.tertiary_model = "gemini-3.8-flash"
    base = digest.model_copy(update={"mode": "rules"})
    store = Store(tmp_path)
    store.state.ai_issue_attempts[base.issue] = 18
    store.save_digest(base)
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            prompt = kwargs["contents"]
            if "source_passages" in prompt and "numeric_spans" in prompt:
                story = base.stories[0]
                return SimpleNamespace(
                    text=json.dumps(
                        {
                            "reviews": [
                                {
                                    "event_id": story.id,
                                    "approved": True,
                                    "translation_ok": True,
                                    "failure": "none",
                                    "reason": "通过",
                                    "replacement": None,
                                    "numeric_checks": [],
                                }
                            ]
                        }
                    )
                )
            event_id = base.stories[0].id
            result = AIResponse(
                stories=[
                    AIStory(
                        event_id=event_id,
                        title="模型更新",
                        summary="公司发布新的模型版本，效果仍需独立验证。",
                        impact="可为开发者提供参考。",
                        category="模型",
                        value_score=80,
                        evidence_ids=[base.stories[0].sources[0].id],
                    )
                ]
            )
            return SimpleNamespace(text=result.model_dump_json())

    client = Client()
    monkeypatch.setattr(
        "ai_news.pipeline.Enhancer", lambda *a, **k: Enhancer(settings, store, client, issue=base.issue)
    )
    # The fake SDK client is shared by the summary and independent-review calls.
    _, created = enhance_frozen(settings, store, base.issue, base_key=base.key, now=now)
    assert created
    assert calls == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    assert store.state.ai_issue_attempts[base.issue] == 20


def test_failed_free_test_recovery_uses_the_next_free_model_without_reset(
    digest, settings, tmp_path, now, monkeypatch
):
    from ai_news.ai import AIUnavailable, Enhancer
    from ai_news.models import AIResponse
    from ai_news.pipeline import enhance_frozen

    issue = digest.issue
    settings.ai_enabled = True
    settings.ai_run_limit = 20
    settings.model = "gemini-3.5-flash-lite"
    settings.backup_model = "gemini-3.1-flash-lite"
    settings.tertiary_model = "gemini-3.8-flash"
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", "5")
    base = digest.model_copy(update={"mode": "rules"})
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 18
    store.state.ai_test_attempts[issue] = 2
    store.save_digest(base)
    selected_models = []

    class AI:
        def enhance(self, events):
            return AIResponse(stories=[])

    def fake_enhancer(*args, **kwargs):
        instance = Enhancer(
            settings, store, client=object(), issue=issue, test_extra_limit=kwargs.get("test_extra_limit", 0)
        )

        def capture(events, **options):
            selected_models.append(options.get("initial_model"))
            return AI().enhance(events)

        instance.enhance = capture
        return instance

    async def no_refresh(events, settings):
        return events

    monkeypatch.setattr("ai_news.pipeline.refresh_frozen_evidence", no_refresh)
    # The explicit callback is unused by compact recovery, so only capture model selection.
    monkeypatch.setattr("ai_news.pipeline.Enhancer", fake_enhancer)
    monkeypatch.setattr(
        "ai_news.editorial.EditorialEnhancer.review_summaries", lambda self, *args, **kwargs: []
    )
    with pytest.raises(AIUnavailable, match="coverage_incomplete"):
        enhance_frozen(settings, store, issue, now=now, test_extra_limit=5, base_key=base.key)
    assert selected_models == ["gemini-3.8-flash"]
    assert store.state.ai_issue_attempts[issue] == 18
    assert store.state.ai_test_attempts[issue] == 2


def test_explicit_test_allowance_repairs_one_batch_and_reviews_without_resetting_production(
    digest, settings, tmp_path, now, monkeypatch
):
    import json
    from types import SimpleNamespace

    from ai_news.ai import Enhancer
    from ai_news.models import AIResponse, AIStory
    from ai_news.pipeline import enhance_frozen

    issue = digest.issue
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", "5")
    monkeypatch.setenv("GEMINI_RPD", "500")
    settings.ai_enabled = True
    settings.ai_run_limit = 20
    settings.model = "gemini-3.5-flash-lite"
    settings.backup_model = "gemini-3.1-flash-lite"
    settings.tertiary_model = "gemini-3.8-flash"
    base = digest.model_copy(update={"mode": "rules"})
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 18
    store.save_digest(base)
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            prompt = kwargs["contents"]
            event_id = base.stories[0].id
            if "source_passages" in prompt and "numeric_spans" in prompt:
                return SimpleNamespace(
                    text=json.dumps(
                        {
                            "reviews": [
                                {
                                    "event_id": event_id,
                                    "approved": True,
                                    "translation_ok": True,
                                    "failure": "none",
                                    "reason": "通过",
                                    "replacement": None,
                                    "numeric_checks": [],
                                }
                            ]
                        }
                    )
                )
            story = AIStory(
                event_id=event_id,
                title="模型更新",
                summary="公司发布了新的模型版本，效果仍需独立验证。",
                impact="可为开发者提供参考。",
                category="模型",
                value_score=80,
                evidence_ids=[base.stories[0].sources[0].id],
            )
            if calls.count("gemini-3.1-flash-lite") == 1:
                story.summary += "性能提升99倍。"
            return SimpleNamespace(text=AIResponse(stories=[story]).model_dump_json())

    client = Client()

    async def no_refresh(events, config):
        return events

    monkeypatch.setattr("ai_news.pipeline.refresh_frozen_evidence", no_refresh)
    monkeypatch.setattr(
        "ai_news.pipeline.Enhancer",
        lambda *a, **k: Enhancer(
            settings, store, client, issue=issue, test_extra_limit=k.get("test_extra_limit", 0)
        ),
    )
    result, created = enhance_frozen(settings, store, issue, now=now, test_extra_limit=5, base_key=base.key)
    assert created and result.mode == "ai" and result.stories
    assert len(calls) == 3
    assert store.state.ai_issue_attempts[issue] == 18
    assert store.state.ai_test_attempts[issue] == 3


def test_last_production_and_test_slots_are_split_between_complete_summary_and_review(
    digest, settings, tmp_path, now, monkeypatch
):
    import json
    from types import SimpleNamespace

    from ai_news.ai import AIResponse, AIStory, Enhancer
    from ai_news.pipeline import enhance_frozen

    issue = digest.issue
    settings.ai_enabled = True
    settings.ai_run_limit = 20
    settings.model, settings.backup_model, settings.tertiary_model = (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    )
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", "5")
    base = digest.model_copy(update={"mode": "rules"})
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 19
    store.state.ai_test_attempts[issue] = 4
    store.save_digest(base)
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            story = base.stories[0]
            calls.append(
                (
                    kwargs["model"],
                    Store(tmp_path).state.ai_issue_attempts[issue],
                    Store(tmp_path).state.ai_test_attempts[issue],
                )
            )
            prompt = kwargs["contents"]
            if "source_passages" in prompt and "numeric_spans" in prompt:
                return SimpleNamespace(
                    text=json.dumps(
                        {
                            "reviews": [
                                {
                                    "event_id": story.id,
                                    "approved": True,
                                    "translation_ok": True,
                                    "failure": "none",
                                    "reason": "通过",
                                    "replacement": None,
                                    "numeric_checks": [],
                                }
                            ]
                        }
                    )
                )
            summary = AIStory(
                event_id=story.id,
                title="模型更新",
                summary="公司发布新的模型版本，效果仍需独立验证。",
                impact="可为开发者提供参考。",
                category="模型",
                value_score=80,
                evidence_ids=[story.sources[0].id],
            )
            return SimpleNamespace(text=AIResponse(stories=[summary]).model_dump_json())

    async def no_refresh(events, config):
        return events

    client = Client()
    monkeypatch.setattr("ai_news.pipeline.refresh_frozen_evidence", no_refresh)
    monkeypatch.setattr(
        "ai_news.pipeline.Enhancer",
        lambda *a, **k: Enhancer(
            settings, store, client, issue=issue, test_extra_limit=k.get("test_extra_limit", 0)
        ),
    )
    result, created = enhance_frozen(settings, store, issue, now=now, test_extra_limit=5, base_key=base.key)
    assert created and result.mode == "ai" and len(result.stories) == 1
    assert calls == [(settings.model, 20, 4), (settings.model, 20, 5)]
    assert store.state.ai_issue_attempts[issue] == 20
    assert store.state.ai_test_attempts[issue] == 5
