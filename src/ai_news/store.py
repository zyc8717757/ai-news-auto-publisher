from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .models import Digest, RunState

_TRANSIENT_PUSH_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "could not resolve",
    "remote end hung up",
    "unexpected disconnect while reading sideband packet",
    "unexpected disconnect",
    "early eof",
    "unexpected eof",
    "http stream",
    "http/2 stream",
    "http2 stream",
    "tls connection",
    "server aborted the ssl handshake",
    "http 429",
    "error: 429",
    "error: 500",
    "error: 502",
    "error: 503",
    "error: 504",
)
_AUTH_PUSH_MARKERS = ("authentication failed", "permission denied", "could not read username", "http 403")
_NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "fetch first", "stale info")


def _is_transient_push_failure(stderr: str) -> bool:
    error = stderr.lower()
    return any(marker in error for marker in _TRANSIENT_PUSH_MARKERS) or bool(
        re.search(r"curl\s+\(?\s*(18|52|55|56|92)\b", error)
    )


def _push_failure_class(stderr: str) -> str:
    error = stderr.lower()
    if any(marker in error for marker in _NON_FAST_FORWARD_MARKERS):
        return "non_fast_forward"
    if any(marker in error for marker in _AUTH_PUSH_MARKERS):
        return "authentication"
    if _is_transient_push_failure(stderr):
        return "transport_exhausted"
    return "unclassified"


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


class Store:
    """Only validated public models cross the persistent/remote boundary."""

    def __init__(self, root: Path, remote: bool = False):
        self.root = root.resolve()
        self.remote = remote
        path = self.root / "state.json"
        self.state = RunState.model_validate_json(path.read_text("utf-8")) if path.exists() else RunState()

    def commit(self) -> None:
        if not self.remote:
            return
        # This directory must be a dedicated news-data checkout; never stage the source workspace.
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=self.root, text=True
        ).strip()
        if branch != "news-data":
            raise RuntimeError("Remote persistence requires the dedicated news-data branch")
        subprocess.run(
            ["git", "add", "--", "state.json", "digests"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.root).returncode
        if changed not in {0, 1}:
            raise RuntimeError("public_data_git_diff_failed")
        if changed:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=ai-news-bot",
                    "-c",
                    "user.email=ai-news-bot@users.noreply.github.com",
                    "commit",
                    "-m",
                    "Update public digest state",
                ],
                cwd=self.root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # A previous push may have failed after committing locally. Even with
        # no staged diff, durability requires pushing that pending commit.
        for attempt in range(3):
            result = subprocess.run(
                ["git", "push", "origin", "HEAD:news-data"],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            if self._remote_head_matches_local():
                return
            failure_class = _push_failure_class(result.stderr)
            transient = failure_class == "transport_exhausted"
            if not transient or attempt == 2:
                print(
                    f"public_data_git_push_failure={failure_class}",
                    file=sys.stderr,
                )
                raise RuntimeError("public_data_git_push_failed")
            time.sleep(2 ** (attempt + 1))

    def _remote_head_matches_local(self) -> bool:
        """Confirm an ambiguous push only when the remote branch has this exact commit."""
        try:
            local_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, stderr=subprocess.DEVNULL
            ).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40,64}", local_head):
                return False
            remote = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", "refs/heads/news-data"],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return False
        if remote.returncode != 0:
            return False
        matches = [
            fields[0].lower()
            for line in remote.stdout.splitlines()
            if len(fields := line.split()) == 2 and fields[1] == "refs/heads/news-data"
        ]
        return len(matches) == 1 and matches[0] == local_head

    def save_state(self) -> None:
        # Validate again in case a caller mutated a nested container in place.
        validated = RunState.model_validate(self.state.model_dump())
        atomic_write(self.root / "state.json", validated.model_dump_json(indent=2))
        (self.root / "digests").mkdir(exist_ok=True)
        self.commit()

    def save_digest(self, digest: Digest) -> None:
        digest = Digest.model_validate(digest.model_dump())
        path = self.root / "digests" / f"{digest.key}.json"
        if path.exists():
            raise ValueError("Frozen digest revision already exists")
        atomic_write(path, digest.model_dump_json(indent=2))
        self.save_state()

    def digests(self) -> list[Digest]:
        return [
            Digest.model_validate(json.loads(path.read_text("utf-8")))
            for path in sorted((self.root / "digests").glob("*.json"))
        ]

    def latest(self, issue: str) -> Digest | None:
        matches = [digest for digest in self.digests() if digest.issue == issue]
        return max(matches, key=lambda digest: digest.revision) if matches else None
