import json
from datetime import timedelta

import pytest

from ai_news.ai import AIUnavailable, Budget, Enhancer
from ai_news.editorial import EditorialEnhancer, Ranking, Screening, validate_or_separate_merges
from ai_news.mailer import MailConfig, deliver
from ai_news.models import NewsEvent, SourceCache
from ai_news.pipeline import generate
from ai_news.site import audit_site, build_site
from ai_news.store import Store


def test_invalid_cross_version_merge_keeps_both_events(source_item):
    events = {
        key: NewsEvent(id=key, evidence=[source_item], entities=["lab"], versions=[version])
        for key, version in [("a", "1"), ("b", "2")]
    }
    ranking = Ranking(ordered_ids=["a"], merges=[{"keep_id": "a", "other_ids": ["b"]}])
    validate_or_separate_merges(ranking, events)
    assert ranking.ordered_ids == ["a", "b"] and not ranking.merges
    invalid = Ranking(ordered_ids=["a"], merges=[{"keep_id": "a", "other_ids": ["unknown"]}])
    with pytest.raises(ValueError, match="IDs invalid"):
        validate_or_separate_merges(invalid, events)


def test_screen_schema_failure_repairs_then_changes_model_with_exact_feedback(
    settings, tmp_path, now, monkeypatch
):
    settings.ai_run_limit = 20
    store = Store(tmp_path)
    editor = EditorialEnhancer(
        settings,
        store,
        budget=Budget(settings, store, now=lambda: now + timedelta(minutes=len(store.state.ai_attempts))),
    )
    calls = []

    def respond(client, model, prompt, limit):
        calls.append((model, prompt))
        return json.dumps(
            {
                "decisions": [
                    {
                        "event_id": "a",
                        "include": True,
                        "category": "错误分类" if len(calls) <= 2 else "产品",
                        "importance": 90,
                        "reason": "有实际价值的产品发布",
                    }
                ]
            }
        )

    monkeypatch.setattr(editor, "generate_text", respond)
    result = editor.call(object(), "screen", [{"event_id": "a"}], Screening, "screen")
    assert result.decisions[0].event_id == "a"
    assert [m for m, _ in calls] == [settings.model, settings.model, settings.backup_model]
    assert "字段错误位置和类型" in calls[1][1] and "literal_error" in calls[1][1]
    assert '完整覆盖事件ID：["a"]' in calls[1][1] and "category只能" in calls[1][1]
    assert len(store.state.ai_attempts) == 3


def test_complete_editorial_flow_repairs_only_failed_events(
    settings, official, tmp_path, source_item, now, monkeypatch
):
    settings.ai_enabled, settings.ai_run_limit = True, 20
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [NewsEvent(id=key, evidence=[source_item], rule_score=90) for key in ("a", "b", "c")]
    store = Store(tmp_path)
    budget = Budget(
        settings,
        store,
        issue="2026-10-10-am",
        now=lambda: now + timedelta(minutes=len(store.state.ai_attempts)),
        sleep=lambda _: None,
    )
    editor = EditorialEnhancer(settings, store, client=object(), budget=budget)
    summary_batches, review_batches = [], []

    def respond(self, client, model, prompt, limit):
        if "阶段1：" in prompt:
            result = {
                "decisions": [
                    {
                        "event_id": e.id,
                        "include": True,
                        "category": "产品",
                        "importance": 90,
                        "reason": "产品更新具有实际使用价值",
                    }
                    for e in events
                ]
            }
        elif "阶段2：" in prompt:
            result = {"ordered_ids": [e.id for e in events], "merges": []}
        elif "阶段4：" in prompt:
            data = json.JSONDecoder().raw_decode(prompt.split("\n只返回JSON，不要代码围栏。输入：")[1])[0]
            keys = [x["story"]["event_id"] for x in data]
            review_batches.append(keys)
            result = {
                "reviews": [
                    {
                        "event_id": key,
                        "approved": not (key == "b" and len(keys) > 1),
                        "translation_ok": True,
                        "failure": "translation" if key == "b" and len(keys) > 1 else "none",
                        "reason": "已核对原文与中文表达",
                        "numeric_checks": [],
                    }
                    for key in keys
                ]
            }
        else:
            data = json.JSONDecoder().raw_decode(prompt.split("\n输入数据：\n")[1])[0]
            keys = [x["event_id"] for x in data]
            summary_batches.append(keys)
            result = {
                "stories": [
                    {
                        "event_id": key,
                        "title": "公司更新产品",
                        "summary": "公司宣布更新产品，实际效果仍需独立验证。"
                        + ("性能提升999倍。" if key == "b" and len(keys) > 1 else ""),
                        "impact": "可能为开发者提供更多选择。",
                        "category": "产品",
                        "value_score": 90,
                        "evidence_ids": [source_item.id],
                    }
                    for key in keys
                ]
            }
        return json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(Enhancer, "generate_text", respond)
    monkeypatch.setattr("ai_news.editorial.EditorialEnhancer", lambda *a, **kw: editor)
    monkeypatch.setattr("ai_news.pipeline.select_events", lambda *a, **kw: events)

    class Collector:
        async def collect(self, *args):
            return [source_item], {official.id: SourceCache()}

    digest, created = generate(settings, [official], store, "2026-10-10-am", now=now, collector=Collector())
    assert created and digest.mode == "ai"
    assert [s.id for s in digest.stories] == [e.id for e in editor.selected_events] == ["a", "b", "c"]
    assert summary_batches == [["a", "b", "c"], ["b"]]
    assert review_batches == [["a", "b", "c"], ["b"]]
    assert len(store.state.ai_attempts) == store.state.ai_issue_attempts["2026-10-10-am"] == 6
    assert Store(store.root).latest(digest.issue).model_dump() == digest.model_dump()
    output = tmp_path / "site"
    build_site([digest], output, settings.base_url)
    audit_site(output)
    public = json.loads((output / "data" / f"{digest.key}.json").read_text("utf-8"))
    assert [s["id"] for s in public["stories"]] == ["a", "b", "c"]
    store.state.issues[digest.key].deployment = "ready"
    store.save_state()
    messages = []

    class Capture:
        def send(self, message):
            messages.append(message)

    config = MailConfig("smtp.example.com", 465, "ssl", "test", "test", "a@example.com", "b@example.com")
    assert deliver(digest, store, config, settings.base_url, channel=Capture()) == "sent"
    assert deliver(digest, store, config, settings.base_url, channel=Capture()) == "already_sent"
    assert len(messages) == 1 and len(list(messages[0].iter_attachments())) == 3


