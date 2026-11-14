import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ai_news.ai import AIUnavailable, Budget, Enhancer, checked_stories, validate_response
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


def request_payload(prompt):
    return json.loads(prompt.rsplit("\n输入数据：\n", 1)[1])


@pytest.mark.parametrize("bad_kind", ["number", "field", "evidence", "missing", "duplicate"])
def test_partial_summary_keeps_only_independently_valid_events(source_item, bad_kind):
    events = [NewsEvent(id=k, evidence=[source_item]) for k in ("good", "bad")]
    rows = [response(e).stories[0].model_dump() for e in events]
    if bad_kind == "number":
        rows[1]["summary"] += "性能提升999倍。"
    elif bad_kind == "field":
        rows[1]["impact"] = ""
    elif bad_kind == "evidence":
        rows[1]["evidence_ids"] = ["invented"]
    elif bad_kind == "missing":
        rows.pop()
    else:
        rows.append(rows[1].copy())
    rows.append({**rows[0], "event_id": "injected"})
    valid, failures = checked_stories(json.dumps({"stories": rows}), events)
    assert set(valid) == {"good"}
    assert set(failures) == {"bad"}


def test_strict_summary_rejects_unrequested_raw_event_even_if_expected_stories_are_valid(source_item):
    events = [NewsEvent(id=key, evidence=[source_item]) for key in ("a", "b")]
    stories = [response(event).stories[0].model_dump() for event in events]
    stories.append({**stories[0], "event_id": "alien"})
    with pytest.raises(ValueError, match="event set mismatch"):
        checked_stories(json.dumps({"stories": stories}), events, reject_unknown=True)


@pytest.mark.parametrize("repair_kind", ["invalid_number", "malformed", "fixed"])
def test_summary_repair_retains_good_siblings_and_persistent_budget(
    settings, tmp_path, monkeypatch, source_item, repair_kind
):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [NewsEvent(id=k, evidence=[source_item]) for k in ("good", "bad")]
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            if len(calls) == 2 and repair_kind == "malformed":
                return SimpleNamespace(text="invalid JSON")
            requested = {record["event_id"] for record in request_payload(kwargs["contents"])}
            stories = [response(e).stories[0] for e in events if e.id in requested]
            if len(calls) == 1 or repair_kind != "fixed":
                next(story for story in stories if story.event_id == "bad").summary += "性能提升999倍。"
            return SimpleNamespace(text=AIResponse(stories=stories).model_dump_json())

    store = Store(tmp_path)
    output = Enhancer(settings, store, Client()).enhance(events, allow_partial=True)
    assert [s.event_id for s in output.stories] == (["good", "bad"] if repair_kind == "fixed" else ["good"])
    assert len(Store(tmp_path).state.ai_attempts) == (2 if repair_kind == "fixed" else 3)
    assert "ai_numeric_claim_unsupported" in calls[1]
    assert "需要修正的字段" in calls[1] and '"field": "summary"' in calls[1]
    assert '"unit": "multiple"' in calls[1] and "不是事实证明" in calls[1]
    assert [record["event_id"] for record in request_payload(calls[0])] == ["good", "bad"]
    assert all([record["event_id"] for record in request_payload(prompt)] == ["bad"] for prompt in calls[1:])


def test_validation_only_audits_are_marked(settings, tmp_path):
    enhancer = Enhancer(settings, Store(tmp_path))
    enhancer.budget.validation_only = True
    enhancer.audit(settings.model, "accepted")
    assert Store(tmp_path).state.ai_audit[-1].validation_only is True


def test_default_summary_mode_never_returns_partial_batch_coverage(
    settings, tmp_path, monkeypatch, source_item
):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [NewsEvent(id=key, evidence=[source_item]) for key in ("good", "bad")]
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            if len(calls) == 2:
                return SimpleNamespace(text=json.dumps({"stories": []}))
            stories = [response(event).stories[0] for event in events]
            stories[1].summary += "性能提升999倍。"
            return SimpleNamespace(text=AIResponse(stories=stories).model_dump_json())

    enhancer = Enhancer(settings, Store(tmp_path), Client())
    with pytest.raises(AIUnavailable, match="ai_output_invalid"):
        enhancer.enhance(events, max_repairs=1)
    assert len(calls) == 2
    assert [record["event_id"] for record in request_payload(calls[1])] == ["bad"]


