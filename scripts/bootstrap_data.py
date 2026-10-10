"""Prepare a dedicated data checkout using the workflow's existing Git credential helper."""

import argparse
import subprocess
from pathlib import Path


def run(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=Path(".runtime/data"))
    args = parser.parse_args()
    if args.path.exists():
        raise ValueError("Data checkout path must be new")
    # No token interpolation: checkout's configured extraheader remains in the source git config.
    remote = run("remote", "get-url", "origin").stdout.strip()
    result = run("ls-remote", "--heads", "origin", "news-data")
    if result.stdout.strip():
        run("fetch", "origin", "news-data")
        run("worktree", "add", "-b", "news-data", str(args.path), "FETCH_HEAD")
    else:
        args.path.mkdir(parents=True)
        run("init", "--initial-branch=news-data", cwd=args.path)
        run("remote", "add", "origin", remote, cwd=args.path)
        # Reuse the helper configuration without serializing tokens to a public file.
        config = run("config", "--local", "--get-regexp", r"^http\..*\.extraheader$")
        for line in config.stdout.splitlines():
            name, value = line.split(" ", 1)
            run("config", name, value, cwd=args.path)
    print("Public data checkout ready")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Data checkout initialization failed") from None
