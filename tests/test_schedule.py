import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from test_setup import github_setup

from ai_news.models import Settings
from ai_news.pipeline import cron_for, cutoff_for, scheduled_issue


def test_adjusted_beijing_cutoff_and_cron():
    assert cron_for("08:35") == "35 8 * * *"
    assert cutoff_for("2026-10-10-am", "08:35", "21:15") == datetime(2026, 10, 10, 0, 35, tzinfo=UTC)
    assert (
        scheduled_issue("15 21 * * *", datetime(2026, 10, 10, 13, 20, tzinfo=UTC), "08:35", "21:15")
        == "2026-10-10-pm"
    )
    with pytest.raises(ValueError):
        scheduled_issue("0 8 * * *", datetime(2026, 10, 10, 1, tzinfo=UTC), "08:35", "21:15")


@pytest.mark.parametrize(
    "morning,evening", [("25:00", "20:00"), ("20:00", "08:00"), ("08:00", "08:00"), ("08:60", "21:00")]
)
def test_schedule_validation(morning, evening):
    with pytest.raises(ValueError):
        Settings(base_url="https://example.com", morning_time=morning, evening_time=evening)


def test_schedule_and_workflow_updated_in_one_nonforced_commit():
    api = object.__new__(github_setup.GitHub)
    api.repo = "test/no-write"
    writes = []

    def request(method, path, body=None):
        if method == "GET":
            if "/git/ref/" in path:
                return {"object": {"sha": "original-head"}}
            if "/git/commits/" in path:
                return {"tree": {"sha": "original-tree"}}
            filename = path.split("/contents/")[1].split("?")[0]
            return {"content": base64.b64encode(Path(filename).read_bytes()).decode()}
        writes.append((method, path, body))
        return {"sha": "new-commit" if path.endswith("commits") else "new-tree"}

    api.request = request
    assert api.update_schedule("09:10", "21:25")
    files = {entry["path"]: entry["content"] for entry in writes[0][2]["tree"]}
    settings = yaml.safe_load(files["config/settings.yaml"])
    workflow = yaml.load(files[".github/workflows/news.yml"], Loader=yaml.BaseLoader)
    assert (settings["morning_time"], settings["evening_time"]) == ("09:10", "21:25")
    assert [s["cron"] for s in workflow["on"]["schedule"]] == ["10 9 * * *", "25 21 * * *", "17 * * * *"]
    assert writes[1][2]["parents"] == ["original-head"]
    assert writes[-1][2]["force"] is False
