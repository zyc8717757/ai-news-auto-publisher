"""Owner-triggered one-request probe; no news processing, Pages or email."""
import importlib.metadata
import json
import os
from pathlib import Path

import httpx

from ai_news.ai import Budget
from ai_news.config import load_config
from ai_news.diagnostics import ENDPOINT, MODEL, probe
from ai_news.store import Store


def main():
    settings, _ = load_config(Path("config"))
    key = os.getenv("GEMINI_API_KEY")
    print(json.dumps({"model": MODEL, "production_model": settings.model,
                      "production_api": "v1beta/models/{model}:generateContent", "probe_endpoint": ENDPOINT,
                      "sdk_version": importlib.metadata.version("google-genai"),
                      "key_present": bool(key), "transport": "REST; no SDK retries"}))
    if not key or os.getenv("AI_FREE_TIER_CONFIRMED") != "true":
        raise RuntimeError("Key missing or free tier unconfirmed")
    budget = Budget(settings, Store(Path(".runtime/data"), remote=True),
                    issue=os.environ["AI_TEST_AUTHORIZED_ISSUE"],
                    test_extra_limit=int(os.environ["AI_TEST_AUTHORIZED_TOTAL"]))
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        result = probe(client, key, budget.reserve)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Probe stopped; no exception payload logged") from None
