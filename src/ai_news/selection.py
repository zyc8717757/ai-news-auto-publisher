from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from rapidfuzz.fuzz import ratio

from .models import HistoryEvent, NewsEvent, Source, SourceItem

ALIASES = {
    "openai": r"openai|gpt[- ]?\d|chatgpt",
    "anthropic": r"anthropic|claude|安思罗普",
    "google": r"google|gemini|deepmind|谷歌",
    "meta": r"\bmeta\b|llama|元宇宙",
    "mistral": r"mistral|ministral",
    "deepseek": r"deepseek|深度求索",
    "qwen": r"qwen|通义|千问",
    "nvidia": r"nvidia|英伟达",
    "microsoft": r"microsoft|copilot|微软",
    "huggingface": r"hugging.?face|transformers|diffusers",
    "ollama": r"ollama",
    "vllm": r"\bvllm\b",
    "pytorch": r"pytorch",
    "langchain": r"langchain|langgraph",
    "litellm": r"litellm",
    "typesafe": r"typesafe|\bjev\b",
}
AI_TERMS = re.compile(
    r"\b(ai|llm|gpt|agent|chatbot|diffusion|transformer|inference|robot|machine learning)\b"
    r"|artificial intelligence|人工智能|大模型|智能体|机器学习|生成式|深度学习|chatgpt|claude|gemini|codex|deepseek|qwen|grok|kimi|doubao",
    re.I,
)
PREVIEW = re.compile(
    r"reportedly|rumou?r|sources? say|alleged|might|消息称|报道称|据报|被曝|疑似|或将|即将|将于|预计|内测|灰度测试",
    re.I,
)
MAJOR = re.compile(
    r"model|模型|launch|releas|introduc|发布|推出|融资|fund|rais|安全|privacy|security|policy|政策|订阅|额度|价格|plan|pricing|新功能|更新|agent|智能体|codex|claude|gemini|deepseek",
    re.I,
)


def ordinary_update(value: SourceItem) -> bool:
    if value.source_id.startswith("arxiv-"):
        return True  # Papers alone are not general-interest news evidence.
    if re.search(r"releases/tag/", value.url):
        patch = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)\b", value.title)
        if (
            patch
            and int(patch[3])
            and not re.search(
                r"breaking|security|critical|重大|安全", value.title + " " + value.excerpt, re.I
            )
        ):
            return True
    return False


VERSION = re.compile(
    r"\b(?:v?\d+\.\d+(?:\.\d+)?(?:-[a-z]+)?|(?:gpt|gemini|claude|llama|qwen)[- ]\d[\w.-]*)\b", re.I
)


def normalized(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower()).strip()


def simhash(text: str) -> int:
    clean = normalized(text)
    tokens = re.findall(r"[a-z0-9_.-]+|[\u4e00-\u9fff]{1,2}", clean)
    weights = [0] * 64
    for token in set(tokens):
        value = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    return sum(1 << bit for bit, value in enumerate(weights) if value > 0)


def features(title: str) -> tuple[list[str], list[str], str]:
    entities = sorted(key for key, pattern in ALIASES.items() if re.search(pattern, title, re.I))
    versions = sorted({value.lower().replace(" ", "-") for value in VERSION.findall(title)})
    if re.search(r"fund|rais|融资|收购|acqui", title, re.I):
        kind = "business"
    elif re.search(r"regulat|policy|law|监管|政策|法案", title, re.I):
        kind = "policy"
    elif re.search(r"paper|research|arxiv|论文|研究", title, re.I):
        kind = "research"
    elif re.search(r"releas|launch|introduc|发布|推出|上线", title, re.I):
        kind = "release"
    else:
        kind = "announcement"
    return entities, versions, kind


def same_event(a: str, b: str) -> bool:
    ae, av, ak = features(a)
    be, bv, bk = features(b)
    if av != bv or (ae and be and ae != be) or ak != bk:
        return False
    if ratio(normalized(a), normalized(b)) >= 86:
        return True
    # A model/version plus a release action is useful across languages; a company name alone is not.
    return bool(ae and ae == be and av and av == bv and ak == "release")


