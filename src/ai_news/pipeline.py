from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .ai import AIUnavailable, Enhancer
from .collector import Collector
from .models import (
    AIResponse,
    Digest,
    HistoryEvent,
    IssueState,
    NewsEvent,
    Settings,
    Source,
    SourceItem,
    Story,
)
from .selection import select_events
from .store import Store

SHANGHAI = ZoneInfo("Asia/Shanghai")


def published_digests(store: Store) -> list[Digest]:
    """Pending validation revisions must not become 'already sent' news.

    An ambiguous SMTP outcome is conservatively treated as potentially sent,
    matching the explicit-resend policy; a never-deployed pending revision isn't.
    """
    result = []
    for digest in store.digests():
        state = store.state.issues.get(digest.key)
        if state and (state.deployment == "ready" or state.email in {"sent", "unknown", "sending"}):
            result.append(digest)
    return result


def cron_for(value: str) -> str:
    hour, minute = map(int, value.split(":"))
    return f"{minute} {hour} * * *"


def cutoff_for(issue: str, morning_time: str = "08:00", evening_time: str = "20:00") -> datetime:
    # Digest validation also constrains path input; reject impossible dates before any writes.
    if len(issue) != 13 or issue[-2:] not in {"am", "pm"} or issue[10] != "-":
        raise ValueError("Expected YYYY-MM-DD-am or YYYY-MM-DD-pm")
    day = datetime.strptime(issue[:10], "%Y-%m-%d")
    hour, minute = map(int, (morning_time if issue.endswith("am") else evening_time).split(":"))
    return day.replace(hour=hour, minute=minute, tzinfo=SHANGHAI).astimezone(UTC)


def scheduled_issue(
    cron: str, now: datetime, morning_time: str = "08:00", evening_time: str = "20:00"
) -> str | None:
    if cron == "17 * * * *":
        local = now.astimezone(SHANGHAI)
        candidates = [
            f"{(local.date() - timedelta(days=days)).isoformat()}-{slot}"
            for days in (0, 1)
            for slot in ("am", "pm")
        ]
        due = [i for i in candidates if cutoff_for(i, morning_time, evening_time) <= now]
        return max(due, key=lambda i: cutoff_for(i, morning_time, evening_time))
    slot = {cron_for(morning_time): "am", cron_for(evening_time): "pm"}.get(cron)
    if not slot:
        raise ValueError("Unknown schedule")
    local = now.astimezone(SHANGHAI)
    day = local.date()
    issue = f"{day.isoformat()}-{slot}"
    cutoff = cutoff_for(issue, morning_time, evening_time)
    if cutoff > now:
        day -= timedelta(days=1)
        issue = f"{day.isoformat()}-{slot}"
        cutoff = cutoff_for(issue, morning_time, evening_time)
    return issue if now - cutoff <= timedelta(hours=6) else None


def ai_stories(response, events, decisions=None) -> list[Story]:
    by_event = {event.id: event for event in events}
    stories = []
    for value in response.stories:
        if value.duplicate_of:
            continue
        event = by_event[value.event_id]
        decision = decisions.get(event.id) if decisions else None
        if event.evidence[0].trust == "research":
            caveat = (
                "预印本，尚未确认同行评审；论文声称的结果需独立验证。"
                if event.evidence[0].source_id.startswith("arxiv-")
                else "研究机构提供的信息；研究结论需独立验证。"
            )
        else:
            caveat = "来自官方声明，性能与效果尚需独立验证。" if event.evidence[0].trust == "official" else ""
        stories.append(
            Story(
                id=event.id,
                title=value.title,
                summary=value.summary,
                impact=value.impact,
                category=decision.category if decision else value.category,
                score=round(0.7 * event.rule_score + 0.3 * value.value_score, 2),
                sources=[e.public_ref() for e in event.evidence if e.id in value.evidence_ids],
                uncertainty=" ".join(
                    x
                    for x in [
                        caveat,
                        value.uncertainty,
                        "未经官方确认，仅供前瞻观察。" if decision and decision.observation else "",
                        "来源仅提供日期，时区及准确发布时间未知。"
                        if any(e.time_precision == "day" for e in event.evidence)
                        else "",
                    ]
                    if x
                ),
                section="observation" if decision and decision.observation else "news",
                selection_reason=decision.reason if decision else "",
                backfill=event.backfill,
                entities=event.entities,
                versions=event.versions,
                event_type=event.event_type,
                previous_issue=event.previous_issue,
                update_note=event.update_note,
            )
        )
    return stories