def test_numeric_repair_then_schema_repair_preserves_complete_event_set(
    settings, tmp_path, monkeypatch, source_item
):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [
        NewsEvent(id=key, evidence=[source_item.model_copy(update={"id": "e-" + key})])
        for key in ("good", "needs_repair")
    ]
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            requested = {record["event_id"] for record in request_payload(kwargs["contents"])}
            stories = [response(event).stories[0].model_dump() for event in events if event.id in requested]
            if len(calls) == 1:
                next(story for story in stories if story["event_id"] == "needs_repair")["summary"] += (
                    "提升99倍。"
                )
            elif len(calls) == 2:
                next(story for story in stories if story["event_id"] == "needs_repair")["debug_note"] = (
                    "不可由schema接收"
                )
            return SimpleNamespace(text=json.dumps({"stories": stories}, ensure_ascii=False))

    store = Store(tmp_path)
    result = Enhancer(settings, store, Client(), Budget(settings, store, sleep=lambda _: None)).enhance(
        events
    )
    assert {s.event_id for s in result.stories} == {e.id for e in events}
    assert len(Store(tmp_path).state.ai_attempts) == 3
    assert '"stories"' in calls[1] and '"event_id"' in calls[1]
    assert "reason" in calls[2] and "diagnostic" in calls[2]
    assert [record["event_id"] for record in request_payload(calls[0])] == ["good", "needs_repair"]
    assert all(
        [record["event_id"] for record in request_payload(prompt)] == ["needs_repair"] for prompt in calls[1:]
    )


def test_ai_summary_repairs_remain_bounded(settings, tmp_path, monkeypatch, source_item):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [NewsEvent(id="event", evidence=[source_item])]
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["contents"])
            return SimpleNamespace(text="invalid JSON")

    store = Store(tmp_path)
    with pytest.raises(AIUnavailable, match="ai_output_invalid"):
        Enhancer(settings, store, Client(), Budget(settings, store, sleep=lambda _: None)).enhance(events)
    assert len(calls) == len(Store(tmp_path).state.ai_attempts) == 3


def test_frozen_recovery_summarizes_each_original_event_independently(
    settings, tmp_path, monkeypatch, source_item
):
    settings.ai_enabled = True
    settings.ai_run_limit = 20
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    events = [
        NewsEvent(id=f"event-{i}", evidence=[source_item.model_copy(update={"id": f"e-{i}"})])
        for i in range(6)
    ]
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            payload = json.loads(kwargs["contents"].split("\n输入数据：\n")[-1])
            assert len(payload) == 1
            calls.append(payload[0]["event_id"])
            event = next(e for e in events if e.id == payload[0]["event_id"])
            return SimpleNamespace(text=AIResponse(stories=response(event).stories).model_dump_json())

    store = Store(tmp_path)
    enhancer = Enhancer(settings, store, Client(), Budget(settings, store, sleep=lambda _: None))
    result = enhancer.enhance(events, allow_partial=True, individual=True)
    assert {story.event_id for story in result.stories} == {event.id for event in events}
    assert len(calls) == len(Store(tmp_path).state.ai_attempts) == 6


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
            assert kwargs["config"].thinking_config.thinking_budget == 0
            assert kwargs["config"].response_json_schema["properties"]["stories"]["maxItems"] == 1
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


