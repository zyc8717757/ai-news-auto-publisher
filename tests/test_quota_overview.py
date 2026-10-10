import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from ai_news.ai import AIUnavailable, Budget, quota_used
from ai_news.models import QuotaSnapshot
from ai_news.overview import overview_png
from ai_news.store import Store


def test_quota_reconciliation_preserves_failed_attempts_and_issue_caps(settings, tmp_path):
    now = datetime(2026, 10, 10, 13, tzinfo=UTC)
    settings.model = "gemini-3.5-flash-lite"
    store = Store(tmp_path)
    store.state.ai_attempts = [now - timedelta(minutes=5)] * 10
    store.state.ai_quota_snapshot = QuotaSnapshot(observed_at=now, model=settings.model, used=2)
    store.state.ai_issue_attempts["2026-10-10-am"] = 5
    assert quota_used(store.state, now, settings.model) == 2
    with pytest.raises(AIUnavailable, match="issue_budget"):
        Budget(settings, store, now=lambda: now, issue="2026-10-10-am").reserve(100)
    Budget(settings, store, now=lambda: now + timedelta(seconds=1), issue="2026-10-10-pm").reserve(100)
    assert len(store.state.ai_attempts) == 11
    assert quota_used(store.state, now + timedelta(seconds=2), settings.model) == 3
    restored = Store(tmp_path)
    assert restored.state.ai_quota_snapshot.used == 2
    assert quota_used(restored.state, now + timedelta(days=1), settings.model) == 0


def test_500_daily_cap_blocks_even_after_manual_reconciliation(settings, tmp_path):
    now = datetime(2026, 10, 10, 13, tzinfo=UTC)
    settings.model = "gemini-3.5-flash-lite"
    store = Store(tmp_path)
    store.state.ai_quota_snapshot = QuotaSnapshot(observed_at=now, model=settings.model, used=500)
    with pytest.raises(AIUnavailable, match="budget_exhausted"):
        Budget(settings, store, now=lambda: now).reserve(100)
    assert not store.state.ai_attempts


def test_overview_png_is_readable_size_and_public_only(digest):
    image = Image.open(io.BytesIO(overview_png(digest)))
    assert image.width == 1600 and image.height >= 620
    assert image.mode == "RGB"