def enhance_frozen(
    settings: Settings,
    store: Store,
    issue: str,
    enhancer=None,
    now=None,
    test_extra_limit: int = 0,
    base_key: str | None = None,
) -> tuple[Digest, bool]:
    """Upgrade frozen source information without recollecting, moving coverage or clearing budgets."""
    latest = store.latest(issue)
    existing = (
        next((d for d in store.digests() if d.issue == issue and d.key == base_key), None)
        if base_key
        else latest
    )
    if not existing or not existing.stories:
        raise ValueError("A nonempty frozen digest is required")
    if (
        base_key
        and latest
        and latest.mode == "ai"
        and latest.revision > existing.revision
        and latest.cutoff == existing.cutoff
        and {s.id for s in latest.stories} == {s.id for s in existing.stories}
    ):
        return latest, False
    if existing.mode == "ai" and not base_key:
        return existing, False
    if settings.paused:
        raise ValueError("publisher_paused")
    now = now or datetime.now(UTC)
    events = [
        NewsEvent(
            id=s.id,
            rule_score=s.score,
            entities=s.entities,
            versions=s.versions,
            event_type=s.event_type,
            previous_issue=s.previous_issue,
            update_note=s.update_note,
            evidence=[
                SourceItem(**ref.model_dump(), collected_at=now, excerpt=s.summary if index == 0 else "")
                for index, ref in enumerate(s.sources)
            ],
        )
        for s in existing.stories
    ]
    processor = enhancer or Enhancer(settings, store, issue=issue, test_extra_limit=test_extra_limit)
    response = processor.enhance(events)
    if enhancer is None:
        from .editorial import EditorialEnhancer

        response = AIResponse(
            stories=EditorialEnhancer(
                settings, store, client=processor.client, budget=processor.budget
            ).review_summaries(processor.client, response.stories, events)
        )
    if base_key and {s.event_id for s in response.stories if not s.duplicate_of} != {e.id for e in events}:
        raise AIUnavailable("ai_frozen_coverage_incomplete")
    stories = sorted(ai_stories(response, events), key=lambda story: (-story.score, story.id))
    digest = existing.model_copy(
        update={
            "revision": latest.revision + 1,
            "created_at": now,
            "mode": "ai",
            "stories": stories,
            "warnings": [w for w in existing.warnings if w != "ai_unavailable"],
        }
    )
    store.state.issues[digest.key] = IssueState()
    store.save_digest(digest)
    logging.getLogger(__name__).warning(
        "AI model accepted: %s", getattr(processor, "used_model", settings.model)
    )
    return digest, True