def test_failed_completion_preserves_all_selected_sources_in_frozen_digest(
    settings, official, source_item, now, tmp_path, monkeypatch
):
    settings.ai_enabled = True
    selected = [NewsEvent(id=key, evidence=[source_item], rule_score=50) for key in ("chosen-a", "chosen-b")]

    class Collector:
        async def collect(self, *args):
            return [source_item], {official.id: SourceCache()}

    def fail(self, *args):
        self.selected_events = selected
        raise AIUnavailable("ai_review_coverage_incomplete")

    monkeypatch.setattr(EditorialEnhancer, "edit", fail)
    store = Store(tmp_path / "data")
    digest, _ = generate(settings, [official], store, "2026-10-10-am", now=now, collector=Collector())
    assert digest.mode == "rules" and "ai_coverage_incomplete" in digest.warnings
    assert {s.id for s in digest.stories} == {e.id for e in selected}
    assert Store(store.root).latest(digest.issue).model_dump() == digest.model_dump()
    output = tmp_path / "site"
    build_site([digest], output, settings.base_url)
    audit_site(output)
    public = json.loads((output / "data" / f"{digest.key}.json").read_text("utf-8"))
    assert {s["id"] for s in public["stories"]} == {e.id for e in selected}


@pytest.mark.parametrize("limit,mode,ids", [(15, "rules", {"a", "b"}), (1, "ai", {"a"})])
def test_noneditorial_duplicate_suggestion_cannot_publish_partial_ai(
    settings, official, source_item, now, tmp_path, monkeypatch, limit, mode, ids
):
    from ai_news.models import AIResponse, AIStory

    settings.ai_enabled, settings.editorial_enabled = True, False
    settings.max_items = limit
    events = [NewsEvent(id=key, evidence=[source_item], rule_score=90) for key in ("a", "b")]
    monkeypatch.setattr("ai_news.pipeline.select_events", lambda *a, **kw: events)

    class Collector:
        async def collect(self, *args):
            return [source_item], {official.id: SourceCache()}

    class AI:
        def enhance(self, events):
            return AIResponse(
                stories=[
                    AIStory(
                        event_id=e.id,
                        title="产品更新",
                        summary="公司宣布更新产品，实际效果仍需验证。",
                        impact="可能提供更多产品选择。",
                        category="产品",
                        value_score=90,
                        evidence_ids=[source_item.id],
                        duplicate_of="a" if e.id == "b" else None,
                    )
                    for e in events
                ]
            )

    digest, _ = generate(
        settings,
        [official],
        Store(tmp_path),
        "2026-10-10-am",
        now=now,
        collector=Collector(),
        enhancer=AI(),
    )
    assert digest.mode == mode and {s.id for s in digest.stories} == ids
