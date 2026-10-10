"""Recheck an explicit public correction against originals; never deploy or email."""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from google import genai
from google.genai import types

from ai_news.ai import validate_response
from ai_news.collector import article_metadata, plain
from ai_news.config import load_config
from ai_news.editorial import EditorialEnhancer, Reviews, evidence_payload
from ai_news.models import AIResponse, AIStory, Digest, IssueState, NewsEvent, SourceItem, Story
from ai_news.network import PublicHTTP
from ai_news.store import Store, atomic_write


def corrected(base, spec):
    if spec["base_key"] != base.key or not set(spec["updates"]) <= {s.id for s in base.stories}:
        raise ValueError("Correction does not match frozen base")
    allowed = {"title", "summary", "impact", "uncertainty", "selection_reason"}
    stories = []
    for story in base.stories:
        update = spec["updates"].get(story.id, {})
        if not set(update) <= allowed:
            raise ValueError("Only public editorial text can be corrected")
        stories.append(Story.model_validate({**story.model_dump(), **update}))
    return stories


async def originals(stories):
    http = PublicHTTP(20)
    events = []
    review_texts = {}
    for story in stories:
        evidence = []
        for ref in story.sources:
            try:
                page = await http.get(ref.url)
            except Exception:
                print(json.dumps({"status": "correction_source_unavailable", "evidence_id": ref.id}))
                raise
            title, excerpt, *_ = article_metadata(page.text)
            body = page.text
            # Discourse's HTML shell often omits the actual announcement. Use
            # its public first-post JSON, preserving the frozen evidence URL/ID.
            if ref.source_id == "primary-openai-community" or "community.openai.com/t/" in ref.url:
                topic = await http.get(ref.url.rstrip("/") + ".json")
                body = json.loads(topic.text)["post_stream"]["posts"][0]["cooked"]
                excerpt = plain(body, 1600)
            soup = BeautifulSoup(body, "html.parser")
            for node in soup(["script", "style", "nav", "footer", "header"]):
                node.decompose()
            article = soup.find("article") or soup.find("main") or soup
            # Full evidence is transient review input only, never public state,
            # logs, artifacts or copied original-text archives.
            review_texts[ref.id] = plain(str(article), 12000)
            if not title or not excerpt:
                raise ValueError("Original evidence unavailable")
            evidence.append(
                SourceItem(
                    **{**ref.model_dump(), "title": title[:400]},
                    excerpt=excerpt,
                    collected_at=datetime.now(UTC),
                )
            )
        events.append(NewsEvent(id=story.id, evidence=evidence))
    return events, review_texts


def main():
    issue = os.environ["REQUESTED_ISSUE"]
    settings, _ = load_config(Path("config"))
    if "AI_ENABLED" in os.environ:
        settings.ai_enabled = os.environ["AI_ENABLED"] == "true"
    if os.getenv("PUBLISHER_PAUSED") == "true":
        raise ValueError("Publisher paused")
    if os.getenv("AI_FREE_TIER_CONFIRMED") != "true" or not settings.ai_enabled:
        raise ValueError("Free AI project confirmation required")
    store = Store(Path(".runtime/data"), remote=True)
    spec = json.loads((Path("config/corrections") / f"{issue}.json").read_text("utf-8"))
    base = store.latest(issue)
    if base is None:
        raise ValueError("Missing frozen base")
    # A retry after a successful freeze must never consume another AI request.
    if base.key != spec["base_key"] and "fact_correction_reviewed" in base.warnings:
        result = {"status": "ready", "issue": issue, "key": base.key, "created": False}
        atomic_write(Path(".runtime/result.json"), json.dumps(result))
        return
    stories = corrected(base, spec)
    events, review_texts = asyncio.run(originals(stories))
    summaries = [
        AIStory(
            event_id=s.id,
            title=s.title,
            summary=s.summary,
            impact=s.impact,
            category=s.category,
            value_score=s.score,
            evidence_ids=[r.id for r in s.sources],
            uncertainty=s.uncertainty,
        )
        for s in stories
    ]
    for summary, event in zip(summaries, events):
        try:
            validate_response(AIResponse(stories=[summary]), [event])
        except ValueError:
            print(json.dumps({"status": "correction_local_rejected", "event_id": event.id}))
            raise
    enhancer = EditorialEnhancer(settings, store, issue=issue)
    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        vertexai=False,
        http_options=types.HttpOptions(
            base_url="https://generativelanguage.googleapis.com",
            api_version="v1beta",
            timeout=60000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    expected = {s.id for s in stories}

    def check(result):
        if len(result.reviews) != len(expected) or {r.event_id for r in result.reviews} != expected:
            raise ValueError("Review event mismatch")

    payload = []
    for summary, event in zip(summaries, events):
        source_input = evidence_payload(event)
        for evidence in source_input["evidence"]:
            evidence["excerpt"] = review_texts[evidence["id"]]
        payload.append({"story": summary.model_dump(), "input": source_input})
    review = enhancer.call(
        client,
        "\n只做事实终审，逐条检查数字、币种、数量级、发布时态、归因和前瞻标记。不要重新选稿。"
        "影响分析可以为合理的可能性；官方声明可有归因地报道。每个外层event_id给approved及原因。",
        payload,
        Reviews,
        "correction_review",
        3000,
        validator=check,
    )
    rejected = [r.event_id for r in review.reviews if not r.approved]
    if rejected:
        print(json.dumps({"status": "correction_review_veto", "event_ids": rejected}))
        if spec.get("require_all", False):
            raise ValueError("Layout verification requires unchanged approved content")
        stories = [s for s in stories if s.id not in rejected]
    if not stories:
        raise ValueError("No stories passed correction review")
    digest = Digest.model_validate(
        {
            **base.model_dump(),
            "revision": base.revision + 1,
            "created_at": datetime.now(UTC),
            "stories": stories,
            "warnings": list(dict.fromkeys([*base.warnings, "fact_correction_reviewed"])),
        }
    )
    store.state.issues[digest.key] = IssueState()
    store.save_digest(digest)
    result = {
        "status": "ready",
        "issue": issue,
        "key": digest.key,
        "mode": digest.mode,
        "created": True,
        "count": len(stories),
        "requests": enhancer.budget.run_calls,
    }
    atomic_write(Path(".runtime/result.json"), json.dumps(result))
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "correction_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
