"""Public-only HTTP transport: pin validated DNS answers, including redirects."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .models import web_url


class UnsafeURL(ValueError):
    pass


def public_addresses(host: str) -> list[str]:
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = sorted({entry[4][0] for entry in answers})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeURL("Target does not resolve exclusively to public addresses")
    return addresses


@dataclass
class Page:
    status: int
    text: str
    headers: dict[str, str]
    url: str


class PublicHTTP:
    def __init__(self, timeout: int = 20, max_bytes: int = 2_000_000):
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Page:
        original_headers = headers or {}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for _ in range(6):
                web_url(url)
                parsed = urlsplit(url)
                host = parsed.hostname
                assert host
                addresses = await asyncio.to_thread(public_addresses, host)
                # Connecting to the validated IP prevents a second DNS lookup/rebinding.
                address = addresses[0]
                netloc = f"[{address}]" if ":" in address else address
                if parsed.port:
                    netloc += f":{parsed.port}"
                pinned = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
                request_headers = {
                    "User-Agent": "AI-News-Free-Edition/1.0 (+public RSS digest)",
                    "Host": parsed.netloc,
                    **original_headers,
                }
                async with client.stream(
                    "GET", pinned, headers=request_headers, extensions={"sni_hostname": host}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers["location"])
                        if parsed.scheme == "https" and urlsplit(target).scheme != "https":
                            raise UnsafeURL("HTTPS downgrade rejected")
                        # Conditional headers must not leak across origins.
                        if urlsplit(target).netloc != parsed.netloc:
                            original_headers = {}
                        url = target
                        continue
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ValueError("Response is too large")
                        chunks.append(chunk)
                    content = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    if response.status_code != 304:
                        response.raise_for_status()
                    return Page(response.status_code, content, dict(response.headers), url)
        raise UnsafeURL("Too many redirects")
