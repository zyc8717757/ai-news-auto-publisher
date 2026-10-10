import json

import httpx
import pytest

from ai_news.diagnostics import ENDPOINT, probe, provider_error_details


@pytest.mark.parametrize("code", [400, 403, 404, 429, 503])
def test_probe_is_one_physical_request_and_redacts_provider_errors(code):
    captured, reserved = [], []

    def handler(request):
        captured.append(request)
        return httpx.Response(code, json={"error": {
            "status": "INVALID_ARGUMENT", "message": "Bad DUMMY_SECRET api_key=DUMMY_SECRET",
            "details": [{"api_key": "DUMMY_SECRET", "reason": "SCHEMA_INVALID"}],
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe(client, "DUMMY_SECRET", reserved.append)
    assert len(captured) == len(reserved) == 1
    assert str(captured[0].url) == ENDPOINT
    assert json.loads(captured[0].content) == {
        "contents": [{"role": "user", "parts": [{"text": "Reply exactly OK."}]}]
    }
    assert result["http_status"] == code
    assert "DUMMY_SECRET" not in json.dumps(result)
    assert result["error"]["details"][0]["reason"] == "SCHEMA_INVALID"


@pytest.mark.parametrize("code,expected", [(400, 1), (403, 1), (404, 1), (429, 3), (503, 3)])
def test_optional_retries_are_bounded_and_only_transient(code, expected):
    reservations, delays = [], []
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(code, json={}))) as client:
        result = probe(client, "DUMMY", reservations.append, max_attempts=3, sleep=delays.append)
    assert result["attempts"] == len(reservations) == expected
    assert delays == ([2, 4] if expected == 3 else [])


def test_probe_ok():
    body = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        assert probe(client, "DUMMY", lambda n: None)["reply_ok"]


def test_sdk_error_details_never_include_request_headers(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "DUMMY_SECRET")
    exc = RuntimeError("request headers Authorization: DUMMY_SECRET")
    exc.status_code = 400
    exc.response = httpx.Response(400, json={"error": {
        "status": "INVALID_ARGUMENT", "message": "Unknown field DUMMY_SECRET",
        "details": [{"reason": "INVALID_FIELD", "token": "DUMMY_SECRET"}],
    }})
    result = provider_error_details(exc)
    assert result["http_status"] == 400
    assert result["error"]["status"] == "INVALID_ARGUMENT"
    assert "DUMMY_SECRET" not in json.dumps(result)
    assert "Authorization" not in json.dumps(result)