def generate(
    settings: Settings,
    sources: list[Source],
    store: Store,
    issue: str,
    regenerate: bool = False,
    now: datetime | None = None,
    collector=None,
    enhancer=None,
) -> tuple[Digest, bool]:
    now = now or datetime.now(UTC)
    cutoff = cutoff_for(issue, settings.morning_time, settings.evening_time)
    if cutoff > now:
        raise ValueError("Cannot generate an unfinished future period")
    existing = store.latest(issue)
    if existing and not regenerate:
        return existing, False
    if settings.paused:
        raise ValueError("publisher_paused")
    # Both editions revisit 24h plus the 24–48h important-backfill pool. History is
    # authoritative for already-published events; a coverage watermark is not rejection.
    start = cutoff - timedelta(hours=48)
    backfill = bool(store.state.last_covered_at and cutoff < store.state.last_covered_at)
    cache = {} if regenerate or backfill else store.state.source_cache
    items, caches = asyncio.run(
        (collector or Collector(settings, cutoff=cutoff)).collect(sources, cache, now)
    )
    if not caches or all(cache.status == "error" for cache in caches.values()):
        raise RuntimeError("all_sources_unavailable")
    public = published_digests(store)
    known_issues = {d.issue for d in store.digests()}
    history = [h for h in store.state.history if h.issue not in known_issues and h.issue != issue]
    history.extend(
        HistoryEvent(
            id=s.id,
            issue=d.issue,
            cutoff=d.cutoff,
            title=s.sources[0].title,
            urls=[r.url for r in s.sources],
            entities=s.entities,
            versions=s.versions,
            event_type=s.event_type,
        )
        for d in public
        if d.issue != issue
        for s in d.stories
    )
    events = select_events(items, sources, start, cutoff, history, settings.dedup_hours)
    events = events[: settings.ai_candidates]
    warnings = ["source_errors"] if any(cache.status == "error" for cache in caches.values()) else []
    stories = []
    mode = "rules"
    if events and settings.ai_enabled:
        try:
            if settings.editorial_enabled and enhancer is None:
                from .editorial import EditorialEnhancer

                latest = {}
                for d in public:
                    if (
                        d.issue != issue
                        and d.cutoff >= cutoff - timedelta(days=30)
                        and d.editorial_version == 2
                    ):
                        if d.issue not in latest or d.revision > latest[d.issue].revision:
                            latest[d.issue] = d
                counts = Counter(s.category for d in latest.values() for s in d.news)
                response, decisions, selected = EditorialEnhancer(settings, store, issue=issue).edit(
                    events, cutoff, dict(counts)
                )
                stories = ai_stories(response, selected, decisions)
            else:
                processor = enhancer or Enhancer(settings, store, issue=issue)
                response = processor.enhance(events)
                if enhancer is None:
                    from .editorial import EditorialEnhancer

                    response = AIResponse(
                        stories=EditorialEnhancer(
                            settings, store, client=processor.client, budget=processor.budget
                        ).review_summaries(processor.client, response.stories, events)
                    )
                stories = ai_stories(response, events)
            mode = "ai"
        except AIUnavailable as exc:
            logging.getLogger(__name__).warning("AI degraded: %s", exc)
            warnings.append("ai_unavailable")
    if mode == "rules":
        observers = 0
        for event in events:
            primary = event.evidence[0]
            if settings.editorial_enabled:
                if (
                    primary.trust == "research"
                    or event.rule_score < settings.importance_threshold
                    or event.backfill
                    and event.rule_score < 80
                ):
                    continue
                if event.observation and observers >= settings.max_observations:
                    continue
                observers += event.observation
            stories.append(
                Story(
                    id=event.id,
                    title=primary.title,
                    summary=primary.excerpt[:500] or "来源未提供可用摘要，请阅读原文。",
                    impact="",
                    category="来源原文",
                    score=round(event.rule_score, 2),
                    sources=[e.public_ref() for e in event.evidence],
                    uncertainty="AI 增强未完成；以下为来源信息，不代表已经完成事实复核。",
                    entities=event.entities,
                    versions=event.versions,
                    event_type=event.event_type,
                    previous_issue=event.previous_issue,
                    update_note=event.update_note,
                    section="observation" if event.observation else "news",
                    backfill=event.backfill,
                )
            )
    # Preserve AI's global importance order; observations never displace formal Top3.
    if mode == "rules" or not settings.editorial_enabled or enhancer is not None:
        stories.sort(key=lambda story: (-story.score, story.id))
    stories.sort(key=lambda story: story.section == "observation")
    stories = stories[: settings.max_items]
    if not stories:
        warnings.append("no_new_items")
    digest = Digest(
        issue=issue,
        revision=existing.revision + 1 if existing else 1,
        created_at=now,
        cutoff=cutoff,
        window_start=start,
        mode=mode,
        stories=stories,
        warnings=warnings,
        editorial_version=2 if settings.editorial_enabled else 1,
    )
    store.state.issues[digest.key] = IssueState()
    # Backfills must not consume ETags or move coverage for later live periods.
    if store.state.last_covered_at is None or cutoff >= store.state.last_covered_at:
        for value in items:
            if value.published_at is not None and value.published_at > cutoff and value.source_id in caches:
                caches[value.source_id].etag = ""
                caches[value.source_id].modified = ""
        store.state.source_cache.update(caches)
        store.state.last_covered_at = cutoff
    store.state.history = [
        h
        for h in store.state.history
        if h.cutoff >= now - timedelta(days=settings.history_days) and h.issue != issue
    ]
    store.state.history.extend(
        HistoryEvent(
            id=s.id,
            issue=issue,
            cutoff=cutoff,
            title=s.sources[0].title,
            urls=[ref.url for ref in s.sources],
            entities=s.entities,
            versions=s.versions,
            event_type=s.event_type,
        )
        for s in stories
    )
    store.save_digest(digest)
    return digest, True
