from types import SimpleNamespace

import pytest

from ai_news.store import Store


def test_pending_commit_is_pushed_even_without_new_diff(tmp_path, monkeypatch):
    pushes, delays = [], []
    monkeypatch.setattr("ai_news.store.subprocess.check_output", lambda *a, **k: "news-data\n")
    monkeypatch.setattr("ai_news.store.time.sleep", delays.append)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1 if len(pushes) == 1 else 0, stderr="fatal: HTTP error: 503")
        assert command[1] in {"add", "diff"}
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    Store(tmp_path, remote=True).commit()
    assert len(pushes) == 2 and delays == [2]


def test_authorization_failure_is_not_retried_or_exposed(tmp_path, monkeypatch):
    pushes = []
    monkeypatch.setattr("ai_news.store.subprocess.check_output", lambda *a, **k: "news-data\n")

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1, stderr="authentication failed: PRIVATE_MARKER")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    with pytest.raises(RuntimeError, match="^public_data_git_push_failed$"):
        Store(tmp_path, remote=True).commit()
    assert len(pushes) == 1
