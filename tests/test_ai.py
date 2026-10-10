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


def test_thirty_candidates_use_one_request_and_backup_on_quota(settings, tmp_path, monkeypatch, source_item):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [NewsEvent(id=f"event-{i}", evidence=[source_item]) for i in range(30)]
    calls = []

    class QuotaError(Exception):
        code = 429

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise QuotaError()
            stories = [response(event).stories[0] for event in events]
            return SimpleNamespace(text=AIResponse(stories=stories).model_dump_json())

    output = Enhancer(settings, Store(tmp_path), Client()).enhance(events)
    assert len(output.stories) == 30
    assert calls == ["gemini-2.5-flash-lite", "gemini-2.5-flash"]


def test_fallback_retry_and_repair_share_configured_ceiling(settings, tmp_path, monkeypatch, source_item):
    settings.ai_enabled = True
    settings.ai_run_limit = 3
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    calls = []

    class ServiceError(Exception):
        code = 503

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if len(calls) <= 2:
                raise ServiceError()
            return SimpleNamespace(text="invalid JSON")

    store = Store(tmp_path)
    with pytest.raises(AIUnavailable):
        Enhancer(settings, store, Client()).enhance([NewsEvent(id="event", evidence=[source_item])])
    assert len(calls) == 3 and len(Store(tmp_path).state.ai_attempts) == 3


def test_absolute_five_request_ceiling(settings, tmp_path, monkeypatch, now):
    monkeypatch.setenv("GEMINI_RPM", "10")
    store = Store(tmp_path)
    budget = Budget(settings, store, now=lambda: now)
    for _ in range(5):
        budget.reserve(100)
    with pytest.raises(AIUnavailable):
        budget.reserve(100)
    assert len(Store(tmp_path).state.ai_attempts) == 5


def test_issue_ceiling_survives_regeneration_and_legacy_migration(settings, tmp_path, now, monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "20")
    store = Store(tmp_path)
    store.state.ai_attempts = [now]
    store.save_state()
    for _ in range(4):
        Budget(settings, Store(tmp_path), now=lambda: now, issue="2026-10-10-am").reserve(100)
    with pytest.raises(AIUnavailable, match="issue_budget"):
        Budget(settings, Store(tmp_path), now=lambda: now, issue="2026-10-10-am").reserve(100)
    assert Store(tmp_path).state.ai_issue_attempts["2026-10-10-am"] == 5


def test_provider_failure_never_echoes_secret():
    from ai_news.ai import provider_failure

    error = RuntimeError("API key not valid: PRIVATE_SHOULD_NEVER_APPEAR")
    assert provider_failure(error) == "ai_key_invalid"


def test_two_authorized_test_calls_do_not_reset_production_budget(settings, tmp_path, now, monkeypatch):
    issue = "2026-10-10-am"
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("GEMINI_RPM", "20")
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 5
    store.state.ai_attempts = [now] * 5
    store.save_state()
    for _ in range(2):
        Budget(settings, Store(tmp_path), now=lambda: now, issue=issue, test_extra_limit=2).reserve(100)
    recovered = Store(tmp_path)
    assert recovered.state.ai_issue_attempts[issue] == 5
    assert recovered.state.ai_test_attempts[issue] == 2
    assert len(recovered.state.ai_attempts) == 7
    with pytest.raises(AIUnavailable):
        Budget(settings, recovered, now=lambda: now, issue=issue, test_extra_limit=2).reserve(100)


def test_test_allowance_requires_exact_authorized_issue(settings, tmp_path, now, monkeypatch):
    monkeypatch.delenv("AI_TEST_AUTHORIZED_ISSUE", raising=False)
    store = Store(tmp_path)
    with pytest.raises(AIUnavailable, match="not_authorized"):
        Budget(settings, store, now=lambda: now, issue="2026-10-10-am", test_extra_limit=2).reserve(100)
    assert store.state.ai_attempts == []


def test_extra_tests_still_obey_daily_limit(settings, tmp_path, now, monkeypatch):
    issue = "2026-10-10-am"
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    store = Store(tmp_path)
    store.state.ai_attempts = [now] * 10
    with pytest.raises(AIUnavailable):
        Budget(settings, store, now=lambda: now, issue=issue, test_extra_limit=2).reserve(100)


def test_date_claims_use_the_supplied_publication_evidence(source_item):
    event = NewsEvent(id="event", evidence=[source_item])
    result = response(event)
    result.stories[
        0
    ].summary = f"来源于{source_item.published_at.month}月{source_item.published_at.day}日公布该模型消息，效果尚待独立验证。"
    validate_response(result, [event])


def test_real_sdk_sends_json_schema_instead_of_legacy_proto_schema(
    settings, tmp_path, monkeypatch, source_item
):
    import httpx
    from google import genai
    from google.genai import types

    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    event = NewsEvent(id="event", evidence=[source_item])
    captured = []

    def transport(request):
        config = json.loads(request.content)["generationConfig"]
        captured.append(config)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": response(event).model_dump_json()}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    client = genai.Client(
        api_key="DUMMY_NOT_REAL",
        vertexai=False,
        http_options=types.HttpOptions(httpx_client=httpx.Client(transport=httpx.MockTransport(transport))),
    )
    output = Enhancer(settings, Store(tmp_path), client).enhance([event])
    assert output.stories[0].event_id == "event"
    assert "responseSchema" not in captured[0]
    assert captured[0]["responseJsonSchema"]["additionalProperties"] is False
    assert "additional_properties" not in json.dumps(captured[0]["responseJsonSchema"])