def test_summary_phase_uses_three_free_models_then_keeps_successful_model(
    settings, tmp_path, monkeypatch, source_item
):
    settings.ai_enabled = True
    settings.model = "gemini-3.5-flash-lite"
    settings.backup_model = "gemini-3.1-flash-lite"
    settings.tertiary_model = "gemini-3.8-flash"
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    calls = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if len(calls) < 3:
                exc = RuntimeError("transient provider failure")
                exc.code = 503
                raise exc
            event = NewsEvent(id="event", evidence=[source_item])
            return SimpleNamespace(text=response(event).model_dump_json())

    event = NewsEvent(id="event", evidence=[source_item])
    store = Store(tmp_path)
    enhancer = Enhancer(settings, store, Client(), Budget(settings, store, sleep=lambda _: None))
    assert enhancer.enhance([event]).stories[0].event_id == event.id
    assert calls == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
    assert enhancer.used_model == "gemini-3.8-flash"
    assert len(Store(tmp_path).state.ai_attempts) == 3


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


def test_primary_model_rpm_does_not_consume_backup_model_rpm(settings, tmp_path, now, monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "100")
    store = Store(tmp_path)
    primary = "gemini-3.5-flash-lite"
    backup = "gemini-3.1-flash-lite"
    for _ in range(6):
        Budget(settings, store, now=lambda: now).reserve(100, model=primary)

    Budget(settings, Store(tmp_path), now=lambda: now).reserve(100, model=backup)
    state = Store(tmp_path).state
    assert len(state.ai_model_attempts[primary]) == 6
    assert len(state.ai_model_attempts[backup]) == 1


def test_same_model_rpm_waits_until_enough_calls_leave_the_window(settings, tmp_path, monkeypatch, now):
    monkeypatch.setenv("GEMINI_RPM", "5")
    model = "gemini-3.1-flash-lite"
    previous = [
        now - timedelta(seconds=40),
        *(now - timedelta(seconds=offset) for offset in (20, 19, 18, 17, 16)),
    ]
    store = Store(tmp_path)
    store.state.ai_attempts = list(previous)
    store.state.ai_model_attempts[model] = list(previous)
    store.save_state()
    clock = {"now": now}
    delays = []

    def sleep(delay):
        delays.append(delay)
        clock["now"] += timedelta(seconds=delay)

    Budget(settings, store, now=lambda: clock["now"], sleep=sleep).reserve(100, model=model)
    assert len(delays) == 1 and delays[0] == pytest.approx(41)
    assert len(Store(tmp_path).state.ai_model_attempts[model]) == 7


def test_unknown_legacy_attempts_conservatively_block_every_model(settings, tmp_path, monkeypatch, now):
    monkeypatch.setenv("GEMINI_RPM", "5")
    store = Store(tmp_path)
    store.state.ai_attempts = [now] * 5
    store.save_state()
    budget = Budget(settings, store, now=lambda: now, sleep=lambda _: None)
    with pytest.raises(AIUnavailable, match="ai_rate_limit"):
        budget.reserve(100, model="gemini-3.1-flash-lite")
    assert len(Store(tmp_path).state.ai_attempts) == 5
    assert Store(tmp_path).state.ai_model_attempts == {}


def test_global_tpm_reservations_still_block_cross_model_overcommit(settings, tmp_path, monkeypatch, now):
    monkeypatch.setenv("GEMINI_TPM", "500")
    store = Store(tmp_path)
    store.state.ai_reserved_tokens = [(now, 450)]
    store.save_state()
    budget = Budget(settings, store, now=lambda: now, sleep=lambda _: None)
    with pytest.raises(AIUnavailable, match="ai_rate_limit"):
        budget.reserve(100, model="gemini-3.1-flash-lite")
    assert len(Store(tmp_path).state.ai_attempts) == 0


@pytest.mark.parametrize("code", [400, 403, 404])
def test_terminal_provider_errors_never_retry_or_switch_model(
    settings, tmp_path, monkeypatch, source_item, code
):
    settings.ai_enabled = True
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    calls = []

    class ProviderError(Exception):
        pass

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            exc = ProviderError()
            exc.code = code
            raise exc

    with pytest.raises(AIUnavailable):
        Enhancer(settings, Store(tmp_path), Client()).enhance([NewsEvent(id="event", evidence=[source_item])])
    assert calls == [settings.model]


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
    settings.ai_day_limit = 10
    issue = "2026-10-10-am"
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    store = Store(tmp_path)
    store.state.ai_attempts = [now] * 10
    with pytest.raises(AIUnavailable):
        Budget(settings, store, now=lambda: now, issue=issue, test_extra_limit=2).reserve(100)


