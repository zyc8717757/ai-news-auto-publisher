"""Translate trusted workflow inputs into CLI arguments without shell interpolation."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_news.config import load_config
from ai_news.pipeline import cutoff_for, scheduled_issue
from ai_news.store import Store, atomic_write

AUTO_RECOVERY_DELAY = timedelta(minutes=15)
AUTO_RECOVERY_MIN_REMAINING = 2
TRANSIENT_PROVIDER_STATUSES = {429, 500, 502, 503, 504}


def build_command(mode: str, issue: str, schedule: str, base_key: str, test_total: int = 0):
    frozen_validation = mode == "validate-news" and bool(base_key)
    frozen_enhancement = mode in {"enhance-issue", "test-enhance"} or frozen_validation
    command = [
        "ai-news",
        "--data",
        ".runtime/data",
        "--remote",
        "enhance-issue" if frozen_enhancement else "generate",
    ]
    command += ["--schedule", schedule] if schedule else ["--issue", issue]
    if frozen_enhancement and base_key:
        command += ["--base-key", base_key]
    if frozen_validation:
        command += ["--validate-only"]
    if mode == "regenerate" or (mode == "validate-news" and not frozen_validation):
        command += ["--regenerate"]
    if mode == "test-enhance":
        command += ["--test-extra-requests", str(test_total)]
    return command


def recovery_issue_for_run(issue: str, schedule: str, settings, now: datetime) -> str:
    if schedule:
        return scheduled_issue(schedule, now, settings.morning_time, settings.evening_time) or ""
    return issue


def automatic_recovery_base_key(mode: str, issue: str, store: Store, now: datetime, ai_run_limit: int):
    """Select only a delayed retry of the same nonempty rules digest after a transient provider failure."""
    if mode != "run" or not issue:
        return None
    digest = store.latest(issue)
    if digest is None or digest.mode != "rules" or not digest.stories:
        return None

    attempts = store.state.ai_issue_attempts.get(issue, 0)
    if not store.state.ai_issue_attempts and store.state.ai_attempts:
        # Match Budget's conservative migration for legacy global-only ledgers.
        attempts = len(store.state.ai_attempts)
    if ai_run_limit - attempts < AUTO_RECOVERY_MIN_REMAINING:
        return None

    audits = [audit for audit in store.state.ai_audit if audit.issue == issue]
    if not audits:
        return None
    latest_audit = max(audits, key=lambda audit: audit.at)
    if latest_audit.extra_test or latest_audit.stage != "provider_error":
        return None
    try:
        status = int(latest_audit.reason.removeprefix("ai_provider_http_"))
    except ValueError:
        return None
    if not latest_audit.reason.startswith("ai_provider_http_") or status not in TRANSIENT_PROVIDER_STATUSES:
        return None
    last_call = max([latest_audit.at, *store.state.ai_attempts])
    if now - last_call < AUTO_RECOVERY_DELAY:
        return None
    return digest.key


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
        recovery_key = None
        recovery_issue = issue
        settings = None
        if mode == "run":
            settings, _ = load_config(Path("config"))
            if "AI_ENABLED" in os.environ:
                settings.ai_enabled = os.environ["AI_ENABLED"].lower() == "true"
            if os.environ.get("PUBLISHER_PAUSED", "").lower() == "true":
                settings.paused = True
            decision_time = datetime.now(UTC)
            recovery_issue = recovery_issue_for_run(issue, schedule, settings, decision_time)
            if (
                recovery_issue
                and settings.ai_enabled
                and not settings.paused
                and os.environ.get("PUBLISHER_PAUSED", "").lower() != "true"
                and os.environ.get("AI_FREE_TIER_CONFIRMED", "").lower() == "true"
            ):
                recovery_key = automatic_recovery_base_key(
                    mode,
                    recovery_issue,
                    Store(Path(".runtime/data")),
                    decision_time,
                    settings.ai_run_limit,
                )

        if recovery_key:
            command = build_command("enhance-issue", recovery_issue, "", recovery_key)
            command.append("--fallback-to-existing-rules")
            subprocess.run(command, check=True)
        else:
            command = build_command(
                mode, issue, schedule, os.environ.get("REQUESTED_BASE_KEY", ""), test_total
            )
            subprocess.run(command, check=True)
        result = json.loads(Path(".runtime/result.json").read_text("utf-8"))
        quality = result.get("quality")
        if quality == "ai_complete":
            print("Digest quality: AI complete")
        elif quality == "rules_degraded":
            print("Digest quality: rules degraded after AI failure")
        elif quality == "rules_only":
            print("Digest quality: rules only")
    else:
        raise ValueError("Unsupported workflow mode")
    ready = result["status"] == "ready"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        # Only fixed status labels are rendered; model text never enters the summary.
        labels = {
            "ai_complete": "AI 全量校验通过",
            "rules_degraded": "规则降级，AI 校验未完成",
            "rules_only": "来源规则版",
        }
        label = labels.get(result.get("quality"), "本步未执行内容校验")
        operation = "只验证，未创建发布修订" if result["status"] == "validated" else "冻结或复用日报"
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"### 日报内容状态\n\n{label}；{operation}。\n\n部署与邮件状态以各自步骤为准。\n")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"ready={'true' if ready else 'false'}\n")
        if ready:
            output.write(f"issue={result['issue']}\n")
            if result.get("mode") in {"ai", "rules"}:
                output.write(f"mode={result['mode']}\n")
            if result.get("quality") in {"ai_complete", "rules_degraded", "rules_only"}:
                output.write(f"quality={result['quality']}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Issue resolution failed") from None
