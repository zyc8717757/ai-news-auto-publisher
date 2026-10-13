import json

import pytest

from ai_news.ai import AIUnavailable, Budget
from ai_news.editorial import EditorialEnhancer
from ai_news.models import AIStory, NewsEvent
from ai_news.store import Store


def editor_case(settings, tmp_path, source_item, now, monkeypatch):
    settings.model = "gemini-3.5-flash-lite"
    store = Store(tmp_path)
    editor = EditorialEnhancer(
        settings, store, budget=Budget(settings, store, now=lambda: now, issue="2026-10-13-am")
    )
    event = NewsEvent(
        id="fund",
        evidence=[
            source_item.model_copy(
                update={
                    "title": "Funding announced",
                    "excerpt": "Raised $870 million at a $7.5 billion valuation.",
                }
            )
        ],
    )
    story = AIStory(
        event_id="fund",
        title="公司宣布融资",
        summary="该公司融资75亿美元，估值8.7亿美元。",
        impact="可能为产品开发提供更多资金。",
        category="商业",
        value_score=80,
        evidence_ids=[source_item.id],
    )
    return editor, store, event, story


def test_semantic_review_corrects_swapped_metrics_and_mainland_chinese(
    settings, tmp_path, source_item, now, monkeypatch
):
    editor, store, event, story = editor_case(settings, tmp_path, source_item, now, monkeypatch)
    review = {
        "event_id": "fund",
        "approved": True,
        "translation_ok": True,
        "reason": "已核对融资与估值口径，并修正翻译",
        "failure": "none",
        "replacement": {
            "title": "公司宣布融资",
            "summary": "该公司宣布融资8.7亿美元，估值75亿美元。",
            "impact": "可能为产品开发提供更多资金。",
            "uncertainty": "来自公司声明。",
            "selection_reason": "",
        },
        "numeric_checks": [
            {
                "field": "summary",
                "output_quote": "融资8.7亿美元",
                "evidence_id": source_item.id,
                "source_quote": "Raised $870 million",
            },
            {
                "field": "summary",
                "output_quote": "估值75亿美元",
                "evidence_id": source_item.id,
                "source_quote": "$7.5 billion valuation",
            },
        ],
    }
    captured = []

    def generate(*args):
        captured.append(args[2])
        return json.dumps({"reviews": [review]}, ensure_ascii=False)

    monkeypatch.setattr(editor, "generate_text", generate)
    result = editor.review_summaries(object(), [story], [event])
    assert result[0].summary == review["replacement"]["summary"]
    assert story.summary != result[0].summary
    assert "中国大陆" in captured[0] and "numeric_spans" in captured[0]
    assert len(Store(tmp_path).state.ai_attempts) == 1


@pytest.mark.parametrize("bad_kind", ["missing_anchor", "wrong_metric", "language_not_approved"])
def test_approval_flag_alone_cannot_bypass_numeric_or_translation_review(
    settings, tmp_path, source_item, now, monkeypatch, bad_kind
):
    editor, store, event, story = editor_case(settings, tmp_path, source_item, now, monkeypatch)
    review = {
        "event_id": "fund",
        "approved": True,
        "translation_ok": bad_kind != "language_not_approved",
        "reason": "通过",
        "failure": "none",
        "replacement": None,
        "numeric_checks": [],
    }
    if bad_kind == "wrong_metric":
        review["numeric_checks"] = [
            {
                "field": "summary",
                "output_quote": "融资75亿美元",
                "evidence_id": source_item.id,
                "source_quote": "$7.5 billion valuation",
            }
        ]
    monkeypatch.setattr(editor, "generate_text", lambda *args: json.dumps({"reviews": [review]}))
    with pytest.raises(AIUnavailable, match="ai_output_invalid"):
        editor.review_summaries(object(), [story], [event])
    assert len(Store(tmp_path).state.ai_attempts) == 2


def test_bad_numeric_audit_does_not_erase_an_independently_verified_sibling(
    settings, tmp_path, source_item, now, monkeypatch
):
    editor, store, event, story = editor_case(settings, tmp_path, source_item, now, monkeypatch)
    other = event.model_copy(update={"id": "other"})
    safe = story.model_copy(update={"event_id": "other", "summary": "该公司宣布融资，实际进展仍需持续观察。"})
    rows = [
        {
            "event_id": key,
            "approved": True,
            "translation_ok": True,
            "reason": "通过",
            "failure": "none",
            "numeric_checks": [],
        }
        for key in ("fund", "other")
    ]
    monkeypatch.setattr(editor, "generate_text", lambda *args: json.dumps({"reviews": rows}))
    assert [s.event_id for s in editor.review_summaries(object(), [story, safe], [event, other])] == ["other"]
    assert len(Store(tmp_path).state.ai_attempts) == 2
