import re
from pathlib import Path

import yaml


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
    assert len(workflow["on"]["schedule"]) == 2
    assert all(s["timezone"] == "Asia/Shanghai" for s in workflow["on"]["schedule"])
    build = next(s for s in publish["steps"] if s.get("id") == "build")
    assert "inputs.mode != 'send'" in build["if"]
    uploads = [s for s in publish["steps"] if "upload" in s.get("uses", "")]
    assert all(s.get("with", {}).get("path") in {"site", "${{ runner.temp }}/artifact.tar"} for s in uploads)
    assert "inputs.mode != 'validate-news'" in build["if"]
    notify = next(s for s in publish["steps"] if s.get("name") == "Verify deployment and send private email")
    assert "inputs.mode != 'validate-news'" in notify["if"]
