import asyncio
import socket

import httpx
import pytest

from ai_news.network import PublicHTTP, UnsafeURL, public_addresses


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1"])
def test_private_dns_rejected(address, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (address, 0))])
    with pytest.raises(UnsafeURL):
        public_addresses("untrusted.example")


def test_public_ipv4_is_preferred_but_mixed_private_answers_still_fail(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, None, ("2606:4700:4700::1111", 0)),
            (None, None, None, None, ("34.1.1.1", 0)),
        ],
    )
    assert public_addresses("public.example")[0] == "34.1.1.1"
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("::1", 0)), (None, None, None, None, ("34.1.1.1", 0))],
    )
    with pytest.raises(UnsafeURL):
        public_addresses("mixed.example")


def test_pinned_host_and_redirect_revalidation(monkeypatch):
    requests = []

    def resolve(host):
        if host == "private.example":
            raise UnsafeURL("private")
        return ["93.184.216.34"]

    monkeypatch.setattr("ai_news.network.public_addresses", resolve)
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://private.example/"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(UnsafeURL):
        asyncio.run(PublicHTTP().get("https://public.example/feed"))
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"


def test_oversized_response_rejected(monkeypatch):
    monkeypatch.setattr("ai_news.network.public_addresses", lambda host: ["93.184.216.34"])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="x" * 100)), **kw
        ),
    )
    with pytest.raises(ValueError, match="large"):
        asyncio.run(PublicHTTP(max_bytes=10).get("https://public.example/rss"))
