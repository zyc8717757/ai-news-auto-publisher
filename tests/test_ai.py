import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ai_news.ai import AIUnavailable, Budget, Enhancer, validate_response
from ai_news.models import AIResponse, AIStory, NewsEvent
from ai_news.store import Store


def response(event):
    return AIResponse(
        stories=[
            AIStory(
                event_id=event.id,
                title="模型更新",
                summary="该公司宣布了新的模型版本，实际效果仍需独立验证。",
                impact="可能为开发者提供更多选择。",
                category="模型",
                value_score=80,
                evidence_ids=[event.evidence[0].id],
            )
        ]
    )


def test_reject_unknown_evidence_and_numbers(source_item):
    event = NewsEvent(id="event", evidence=[source_item])
    result = response(event)
    result.stories[0].evidence_ids = ["invented"]
    with pytest.raises(ValueError):
        validate_response(result, [event])
    result = response(event)
    result.stories[0].summary += "性能提升99倍。"
    with pytest.raises(ValueError):
        validate_response(result, [event])


def test_unconfirmed_project_never_calls(settings, tmp_path, monkeypatch, source_item):
    settings.ai_enabled = True
    monkeypatch.delenv("AI_FREE_TIER_CONFIRMED", raising=False)
    enhancer = Enhancer(settings, Store(tmp_path))
    with pytest.raises(AIUnavailable):
        enhancer.enhance([NewsEvent(id="a", evidence=[source_item])])
    assert enhancer.store.state.ai_attempts == []


def test_attempt_reserved_before_request(settings, tmp_path, monkeypatch, source_item, now):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("GEMINI_TPM", "30000")
    store = Store(tmp_path)
    event = NewsEvent(id="event", evidence=[source_item])

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            assert len(Store(tmp_path).state.ai_attempts) == 1
            assert kwargs["model"] == "gemini-2.5-flash-lite"
            assert not kwargs["config"].tools
            return SimpleNamespace(text=response(event).model_dump_json())

    enhancer = Enhancer(settings, store, Client(), Budget(settings, store, now=lambda: now))
    assert enhancer.enhance([event]).stories[0].event_id == event.id


def test_budget_survives_restart(settings, tmp_path, monkeypatch, now):
    monkeypatch.setenv("GEMINI_RPM", "100")
    monkeypatch.setenv("GEMINI_RPD", "2")
    store = Store(tmp_path)
    Budget(settings, store, now=lambda: now).reserve(100)
    second = Store(tmp_path)
    Budget(settings, second, now=lambda: now + timedelta(minutes=2)).reserve(100)
    with pytest.raises(AIUnavailable):
        Budget(settings, Store(tmp_path), now=lambda: now + timedelta(minutes=4)).reserve(100)
    assert len(json.loads((tmp_path / "state.json").read_text())["ai_attempts"]) == 2


def test_persistence_failure_blocks_request(settings, tmp_path, monkeypatch, source_item):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    store = Store(tmp_path)
    monkeypatch.setattr(store, "save_state", lambda: (_ for _ in ()).throw(RuntimeError("no persistence")))

    class Client:
        @property
        def models(self):
            pytest.fail("Provider must not be invoked")

    with pytest.raises(RuntimeError):
        Enhancer(settings, store, Client()).enhance([NewsEvent(id="event", evidence=[source_item])])
