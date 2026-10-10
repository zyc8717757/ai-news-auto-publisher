from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .models import Digest, RunState


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
        if changed == 0:
            return
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
        subprocess.run(
            ["git", "push", "origin", "HEAD:news-data"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
