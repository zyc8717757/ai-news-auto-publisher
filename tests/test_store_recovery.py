from types import SimpleNamespace

import pytest

from ai_news.store import Store


def mock_local_head(monkeypatch, local_head):
    def check_output(command, **kwargs):
        if command[1] == "branch":
            return "news-data\n"
        assert command[1:3] == ["rev-parse", "HEAD"]
        return f"{local_head}\n"

    monkeypatch.setattr("ai_news.store.subprocess.check_output", check_output)


def remote_head_result(commit):
    return SimpleNamespace(
        returncode=0,
        stdout=f"{commit}\trefs/heads/news-data\n",
        stderr="",
    )


def test_pending_commit_is_pushed_even_without_new_diff(tmp_path, monkeypatch):
    pushes, delays = [], []
    mock_local_head(monkeypatch, "a" * 40)
    monkeypatch.setattr("ai_news.store.time.sleep", delays.append)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1 if len(pushes) == 1 else 0, stderr="fatal: HTTP error: 503")
        if command[1] == "ls-remote":
            return remote_head_result("b" * 40)
        assert command[1] in {"add", "diff"}
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    Store(tmp_path, remote=True).commit()
    assert len(pushes) == 2 and delays == [2]


def test_ambiguous_push_ack_is_confirmed_by_exact_remote_head(tmp_path, monkeypatch):
    local_head = "a" * 40
    pushes, remote_checks, delays = [], [], []
    mock_local_head(monkeypatch, local_head)
    monkeypatch.setattr("ai_news.store.time.sleep", delays.append)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(
                returncode=1,
                stderr="fatal: RPC failed; curl 92 HTTP/2 stream 0 was not closed cleanly: INTERNAL_ERROR",
            )
        if command[1] == "ls-remote":
            remote_checks.append(command)
            return remote_head_result(local_head)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    Store(tmp_path, remote=True).commit()
    assert len(pushes) == len(remote_checks) == 1 and delays == []


def test_authentication_failure_is_not_retried_or_exposed(tmp_path, monkeypatch, capsys):
    pushes, remote_checks = [], []
    mock_local_head(monkeypatch, "a" * 40)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1, stderr="authentication failed: PRIVATE_MARKER")
        if command[1] == "ls-remote":
            remote_checks.append(command)
            return remote_head_result("b" * 40)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    with pytest.raises(RuntimeError, match="^public_data_git_push_failed$"):
        Store(tmp_path, remote=True).commit()
    output = capsys.readouterr()
    assert len(pushes) == len(remote_checks) == 1
    assert "public_data_git_push_failure=authentication" in output.err
    assert "PRIVATE_MARKER" not in output.out + output.err


def test_transient_transport_failure_without_remote_ack_still_fails(tmp_path, monkeypatch, capsys):
    pushes, remote_checks, delays = [], [], []
    mock_local_head(monkeypatch, "a" * 40)
    monkeypatch.setattr("ai_news.store.time.sleep", delays.append)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1, stderr="fatal: remote end hung up unexpectedly; curl 56")
        if command[1] == "ls-remote":
            remote_checks.append(command)
            return remote_head_result("b" * 40)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    with pytest.raises(RuntimeError, match="^public_data_git_push_failed$"):
        Store(tmp_path, remote=True).commit()
    output = capsys.readouterr()
    assert len(pushes) == len(remote_checks) == 3 and delays == [2, 4]
    assert "public_data_git_push_failure=transport_exhausted" in output.err


def test_final_remote_check_confirms_lost_ack(tmp_path, monkeypatch):
    local_head = "a" * 40
    pushes, remote_checks, delays = [], [], []
    mock_local_head(monkeypatch, local_head)
    monkeypatch.setattr("ai_news.store.time.sleep", delays.append)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1, stderr="fatal: early EOF during RPC")
        if command[1] == "ls-remote":
            remote_checks.append(command)
            head = local_head if len(remote_checks) == 3 else "b" * 40
            return remote_head_result(head)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    Store(tmp_path, remote=True).commit()
    assert len(pushes) == len(remote_checks) == 3 and delays == [2, 4]


def test_non_fast_forward_is_not_retried_or_merged(tmp_path, monkeypatch, capsys):
    pushes, remote_checks = [], []
    mock_local_head(monkeypatch, "a" * 40)

    def run(command, **kwargs):
        if command[1] == "push":
            pushes.append(command)
            return SimpleNamespace(returncode=1, stderr="rejected: non-fast-forward")
        if command[1] == "ls-remote":
            remote_checks.append(command)
            return remote_head_result("b" * 40)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("ai_news.store.subprocess.run", run)
    with pytest.raises(RuntimeError, match="^public_data_git_push_failed$"):
        Store(tmp_path, remote=True).commit()
    output = capsys.readouterr()
    assert len(pushes) == len(remote_checks) == 1
    assert "public_data_git_push_failure=non_fast_forward" in output.err
