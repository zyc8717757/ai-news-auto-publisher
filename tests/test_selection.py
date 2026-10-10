from datetime import timedelta

from ai_news.models import HistoryEvent, Source
from ai_news.selection import independent_groups, same_event, select_events, simhash


def test_versions_and_cross_language_identity():
    assert same_event("OpenAI GPT-5.1 released", "OpenAI 发布 GPT-5.1")
    assert not same_event("OpenAI GPT-5.1 released", "OpenAI GPT-5.2 released")
    assert not same_event("Google launches music tool", "Google launches coding tool")
    assert simhash("AI model release") == simhash("AI model release")


def test_official_gate_and_history(source_item, official, now):
    values = select_events([source_item], [official], now - timedelta(days=1), now, [])
    assert len(values) == 1
    history = [
        HistoryEvent(
            id=values[0].id,
            issue="2026-10-09-pm",
            cutoff=now - timedelta(hours=12),
            title=source_item.title,
            urls=[source_item.url],
            entities=[],
            versions=[],
            event_type="release",
        )
    ]
    assert not select_events([source_item], [official], now - timedelta(days=1), now, history)


def test_community_never_passes_alone(source_item, official, now):
    source = official.model_copy(update={"trust": "community"})
    value = source_item.model_copy(update={"trust": "community"})
    assert not select_events([value], [source], now - timedelta(days=1), now, [])


def test_syndication_not_independent(source_item):
    first = source_item.model_copy(update={"trust": "media", "group": "media-a"})
    duplicate = first.model_copy(
        update={"id": "second", "url": "https://second.example/news", "group": "media-b"}
    )
    assert independent_groups([first, duplicate]) == 1


def test_two_independent_media_can_pass(source_item, now):
    first = source_item.model_copy(update={"trust": "media", "group": "a", "source_id": "a"})
    second = first.model_copy(
        update={
            "id": "second",
            "url": "https://example.net/story",
            "group": "b",
            "source_id": "b",
            "title": "Example AI model v2.0 is released",
            "excerpt": "Independent testing of the new release.",
        }
    )
    sources = [
        Source(id=x, name=x, url=f"https://{x}.example/rss", trust="media", group=x) for x in ("a", "b")
    ]
    assert len(select_events([first, second], sources, now - timedelta(days=1), now, [])) == 1


def test_material_new_version_links_prior(source_item, official, now):
    old = HistoryEvent(
        id="old",
        issue="2026-10-09-pm",
        cutoff=now - timedelta(hours=12),
        title="OpenAI GPT-5.1 released",
        urls=["https://example.com/old"],
        entities=["openai"],
        versions=["gpt-5.1"],
        event_type="release",
    )
    updated = source_item.model_copy(update={"title": "OpenAI GPT-5.2 released"})
    events = select_events([updated], [official], now - timedelta(days=1), now, [old])
    assert len(events) == 1 and events[0].previous_issue == "2026-10-09-pm"
