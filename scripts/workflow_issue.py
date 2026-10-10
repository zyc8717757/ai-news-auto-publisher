"""Translate trusted workflow inputs into CLI arguments without shell interpolation."""

import json
import os
import subprocess
from pathlib import Path

from ai_news.pipeline import cutoff_for
from ai_news.store import Store, atomic_write


def main():
    mode = os.environ.get("RUN_MODE", "run")
    issue = os.environ.get("REQUESTED_ISSUE", "")
    schedule = os.environ.get("SCHEDULE_CRON", "")
    if not schedule:
        cutoff_for(issue)
    if mode == "check-ai":
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
    elif mode in {"run", "regenerate", "enhance-issue"}:
        command = [
            "ai-news",
            "--data",
            ".runtime/data",
            "--remote",
            "enhance-issue" if mode == "enhance-issue" else "generate",
        ]
        command += ["--schedule", schedule] if schedule else ["--issue", issue]
        if mode == "regenerate":
            command += ["--regenerate"]
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
