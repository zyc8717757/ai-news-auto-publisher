import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ai_news import cli
from ai_news.ai import AIUnavailable
from ai_news.models import AIRequestAudit
from scripts.workflow_issue import (
    automatic_recovery_base_key,
    build_command,
    recovery_issue_for_run,
)


def test_actions_pinned_and_pr_has_no_secrets():
    for path in Path(".github/workflows").glob("*.yml"):
        content = path.read_text("utf-8")
        workflow = yaml.load(content, Loader=yaml.BaseLoader)
        assert "on" in workflow and "permissions" in workflow
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", step["uses"])
    tests = Path(".github/workflows/tests.yml").read_text("utf-8")
    assert "secrets." not in tests and "contents: write" not in tests


def test_live_workflow_gated_and_private_data_not_uploaded():
    workflow = yaml.load(Path(".github/workflows/news.yml").read_text("utf-8"), Loader=yaml.BaseLoader)
    publish = workflow["jobs"]["publish"]
    assert "PUBLISHER_ENABLED" in publish["if"] and "default_branch" in publish["if"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert len(workflow["on"]["schedule"]) == 3
    assert all(s["timezone"] == "Asia/Shanghai" for s in workflow["on"]["schedule"])
    build = next(s for s in publish["steps"] if s.get("id") == "build")
    assert "inputs.mode != 'send'" in build["if"]
    uploads = [s for s in publish["steps"] if "upload" in s.get("uses", "")]
    assert all(s.get("with", {}).get("path") in {"site", "${{ runner.temp }}/artifact.tar"} for s in uploads)
    assert "inputs.mode != 'validate-news'" in build["if"]
    notify = next(s for s in publish["steps"] if s.get("name") == "Verify deployment and send private email")
    assert "inputs.mode != 'validate-news'" in notify["if"]


def test_test_enhance_forwards_explicit_frozen_base_and_test_ceiling():
    assert build_command("test-enhance", "2026-10-28-am", "", "2026-10-28-am-r1", 2) == [
        "ai-news",
        "--data",
        ".runtime/data",
        "--remote",
        "enhance-issue",
        "--issue",
        "2026-10-28-am",
        "--base-key",
        "2026-10-28-am-r1",
        "--test-extra-requests",
        "2",
    ]


def test_validate_news_with_base_key_uses_nonpublishing_frozen_validation():
    issue = "2026-10-28-am"
    assert build_command("validate-news", issue, "", f"{issue}-r1") == [
        "ai-news",
        "--data",
        ".runtime/data",
        "--remote",
        "enhance-issue",
        "--issue",
        issue,
        "--base-key",
        f"{issue}-r1",
        "--validate-only",
    ]


class MemoryStore:
    def __init__(self, digest, attempts, audit):
        self.digests = [digest]
        self.state = SimpleNamespace(
            ai_issue_attempts={digest.issue: attempts},
            ai_attempts=[],
            ai_audit=[audit],
        )

    def latest(self, issue):
        matches = [digest for digest in self.digests if digest.issue == issue]
        return max(matches, key=lambda digest: digest.revision) if matches else None

    def save_digest(self, digest):
        self.digests.append(digest)


def rules_store(
    digest,
    now,
    *,
    attempts=18,
    stage="provider_error",
    reason="ai_provider_http_503",
    extra_test=False,
):
    rules = digest.model_copy(update={"mode": "rules", "warnings": ["ai_unavailable"]})
    audit = AIRequestAudit(
        at=now - timedelta(minutes=15),
        issue=rules.issue,
        model="gemini-3.5-flash-lite",
        stage=stage,
        reason=reason,
        extra_test=extra_test,
    )
    return MemoryStore(rules, attempts, audit), rules


def test_automatic_recovery_waits_15_minutes_and_uses_frozen_base(digest, now):
    store, rules = rules_store(digest, now)
    assert automatic_recovery_base_key("run", rules.issue, store, now - timedelta(seconds=1), 20) is None
    base_key = automatic_recovery_base_key("run", rules.issue, store, now, 20)
    assert base_key == rules.key
    assert build_command("enhance-issue", rules.issue, "", base_key) == [
        "ai-news",
        "--data",
        ".runtime/data",
        "--remote",
        "enhance-issue",
        "--issue",
        rules.issue,
        "--base-key",
        rules.key,
    ]
    assert automatic_recovery_base_key("regenerate", rules.issue, store, now, 20) is None


def test_recent_production_attempt_resets_the_delay(digest, now):
    store, rules = rules_store(digest, now)
    store.state.ai_attempts.append(now)
    assert automatic_recovery_base_key("run", rules.issue, store, now + timedelta(minutes=14), 20) is None
    assert (
        automatic_recovery_base_key("run", rules.issue, store, now + timedelta(minutes=15), 20) == rules.key
    )


def test_automatic_recovery_requires_two_production_slots_and_is_idempotent(digest, now):
    store, rules = rules_store(digest, now, attempts=19)
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None
    store.state.ai_issue_attempts[rules.issue] = 18
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) == rules.key
    store.state.ai_audit[-1] = store.state.ai_audit[-1].model_copy(update={"at": now})
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None
    assert (
        automatic_recovery_base_key("run", rules.issue, store, now + timedelta(minutes=15), 20) == rules.key
    )

    enhanced = rules.model_copy(update={"revision": 2, "mode": "ai"})
    store.save_digest(enhanced)
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None


