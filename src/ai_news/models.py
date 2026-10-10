from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def web_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTP(S) URLs are allowed")
    if any(ord(c) < 33 for c in value):
        raise ValueError("URL contains whitespace or control characters")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Non-web ports are not allowed")
    return value


class Settings(StrictModel):
    paused: bool = False
    ai_enabled: bool = False
    email_enabled: bool = False
    base_url: str
    model: Literal["gemini-2.5-flash-lite"] = "gemini-2.5-flash-lite"
    backup_model: Literal["gemini-2.5-flash"] = "gemini-2.5-flash"
    morning_time: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    evening_time: str = Field(default="20:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    max_items: int = Field(default=15, ge=1, le=15)
    max_candidates: int = Field(default=300, ge=1, le=300)
    ai_candidates: int = Field(default=30, ge=1, le=30)
    source_limit: int = Field(default=50, ge=1, le=50)
    concurrency: int = Field(default=5, ge=1, le=5)
    timeout_seconds: int = Field(default=20, ge=1, le=60)
    source_retries: int = Field(default=2, ge=0, le=2)
    ai_run_limit: int = Field(default=5, ge=1, le=5)
    ai_day_limit: int = Field(default=10, ge=1, le=10)
    dedup_hours: int = Field(default=48, ge=1, le=48)
    history_days: int = Field(default=90, ge=2, le=365)

    _base = field_validator("base_url")(web_url)

    @model_validator(mode="after")
    def ordered_schedule(self):
        if self.morning_time >= self.evening_time:
            raise ValueError("Morning time must be earlier than evening time on the same Beijing date")
        return self


class Source(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    url: str
    kind: Literal["rss", "github", "html"] = "rss"
    language: str = "en"
    trust: Literal["official", "research", "media", "community"]
    group: str
    enabled: bool = True
    ai_only: bool = True
    # HTML adapters may inspect only explicitly configured article link patterns.
    link_pattern: str | None = None

    _url = field_validator("url")(web_url)

    @field_validator("link_pattern")
    @classmethod
    def valid_pattern(cls, value: str | None) -> str | None:
        if value:
            re.compile(value)
        return value


class SourceRef(StrictModel):
    id: str
    source_id: str
    source_name: str
    group: str
    trust: Literal["official", "research", "media", "community"]
    url: str
    title: str = Field(max_length=400)
    published_at: datetime

    _url = field_validator("url")(web_url)

    @field_validator("published_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamp must include timezone")
        return value


class SourceItem(SourceRef):
    collected_at: datetime
    excerpt: str = Field(default="", max_length=1600)
    event_at: datetime | None = None

    def public_ref(self) -> SourceRef:
        return SourceRef.model_validate(self.model_dump(include=set(SourceRef.model_fields)))


class NewsEvent(StrictModel):
    id: str
    evidence: list[SourceItem]
    rule_score: float = Field(default=0, ge=0, le=100)
    entities: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    event_type: str = "announcement"
    previous_issue: str | None = None
    update_note: str = ""


class AIStory(StrictModel):
    event_id: str
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=500)
    impact: str = Field(min_length=5, max_length=240)
    category: Literal["模型", "产品", "开源", "科研", "商业", "政策"]
    value_score: float = Field(ge=0, le=100)
    evidence_ids: list[str] = Field(min_length=1)
    uncertainty: str = Field(default="", max_length=240)
    duplicate_of: str | None = None


class AIResponse(StrictModel):
    stories: list[AIStory] = Field(max_length=30)


class Story(StrictModel):
    id: str
    title: str = Field(max_length=400)
    summary: str = Field(max_length=600)
    impact: str = Field(max_length=300)
    category: str
    score: float = Field(ge=0, le=100)
    sources: list[SourceRef] = Field(min_length=1)
    uncertainty: str = ""
    entities: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    event_type: str = "announcement"
    previous_issue: str | None = None
    update_note: str = ""


class Digest(StrictModel):
    schema_version: Literal[1] = 1
    issue: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}-(am|pm)$")
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    cutoff: datetime
    window_start: datetime
    mode: Literal["ai", "rules"]
    stories: list[Story] = Field(max_length=15)
    warnings: list[str] = Field(default_factory=list)
    template_version: Literal[1] = 1

    @property
    def key(self) -> str:
        return f"{self.issue}-r{self.revision}"

    @property
    def label(self) -> str:
        return f"{self.issue[:10]} AI {'早报' if self.issue.endswith('am') else '晚报'}"


class HistoryEvent(StrictModel):
    id: str
    issue: str
    cutoff: datetime
    title: str
    urls: list[str]
    entities: list[str]
    versions: list[str]
    event_type: str


class SourceCache(StrictModel):
    etag: str = ""
    modified: str = ""
    status: Literal["ok", "unchanged", "error"] = "ok"
    error_code: str = Field(default="", pattern=r"^[a-z0-9_]*$")


class IssueState(StrictModel):
    deployment: Literal["pending", "ready", "failed"] = "pending"
    email: Literal["pending", "sending", "sent", "failed", "unknown", "disabled"] = "pending"
    email_attempts: int = 0


class AIRequestAudit(StrictModel):
    at: datetime
    issue: str | None = None
    model: Literal["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    stage: Literal["provider_error", "output_rejected", "accepted", "budget_blocked"]
    reason: str = Field(default="", pattern=r"^[a-z0-9_]*$")
    extra_test: bool = False


class RunState(StrictModel):
    schema_version: Literal[1] = 1
    last_covered_at: datetime | None = None
    ai_attempts: list[datetime] = Field(default_factory=list)
    ai_issue_attempts: dict[str, int] = Field(default_factory=dict)
    ai_test_attempts: dict[str, int] = Field(default_factory=dict)
    ai_audit: list[AIRequestAudit] = Field(default_factory=list)
    ai_reserved_tokens: list[tuple[datetime, int]] = Field(default_factory=list)
    source_cache: dict[str, SourceCache] = Field(default_factory=dict)
    issues: dict[str, IssueState] = Field(default_factory=dict)
    history: list[HistoryEvent] = Field(default_factory=list)


class DraftBundle(StrictModel):
    short: str
    long: str
    short_weight: int
    long_weight: int
