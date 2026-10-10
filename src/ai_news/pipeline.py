from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .ai import AIUnavailable, Enhancer
from .collector import Collector
from .models import Digest, HistoryEvent, IssueState, Settings, Source, Story
from .selection import select_events
from .store import Store

SHANGHAI = ZoneInfo("Asia/Shanghai")


def cutoff_for(issue: str) -> datetime:
    # Digest validation also constrains path input; reject impossible dates before any writes.
    if len(issue) != 13 or issue[-2:] not in {"am", "pm"} or issue[10] != "-":
        raise ValueError("Expected YYYY-MM-DD-am or YYYY-MM-DD-pm")
    day = datetime.strptime(issue[:10], "%Y-%m-%d")
    return day.replace(hour=8 if issue.endswith("am") else 20, tzinfo=SHANGHAI).astimezone(UTC)


def scheduled_issue(cron: str, now: datetime) -> str | None:
    slot = {"0 8 * * *": "am", "0 20 * * *": "pm"}.get(cron)
    if not slot:
        raise ValueError("Unknown schedule")
    local = now.astimezone(SHANGHAI)
    day = local.date()
    issue = f"{day.isoformat()}-{slot}"
    cutoff = cutoff_for(issue)
    if cutoff > now:
        day -= timedelta(days=1)
        issue = f"{day.isoformat()}-{slot}"
        cutoff = cutoff_for(issue)
    return issue if now - cutoff <= timedelta(hours=6) else None


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
    cutoff = cutoff_for(issue)
    if cutoff > now:
        raise ValueError("Cannot generate an unfinished future period")
    existing = store.latest(issue)
    if existing and not regenerate:
        return existing, False
    if settings.paused:
        raise ValueError("publisher_paused")
    previous = [d for d in store.digests() if d.cutoff < cutoff]
    covered = max((d.cutoff for d in previous), default=None)
    # Rebuilding an old issue must not use a later global coverage watermark.
    start = max(covered, cutoff - timedelta(hours=48)) if covered else cutoff - timedelta(hours=24)
    backfill = bool(store.state.last_covered_at and cutoff < store.state.last_covered_at)
    cache = {} if regenerate or backfill else store.state.source_cache
    items, caches = asyncio.run((collector or Collector(settings)).collect(sources, cache, now))
    if not caches or all(cache.status == "error" for cache in caches.values()):
        raise RuntimeError("all_sources_unavailable")
    history = [h for h in store.state.history if h.issue != issue]
    events = select_events(items, sources, start, cutoff, history, settings.dedup_hours)
    events = events[: settings.ai_candidates]
    warnings = ["source_errors"] if any(cache.status == "error" for cache in caches.values()) else []
    stories = []
    mode = "rules"
    if events and settings.ai_enabled:
        try:
            response = (enhancer or Enhancer(settings, store)).enhance(events)
            by_event = {event.id: event for event in events}
            for value in response.stories:
                event = by_event[value.event_id]
                # AI duplicate suggestions are only advisory; rule identity controls suppression.
                caveat = (
                    (
                        "预印本，尚未确认同行评审；论文声称的结果需独立验证。"
                        if event.evidence[0].source_id.startswith("arxiv-")
                        else "研究机构提供的信息；研究结论需独立验证。"
                    )
                    if event.evidence[0].trust == "research"
                    else (
                        "来自官方声明，性能与效果尚需独立验证。"
                        if event.evidence[0].trust == "official"
                        else ""
                    )
                )
                stories.append(
                    Story(
                        id=event.id,
                        title=value.title,
                        summary=value.summary,
                        impact=value.impact,
                        category=value.category,
                        score=round(0.7 * event.rule_score + 0.3 * value.value_score, 2),
                        sources=[e.public_ref() for e in event.evidence if e.id in value.evidence_ids],
                        uncertainty=" ".join(x for x in [caveat, value.uncertainty] if x),
                        entities=event.entities,
                        versions=event.versions,
                        event_type=event.event_type,
                        previous_issue=event.previous_issue,
                        update_note=event.update_note,
                    )
                )
            mode = "ai"
        except AIUnavailable:
            warnings.append("ai_unavailable")
    if mode == "rules":
        for event in events:
            primary = event.evidence[0]
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
                )
            )
    stories = sorted(stories, key=lambda story: (-story.score, story.id))[: settings.max_items]
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
    )
    store.state.issues[digest.key] = IssueState()
    # Backfills must not consume ETags or move coverage for later live periods.
    if store.state.last_covered_at is None or cutoff >= store.state.last_covered_at:
        for value in items:
            if value.published_at > cutoff and value.source_id in caches:
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
