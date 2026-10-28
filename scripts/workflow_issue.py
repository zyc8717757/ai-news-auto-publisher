"""Translate trusted workflow inputs into CLI arguments without shell interpolation."""

import json
import os
import subprocess
from pathlib import Path

from ai_news.pipeline import cutoff_for
from ai_news.store import Store, atomic_write


def build_command(mode: str, issue: str, schedule: str, base_key: str, test_total: int = 0):
    command = [
        "ai-news",
        "--data",
        ".runtime/data",
        "--remote",
        "enhance-issue" if mode in {"enhance-issue", "test-enhance"} else "generate",
    ]
    command += ["--schedule", schedule] if schedule else ["--issue", issue]
    if mode in {"enhance-issue", "test-enhance"} and base_key:
        command += ["--base-key", base_key]
    if mode in {"regenerate", "validate-news"}:
        command += ["--regenerate"]
    if mode == "test-enhance":
        command += ["--test-extra-requests", str(test_total)]
    return command


def main():
    mode = os.environ.get("RUN_MODE", "run")
    issue = os.environ.get("REQUESTED_ISSUE", "")
    schedule = os.environ.get("SCHEDULE_CRON", "")
    if not schedule:
        cutoff_for(issue)
    if mode == "review-issue":
        subprocess.run(["python", "scripts/review_issue.py"], check=True)
        result = json.loads(Path(".runtime/result.json").read_text("utf-8"))
    elif mode == "check-ai":
        subprocess.run(
            ["ai-news", "--data", ".runtime/data", "--remote", "check-ai", "--issue", issue], check=True
        )
        result = {"status": "checked"}
    elif mode in {"redeploy", "send"}:
        digest = Store(Path(".runtime/data")).latest(issue)
        if digest is None:
            raise ValueError("Missing frozen digest")
        result = {"status": "ready", "issue": issue}
        atomic_write(Path(".runtime/result.json"), json.dumps(result))
    elif mode in {"run", "regenerate", "enhance-issue", "test-enhance", "validate-news"}:
        test_total = 0
        if mode == "test-enhance":
            if os.environ.get("AI_TEST_AUTHORIZED_ISSUE") != issue:
                raise ValueError("This test issue was not explicitly authorized")
            total = int(os.environ.get("AI_TEST_AUTHORIZED_TOTAL", "0"))
            if not 1 <= total <= 5:
                raise ValueError("Explicit cumulative test ceiling required")
            test_total = total
        command = build_command(mode, issue, schedule, os.environ.get("REQUESTED_BASE_KEY", ""), test_total)
        subprocess.run(command, check=True)
        result = json.loads(Path(".runtime/result.json").read_text("utf-8"))
    else:
        raise ValueError("Unsupported workflow mode")
    ready = result["status"] == "ready"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"ready={'true' if ready else 'false'}\n")
        if ready:
            output.write(f"issue={result['issue']}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Issue resolution failed") from None
