import json

import httpx
import pytest

from ai_news.ai import AIUnavailable, Budget
from ai_news.editorial import EditorialEnhancer, Ranking, validate_ranking
from ai_news.models import NewsEvent
from ai_news.store import Store


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "merge_id", "merge_conflict"])
def test_rank_contract_errors_use_the_bounded_repair_path(
    settings, source_item, tmp_path, now, monkeypatch, kind
):
    events = {
        key: NewsEvent(id=key, evidence=[source_item], entities=["lab"], versions=[version])
        for key, version in (("a", "1"), ("b", "2"))
    }
    bad = {
        "unknown": {"ordered_ids": ["not_an_event"], "merges": []},
        "duplicate": {"ordered_ids": ["a", "a"], "merges": []},
        "merge_id": {"ordered_ids": ["a"], "merges": [{"keep_id": "a", "other_ids": ["unknown"]}]},
        "merge_conflict": {"ordered_ids": ["a"], "merges": [{"keep_id": "a", "other_ids": ["b"]}]},
    }[kind]
    store = Store(tmp_path)
    editor = EditorialEnhancer(settings, store, budget=Budget(settings, store, now=lambda: now))
    prompts = []

    def generate(*args):
        prompts.append(args[2])
        return json.dumps(bad if len(prompts) == 1 else {"ordered_ids": ["a", "b"], "merges": []})

    monkeypatch.setattr(editor, "generate_text", generate)
    result = editor.call(
        object(),
        "阶段2排序",
        {"candidates": [{"event_id": key} for key in events]},
        Ranking,
        "rank",
        validator=lambda r: validate_ranking(r, events),
    )
    assert result.ordered_ids == ["a", "b"]
    assert len(Store(tmp_path).state.ai_attempts) == 2
    assert "可用事件ID" in prompts[1]
    assert store.state.ai_audit[0].stage == "output_rejected"
    assert store.state.ai_audit[-1].stage == "accepted"


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504, "timeout"])
def test_editorial_transients_switch_backup_and_keep_it_across_phases(
    settings, tmp_path, now, monkeypatch, code
):
    settings.model, settings.backup_model = "gemini-3.5-flash-lite", "gemini-3.8-flash"
    store = Store(tmp_path)
    editor = EditorialEnhancer(
        settings, store, budget=Budget(settings, store, now=lambda: now, sleep=lambda n: None)
    )
    calls = []

    def generate(client, model, prompt, limit):
        calls.append(model)
        assert len(Store(tmp_path).state.ai_attempts) == len(calls)
        if len(calls) == 1:
            if code == "timeout":
                raise httpx.ReadTimeout("offline")
            exc = RuntimeError("offline")
            exc.code = code
            raise exc
        return '{"ordered_ids":["a"],"merges":[]}'

    monkeypatch.setattr(editor, "generate_text", generate)
    editor.call(object(), "first", {}, Ranking, "screen")
    editor.call(object(), "next", {}, Ranking, "rank")
    assert calls == [settings.model, settings.backup_model, settings.backup_model]


@pytest.mark.parametrize("code", [400, 403, 404])
def test_editorial_terminal_errors_do_not_retry(settings, tmp_path, now, monkeypatch, code):
    store = Store(tmp_path)
    editor = EditorialEnhancer(settings, store, budget=Budget(settings, store, now=lambda: now))

    def generate(*args):
        exc = RuntimeError("offline")
        exc.code = code
        raise exc

    monkeypatch.setattr(editor, "generate_text", generate)
    with pytest.raises(AIUnavailable):
        editor.call(object(), "task", {}, Ranking, "screen")
    assert len(Store(tmp_path).state.ai_attempts) == 1


def test_unavailable_primary_and_backup_stop_after_three_reserved_attempts(
    settings, tmp_path, now, monkeypatch
):
    settings.model, settings.backup_model = "gemini-3.5-flash-lite", "gemini-3.8-flash"
    store = Store(tmp_path)
    editor = EditorialEnhancer(
        settings, store, budget=Budget(settings, store, now=lambda: now, sleep=lambda n: None)
    )

    def generate(*args):
        exc = RuntimeError("offline")
        exc.code = 504
        raise exc

    monkeypatch.setattr(editor, "generate_text", generate)
    with pytest.raises(AIUnavailable, match="504"):
        editor.call(object(), "task", {}, Ranking, "rank")
    assert len(Store(tmp_path).state.ai_attempts) == 3
