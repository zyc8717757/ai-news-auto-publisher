import os
import subprocess
import time


def invoke(*args):
    return subprocess.run(["ai-news", "--data", ".runtime/data", "--remote", *args], check=False).returncode


def main():
    issue = os.environ["ISSUE"]
    mode = os.environ.get("RUN_MODE", "run")
    if mode != "send":
        verified = False
        if os.environ.get("DEPLOY_OUTCOME") == "success":
            for attempt in range(3):
                if invoke("mark-deployment", "--issue", issue) == 0:
                    verified = True
                    break
                if attempt < 2:
                    time.sleep(10)
        if not verified:
            if invoke("mark-deployment", "--issue", issue, "--failed"):
                return 1
    if mode == "redeploy":
        return 0 if verified else 1
    args = ["send", "--issue", issue]
    if os.environ.get("RESEND") == "true":
        args.append("--resend")
    return invoke(*args)


if __name__ == "__main__":
    raise SystemExit(main())