def independent_groups(items: list[SourceItem]) -> int:
    representatives: list[SourceItem] = []
    for value in items:
        if value.trust == "community" or any(value.group == x.group for x in representatives):
            continue
        syndicated = any(
            normalized(value.title) == normalized(x.title)
            or (
                len(value.excerpt) >= 80
                and len(x.excerpt) >= 80
                and (simhash(value.excerpt) ^ simhash(x.excerpt)).bit_count() <= 3
            )
            for x in representatives
        )
        if not syndicated:
            representatives.append(value)
    return len(representatives)


def select_events(
    items: list[SourceItem],
    sources: list[Source],
    start: datetime,
    cutoff: datetime,
    history: list[HistoryEvent],
    dedup_hours: int = 48,
) -> list[NewsEvent]:
    config = {source.id: source for source in sources}
    rank = {"official": 0, "research": 1, "media": 2, "community": 3}
    unique: dict[str, SourceItem] = {}
    for value in items:
        if value.published_at is None or not start < value.published_at <= cutoff:
            continue
        if ordinary_update(value):
            continue
        source = config.get(value.source_id)
        resolved = value.source_id.startswith(("primary-", "media-", "hf-"))
        if (not source and not resolved) or (
            (not source or not source.ai_only)
            and not AI_TERMS.search(value.title + " " + value.excerpt)
            and not features(value.title)[0]
        ):
            continue
        if value.url not in unique or rank[value.trust] < rank[unique[value.url].trust]:
            unique[value.url] = value
    clusters: list[list[SourceItem]] = []
    for value in sorted(unique.values(), key=lambda x: (rank[x.trust], -x.published_at.timestamp())):
        matching = next((cluster for cluster in clusters if same_event(value.title, cluster[0].title)), None)
        if matching is None:
            clusters.append([value, *value.supporting])
        else:
            matching.append(value)
            matching.extend(value.supporting)
    events = []
    recent = [h for h in history if cutoff - timedelta(hours=dedup_hours) <= h.cutoff <= cutoff]
    for cluster in clusters:
        primary = cluster[0]
        trusted = any(value.trust in {"official", "research"} for value in cluster)
        groups = independent_groups(cluster)
        if not trusted and not any(x.trust == "media" for x in cluster):
            continue
        entities, versions, kind = features(primary.title)
        prior = next(
            (
                h
                for h in recent
                if any(x.url in h.urls for x in cluster) or same_event(primary.title, h.title)
            ),
            None,
        )
        if prior:
            continue
        # Only a changed explicit version/action establishes an automatic follow-up.
        previous = next(
            (
                h
                for h in sorted(history, key=lambda h: h.cutoff, reverse=True)
                if entities
                and h.entities == entities
                and versions
                and h.versions
                and h.versions != versions
                and h.event_type == kind
                and cutoff - timedelta(hours=dedup_hours) <= h.cutoff < cutoff
            ),
            None,
        )
        age = max(0, (cutoff - max(x.published_at for x in cluster if x.published_at is not None)).total_seconds() / 3600)
        impact = (
            30 if re.search(r"model|模型|regulat|政策|fund|融资|security|安全", primary.title, re.I) else 18
        )
        score = 30 if primary.trust == "official" else 25 if primary.trust == "research" else 15
        score += impact + min(15, max(0, groups - 1) * 7.5) + max(0, 15 * (1 - age / 48))
        score += 10 if primary.trust in {"official", "research"} else 5
        identity = hashlib.sha256("|".join(sorted(x.url for x in cluster)).encode()).hexdigest()[:20]
        events.append(
            NewsEvent(
                id=identity,
                evidence=cluster,
                rule_score=min(100, score),
                entities=entities,
                versions=versions,
                event_type=kind,
                observation=bool(PREVIEW.search(primary.title)) or (not trusted and groups < 2),
                uncertain=bool(PREVIEW.search(primary.title)),
                backfill=primary.published_at <= cutoff - timedelta(hours=24),
                previous_issue=previous.issue if previous else None,
                update_note="与上一期相比，来源公布了新的明确版本。" if previous else "",
            )
        )
    return sorted(events, key=lambda event: (-event.rule_score, event.id))
