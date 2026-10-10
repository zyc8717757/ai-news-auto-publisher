"""Local deployment helper. Credentials stay in memory/Git Credential Manager."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPO_NAME = "ai-news-auto-publisher"


def system_proxy() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    ) as key:
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            value = winreg.QueryValueEx(key, "ProxyServer")[0]
        except FileNotFoundError:
            return None
    if not enabled:
        return None
    if ";" in value or "=" in value:
        pairs = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        value = pairs.get("https") or pairs.get("http")
    return value if value and "://" in value else f"http://{value}" if value else None


class GitHub:
    def __init__(self, expected_login: str):
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        result = subprocess.run(
            ["git", "-c", "credential.interactive=false", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\nusername={expected_login}\n\n",
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = values.get("password")
        if result.returncode or not token:
            raise RuntimeError("GitHub login required in Git Credential Manager")
        self.proxy = system_proxy()
        self.client = httpx.Client(
            base_url="https://api.github.com",
            proxy=self.proxy,
            trust_env=False,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self.user = self.request("GET", "/user")
        if self.user["login"].lower() != expected_login.lower():
            raise RuntimeError("Authenticated account does not match selected owner")
        self.repo = f"{self.user['login']}/{REPO_NAME}"

    def request(self, method: str, path: str, body=None, allow_404=False):
        response = self.client.request(method, path, json=body)
        if allow_404 and response.status_code == 404:
            return None
        if not response.is_success:
            # GitHub response bodies can include private details; never echo them.
            raise RuntimeError(f"GitHub operation failed: HTTP {response.status_code}")
        return response.json() if response.content else None

    def ensure_repository(self):
        existing = self.request("GET", f"/repos/{self.repo}", allow_404=True)
        if existing:
            if existing["private"]:
                raise RuntimeError("Existing repository is private; refusing to change visibility")
            return existing, False
        return self.request(
            "POST",
            "/user/repos",
            {
                "name": REPO_NAME,
                "private": False,
                "auto_init": False,
                "description": "Free AI news digests, GitHub Pages and private email drafts; manual X publishing.",
                "has_issues": True,
            },
        ), True

    def variable(self, name: str, value: str):
        path = f"/repos/{self.repo}/actions/variables/{name}"
        existing = self.request("GET", path, allow_404=True)
        self.request(
            "PATCH" if existing else "POST",
            path if existing else f"/repos/{self.repo}/actions/variables",
            {"name": name, "value": value},
        )

    def secret(self, name: str, value: str):
        from nacl import encoding, public

        key = self.request("GET", f"/repos/{self.repo}/actions/secrets/public-key")
        encrypted = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder)).encrypt(
            value.encode()
        )
        self.request(
            "PUT",
            f"/repos/{self.repo}/actions/secrets/{name}",
            {
                "encrypted_value": encoding.Base64Encoder.encode(encrypted).decode(),
                "key_id": key["key_id"],
            },
        )

    def dispatch(self, mode: str, issue: str = ""):
        self.request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/news.yml/dispatches",
            {
                "ref": "main",
                "inputs": {"mode": mode, "issue": issue},
            },
        )

    def admission(self) -> bool:
        head = self.request("GET", f"/repos/{self.repo}/branches/main")["commit"]["sha"]
        tests = self.request(
            "GET", f"/repos/{self.repo}/actions/workflows/tests.yml/runs?branch=main&per_page=10"
        )
        if not any(
            run["head_sha"] == head and run["conclusion"] == "success" for run in tests["workflow_runs"]
        ):
            return False
        runs = self.request(
            "GET", f"/repos/{self.repo}/actions/workflows/news.yml/runs?branch=main&per_page=20"
        )
        for run in runs["workflow_runs"]:
            if run["head_sha"] != head or run["status"] != "completed":
                continue
            jobs = self.request("GET", f"/repos/{self.repo}/actions/runs/{run['id']}/jobs")
            if any(job["name"] == "source-health" and job["conclusion"] == "success" for job in jobs["jobs"]):
                return True
        return False


def git(*args, proxy=None):
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    if proxy:
        environment.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy)
    result = subprocess.run(["git", *args], cwd=ROOT, env=environment, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Git operation failed; credentials and provider output suppressed")
    return result.stdout.strip()


def publish(api: GitHub):
    repository, created = api.ensure_repository()
    url = repository["clone_url"]
    if not (ROOT / ".git").exists():
        if not created and repository.get("size", 0):
            raise RuntimeError("Existing nonempty repository requires inspection before publishing")
        git("init", "--initial-branch=main")
        git("remote", "add", "origin", url)
    elif git("remote", "get-url", "origin") != url:
        raise RuntimeError("Existing local origin does not match requested repository")
    git("config", "user.name", api.user["login"])
    git("config", "user.email", f"{api.user['id']}+{api.user['login']}@users.noreply.github.com")
    paths = [
        ".github",
        ".gitignore",
        ".env.example",
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "requirements.lock",
        "package.json",
        "package-lock.json",
        "config",
        "docs",
        "examples",
        "src",
        "tests",
        "scripts",
    ]
    git("add", "--", *paths)
    staged = git("diff", "--cached", "--name-only").splitlines()
    if any(p.startswith((".local/", ".runtime/", ".venv/")) or p.endswith((".eml", ".pem")) for p in staged):
        raise RuntimeError("Private/runtime content staged; refusing to publish")
    if staged:
        git("commit", "-m", "Implement AI News Auto Publisher Free Edition")
    git("push", "--set-upstream", "origin", "main", proxy=api.proxy)
    api.request("PATCH", f"/repos/{api.repo}", {"default_branch": "main"})
    pages = api.request("GET", f"/repos/{api.repo}/pages", allow_404=True)
    if pages is None:
        api.request("POST", f"/repos/{api.repo}/pages", {"build_type": "workflow"})
    elif pages.get("build_type") != "workflow":
        raise RuntimeError("Existing Pages configuration differs; inspection required")
    api.variable("PAGES_BASE_URL", f"https://{api.user['login']}.github.io/{REPO_NAME}")
    for name in ("PUBLISHER_ENABLED", "AI_ENABLED", "EMAIL_ENABLED", "AI_FREE_TIER_CONFIRMED"):
        # Preserve confirmed settings on subsequent code updates.
        if api.request("GET", f"/repos/{api.repo}/actions/variables/{name}", allow_404=True) is None:
            api.variable(name, "false")
    return repository["html_url"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--create-only", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--check-sources", action="store_true")
    args = parser.parse_args()
    api = GitHub(args.owner)
    if args.create_only:
        repository, created = api.ensure_repository()
        print(json.dumps({"repository": repository["html_url"], "created": created}))
    elif args.publish:
        print(json.dumps({"repository": publish(api), "status": "uploaded"}))
    elif args.check_sources:
        api.dispatch("check-sources")
        print('{"status":"source_check_dispatched"}')
    else:
        print(json.dumps({"login": api.user["login"], "repository": api.repo}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
