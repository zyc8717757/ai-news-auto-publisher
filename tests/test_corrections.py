import importlib.util
from pathlib import Path

import pytest

from ai_news.models import Digest


def test_correction_cannot_change_evidence_or_base():
    spec = importlib.util.spec_from_file_location("review_issue", "scripts/review_issue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = Digest.model_validate_json(Path("examples/demo-digest.json").read_text("utf-8"))
    story = base.stories[0]
    with pytest.raises(ValueError):
        module.corrected(base, {"base_key": "wrong", "updates": {}})
    with pytest.raises(ValueError):
        module.corrected(base, {"base_key": base.key, "updates": {story.id: {"sources": []}}})
    updated = module.corrected(base, {"base_key": base.key, "updates": {story.id: {"title": "经原文核实的标题"}}})
    assert updated[0].sources == story.sources
    assert updated[0].title != story.title
    assert base.stories[0].title == story.title


def test_review_mode_never_publishes_or_emails():
    workflow = Path(".github/workflows/news.yml").read_text("utf-8")
    assert workflow.count("inputs.mode != 'review-issue'") == 2
