"""Small, secret-safe provider diagnostics; never log requests or headers."""
from __future__ import annotations

import os
import re
import time
from urllib.parse import quote

import httpx

MODEL = "gemini-3.5-flash-lite"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def provider_error_details(exc):
    """Extract only provider error fields, never exception/request reprs."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    body = getattr(exc, "response_json", None)
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.json()
        except (ValueError, AttributeError):
            pass
    if not isinstance(body, dict):
        body = getattr(exc, "body", {})
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    secrets = [v for k, v in os.environ.items() if re.search(
        r"KEY|TOKEN|PASSWORD|SECRET|SMTP|EMAIL", k, re.I
    )]
    return {"http_status": code, "error": redact({
        "status": error.get("status", error.get("code")),
        "message": error.get("message"), "details": error.get("details", []),
    }, secrets)}


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if re.search(
                r"key|token|authorization|password|credential|secret|email", str(k), re.I
            ) else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]").replace(quote(secret, safe=""), "[REDACTED]")
    value = re.sub(r"AIza[\w-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)[\w.~-]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|token|authorization|password)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value[:6000]


def probe(client, key, reserve, max_attempts=1, sleep=time.sleep):
    """Default is exactly one physical HTTP call. Only 429/503 permit bounded retries."""
    if not 1 <= max_attempts <= 3:
        raise ValueError("Probe attempts must be within 1..3")
    secrets = [key] + [v for k, v in os.environ.items() if re.search(
        r"KEY|TOKEN|PASSWORD|SECRET|SMTP|EMAIL", k, re.I
    )]
    for attempt in range(max_attempts):
        reserve(256)
        try:
            response = client.post(
                ENDPOINT, headers={"x-goog-api-key": key},
                json={"contents": [{"role": "user", "parts": [{"text": "Reply exactly OK."}]}]},
            )
        except httpx.HTTPError:
            return {"http_status": None, "error": {"status": "TRANSPORT_ERROR", "message": "Transport failed; request details suppressed", "details": []}}
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", {})
        result = {"http_status": response.status_code, "attempts": attempt + 1,
                  "error": redact({"status": error.get("status", error.get("code")),
                                   "message": error.get("message"), "details": error.get("details", [])}, secrets)}
        if response.is_success:
            text = "".join(p.get("text", "") for c in body.get("candidates", [])
                           for p in c.get("content", {}).get("parts", []) if not p.get("thought"))
            result["reply_ok"] = text.strip() == "OK"
        if response.status_code not in {429, 503} or attempt + 1 == max_attempts:
            return result
        sleep(min(30, 2 ** (attempt + 1)))