def test_new_single_grant_keeps_old_two_test_calls(settings, tmp_path, now, monkeypatch):
    issue = "2026-10-10-am"
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", "3")
    monkeypatch.setenv("GEMINI_RPM", "20")
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 5
    store.state.ai_test_attempts[issue] = 2
    store.state.ai_attempts = [now] * 7
    Budget(settings, store, now=lambda: now, issue=issue, test_extra_limit=3).reserve(100)
    assert store.state.ai_issue_attempts[issue] == 5 and store.state.ai_test_attempts[issue] == 3
    assert len(store.state.ai_attempts) == 8
    with pytest.raises(AIUnavailable):
        Budget(settings, Store(tmp_path), now=lambda: now, issue=issue, test_extra_limit=3).reserve(100)


@pytest.mark.parametrize("old_used,ceiling", [(5, 10), (10, 13)])
def test_additional_calls_preserve_old_test_and_production_counter(
    settings, tmp_path, now, monkeypatch, old_used, ceiling
):
    issue = "2026-11-14-pm"
    settings.model = "gemini-3.5-flash-lite"
    monkeypatch.setenv("AI_TEST_AUTHORIZED_ISSUE", issue)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", str(ceiling))
    store = Store(tmp_path)
    store.state.ai_issue_attempts[issue] = 19
    store.state.ai_test_attempts[issue] = old_used
    store.save_state()
    for i in range(ceiling - old_used):
        Budget(
            settings,
            Store(tmp_path),
            issue=issue,
            test_extra_limit=ceiling,
            now=lambda i=i: now + timedelta(minutes=i * 2),
        ).reserve(100)
    saved = Store(tmp_path)
    assert saved.state.ai_issue_attempts[issue] == 19
    assert saved.state.ai_test_attempts[issue] == ceiling
    assert len(saved.state.ai_attempts) == ceiling - old_used
    with pytest.raises(AIUnavailable, match="issue_budget_exhausted"):
        Budget(
            settings, saved, issue=issue, test_extra_limit=ceiling, now=lambda: now + timedelta(minutes=12)
        ).reserve(100)
    monkeypatch.setenv("AI_TEST_AUTHORIZED_TOTAL", str(old_used))
    with pytest.raises(AIUnavailable, match="not_authorized"):
        Budget(settings, saved, issue=issue, test_extra_limit=ceiling).reserve(100)


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"])
@pytest.mark.parametrize("response_kind", ["success", "http_503", "connection_error"])
def test_modern_generate_content_has_no_hidden_retry(
    settings, tmp_path, monkeypatch, source_item, model, response_kind
):
    import httpx
    from google import genai
    from google.genai import types

    event = NewsEvent(id="event", evidence=[source_item])
    captured = []

    def handler(request):
        captured.append(request)
        if response_kind == "http_503":
            return httpx.Response(503, json={"error": {"code": 503, "message": "Offline"}})
        if response_kind == "connection_error":
            raise httpx.ConnectError("Offline", request=request)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": response(event).model_dump_json()}]}}]}
        )

    client = genai.Client(
        api_key="DUMMY_NOT_REAL",
        vertexai=False,
        http_options=types.HttpOptions(
            api_version="v1beta",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    enhancer = Enhancer(settings, Store(tmp_path))
    monkeypatch.delenv("AI_NATIVE_SCHEMA_MODELS", raising=False)
    if response_kind == "success":
        assert AIResponse.model_validate_json(enhancer.generate_text(client, model, "Offline", 6000)).stories
    else:
        with pytest.raises(Exception):
            enhancer.generate_text(client, model, "Offline", 6000)
    assert len(captured) == 1
    assert captured[0].url.path == f"/v1beta/models/{model}:generateContent"
    body = json.loads(captured[0].content)
    assert not body.get("tools")
    assert "thinkingConfig" not in body["generationConfig"]
    assert body["generationConfig"] == {"maxOutputTokens": 6000}


def test_date_claims_use_the_supplied_publication_evidence(source_item):
    event = NewsEvent(id="event", evidence=[source_item])
    result = response(event)
    result.stories[
        0
    ].summary = f"来源于{source_item.published_at.month}月{source_item.published_at.day}日公布该模型消息，效果尚待独立验证。"
    validate_response(result, [event])


def test_generate_text_uses_phase_output_schema(settings, tmp_path, monkeypatch):
    settings.model = "gemini-3.1-flash-lite"
    monkeypatch.setenv("AI_NATIVE_SCHEMA_MODELS", "gemini-3.1-flash-lite")
    schema = {
        "type": "object",
        "properties": {"reviews": {"type": "array", "items": {"type": "string"}}},
        "required": ["reviews"],
    }
    captured = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(text='{"reviews":[]}')

    enhancer = Enhancer(settings, Store(tmp_path), Client(), output_schema=schema)
    assert enhancer.generate_text(enhancer.client, settings.model, "prompt", 1000) == '{"reviews":[]}'
    assert captured[0]["config"].response_json_schema == schema
    assert enhancer.output_schema == schema


def test_live_incompatible_numeric_review_keeps_plain_transport_and_full_local_schema(
    settings, tmp_path, monkeypatch
):
    from ai_news.editorial import VerifiedReviews

    monkeypatch.setenv("AI_NATIVE_SCHEMA_MODELS", "gemini-3.5-flash-lite")
    schema = VerifiedReviews.model_json_schema()
    captured = []

    class Client:
        @property
        def models(self):
            return self

        def generate_content(self, **kwargs):
            captured.append(kwargs["config"])
            return SimpleNamespace(text='{"reviews":[]}')

    enhancer = Enhancer(settings, Store(tmp_path), Client(), output_schema=schema)
    enhancer.generate_text(enhancer.client, "gemini-3.5-flash-lite", "review", 12000)
    assert captured[0].response_json_schema is None and captured[0].thinking_config is None
    assert enhancer.output_schema == schema and "NumericAnchor" in schema["$defs"]


def test_real_sdk_sends_json_schema_instead_of_legacy_proto_schema(
    settings, tmp_path, monkeypatch, source_item
):
    import httpx
    from google import genai
    from google.genai import types

    settings.ai_enabled = True
    settings.model = "gemini-3.5-flash-lite"
    monkeypatch.setenv("AI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("AI_NATIVE_SCHEMA_MODELS", "gemini-3.5-flash-lite")
    events = [
        NewsEvent(id=event_id, evidence=[source_item.model_copy(update={"id": f"e-{event_id}"})])
        for event_id in ("event-a", "event-b")
    ]
    captured = []

    def transport(request):
        config = json.loads(request.content)["generationConfig"]
        captured.append(config)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": AIResponse(
                                        stories=[response(event).stories[0] for event in events]
                                    ).model_dump_json()
                                }
                            ],
                        },
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
    output = Enhancer(settings, Store(tmp_path), client).enhance(events)
    assert {story.event_id for story in output.stories} == {"event-a", "event-b"}
    assert "responseSchema" not in captured[0]
    assert captured[0]["responseMimeType"] == "application/json"
    assert "thinkingConfig" not in captured[0]
    schema = captured[0]["responseJsonSchema"]
    assert schema["additionalProperties"] is False
    assert "additional_properties" not in json.dumps(schema)
    stories = schema["properties"]["stories"]
    assert stories["minItems"] == stories["maxItems"] == 2
    story_ref = stories["items"]["$ref"].rsplit("/", 1)[1]
    story_schema = schema["$defs"][story_ref]
    assert story_schema["properties"]["event_id"]["enum"] == ["event-a", "event-b"]
    assert "enum" not in story_schema["properties"]["evidence_ids"]["items"]
