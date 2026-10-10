from datetime import UTC, datetime, timedelta

import pytest

from ai_news.collector import item
from ai_news.models import Digest, Settings, Source, Story


@pytest.fixture
def now():
    return datetime(2026, 10, 10, 0, 30, tzinfo=UTC)


@pytest.fixture
def settings():
    return Settings(base_url="https://reader.github.io/ai-news-auto-publisher", source_retries=0)


@pytest.fixture
def official():
    return Source(
        id="official", name="Official", url="https://example.com/feed.xml", trust="official", group="lab"
    )


@pytest.fixture
def source_item(official, now):
    return item(
        official,
        "https://example.com/model-v2",
        "Example AI model v2.0 released",
        "The company announces a new model v2.0 for testing. Research evidence is still limited.",
        now - timedelta(hours=2),
        now,
    )


@pytest.fixture
def digest(source_item, now):
    story = Story(
        id="event-one",
        title="演示：一项模型更新",
        summary="这是用于验证页面与邮件的演示摘要，不是实际新闻。",
        impact="演示影响分析，实际结论需要来源支持。",
        category="模型",
        score=80,
        sources=[source_item.public_ref()],
        uncertainty="演示数据，请勿当作真实新闻发布。",
    )
    return Digest(
        issue="2026-10-10-am",
        cutoff=now - timedelta(minutes=30),
        created_at=now,
        window_start=now - timedelta(days=1),
        mode="ai",
        stories=[story],
    )
