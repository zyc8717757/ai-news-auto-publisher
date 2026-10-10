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
    updated = module.corrected(
        base, {"base_key": base.key, "updates": {story.id: {"title": "经原文核实的标题"}}}
    )
    assert updated[0].sources == story.sources
    assert updated[0].title != story.title
    assert base.stories[0].title == story.title


def test_review_mode_never_publishes_or_emails():
    workflow = Path(".github/workflows/news.yml").read_text("utf-8")
    assert workflow.count("inputs.mode != 'review-issue'") == 2


def test_publication_only_exposes_selected_pending_revision(digest, tmp_path, monkeypatch, settings):
    from ai_news import cli
    from ai_news.models import IssueState
    from ai_news.store import Store

    store = Store(tmp_path)
    store.state.issues[digest.key] = IssueState(deployment="ready")
    store.save_digest(digest)
    store.save_digest(digest.model_copy(update={"revision": 2}))
    store.save_digest(digest.model_copy(update={"revision": 3}))
    rendered = []
    monkeypatch.setattr(cli, "load_config", lambda path: (settings, []))
    monkeypatch.setattr(cli, "build_site", lambda digests, *args: rendered.extend(digests))
    args = cli.parser().parse_args(["--data", str(tmp_path), "build-site", "--issue", digest.issue])
    assert cli.execute(args) == 0
    assert {d.revision for d in rendered} == {1, 3}
