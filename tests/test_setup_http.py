import threading
from contextlib import contextmanager

import httpx
import pytest
from test_setup import configure_local


@contextmanager
def local_setup():
    calls = []

    class API:
        repo = "test/no-external-writes"

        def admission(self):
            return True

    token = "test-session-token-only"
    server = configure_local.create_server(
        API(), token, configure=lambda api, fields: calls.append(fields.copy())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin, token, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("origin_kind", ["same", "null", "missing"])
def test_loopback_form_handles_standard_and_sandboxed_origin(origin_kind):
    with local_setup() as (base, token, calls):
        headers = {} if origin_kind == "missing" else {"Origin": base if origin_kind == "same" else "null"}
        with httpx.Client(trust_env=False) as client:
            page = client.get(base, params={"token": token})
            assert page.status_code == 200 and 'name="csrf"' in page.text
            response = client.post(base + "/configure", data={"csrf": token}, headers=headers)
            assert response.status_code == 303
            result = client.get(base + response.headers["location"])
        assert result.status_code == 200 and "配置已加密保存" in result.text
        assert len(calls) == 1


@pytest.mark.parametrize("attack", ["foreign_origin", "invalid_token", "missing_token", "foreign_host"])
def test_rejects_cross_origin_and_missing_authorization(attack):
    with local_setup() as (base, token, calls):
        headers = {"Origin": "null"}
        data = {"csrf": token}
        if attack == "foreign_origin":
            headers["Origin"] = "https://untrusted.example"
        elif attack == "invalid_token":
            data["csrf"] = "incorrect"
        elif attack == "missing_token":
            data.clear()
            data["dummy"] = "test"
        elif attack == "foreign_host":
            headers["Host"] = "untrusted.example"
        response = httpx.post(base + "/configure", data=data, headers=headers, trust_env=False)
        assert response.status_code == 403
        assert calls == []


def test_exclusive_listener_prevents_old_and_new_sessions_sharing_a_port():
    class API:
        repo = "test/no-write"

    first = configure_local.create_server(API(), "first")
    try:
        with pytest.raises(OSError):
            configure_local.create_server(API(), "second", first.server_port)
    finally:
        first.server_close()