def test_automatic_recovery_rejects_nontransient_and_test_only_audits(digest, now):
    store, rules = rules_store(digest, now, reason="ai_provider_http_400")
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None

    store.state.ai_audit[-1] = store.state.ai_audit[-1].model_copy(
        update={"reason": "ai_provider_http_503", "stage": "output_rejected"}
    )
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None

    store.state.ai_audit[-1] = store.state.ai_audit[-1].model_copy(
        update={"stage": "provider_error", "extra_test": True}
    )
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None

    store.state.ai_audit[-1] = store.state.ai_audit[-1].model_copy(update={"extra_test": False})
    store.state.ai_audit.append(
        store.state.ai_audit[-1].model_copy(
            update={"at": now, "stage": "output_rejected", "reason": "ai_output_invalid"}
        )
    )
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None

    empty = rules.model_copy(update={"revision": 2, "stories": []})
    store.save_digest(empty)
    assert automatic_recovery_base_key("run", rules.issue, store, now, 20) is None


def test_scheduled_recovery_resolves_the_same_issue_as_cli(settings, now):
    assert recovery_issue_for_run("", "17 * * * *", settings, now) == "2026-10-10-am"


def test_cli_ai_unavailable_fallback_only_keeps_exact_nonempty_rules(digest, now, settings, monkeypatch):
    store, rules = rules_store(digest, now)
    result = {}
    monkeypatch.setattr(cli, "load_config", lambda path: (settings, []))
    monkeypatch.setattr(cli, "Store", lambda *args, **kwargs: store)
    monkeypatch.setattr(cli, "atomic_write", lambda path, body: result.update(json.loads(body)))
    monkeypatch.setattr(
        cli,
        "enhance_frozen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AIUnavailable("ai_provider_http_503")),
    )
    args = SimpleNamespace(
        command="enhance-issue",
        config=Path("config"),
        data=Path("data"),
        remote=False,
        issue=rules.issue,
        test_extra_requests=0,
        base_key=rules.key,
        fallback_to_existing_rules=True,
        validate_only=False,
        result=Path("result.json"),
    )
    assert cli.execute(args) == 0
    assert result["quality"] == "rules_degraded" and result["key"] == rules.key

    captured = {}

    def validated(*args, **kwargs):
        captured.update(kwargs)
        return rules.model_copy(update={"revision": 2, "mode": "ai"}), False

    monkeypatch.setattr(cli, "enhance_frozen", validated)
    args.validate_only = True
    assert cli.execute(args) == 0
    assert captured["validate_only"] is True
    assert result["status"] == "validated" and result["quality"] == "ai_complete"

    monkeypatch.setattr(
        cli,
        "enhance_frozen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AIUnavailable("ai_provider_http_503")),
    )
    args.validate_only = False
    enhanced = rules.model_copy(update={"revision": 2, "mode": "ai"})
    store.save_digest(enhanced)
    with pytest.raises(AIUnavailable):
        cli.execute(args)
