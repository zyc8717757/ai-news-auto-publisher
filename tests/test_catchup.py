from datetime import datetime

from ai_news.pipeline import scheduled_issue


def test_hourly_recovery_selects_latest_due_issue():
    for at, expected in [
        ("2026-10-11T13:00:00+08:00", "2026-10-11-am"),
        ("2026-10-11T08:29:00+08:00", "2026-10-10-pm"),
        ("2026-10-11T20:31:00+08:00", "2026-10-11-pm"),
    ]:
        assert scheduled_issue("17 * * * *", datetime.fromisoformat(at), "08:30", "20:30") == expected


def test_completed_catchup_does_not_collect_or_send(digest, settings, tmp_path, monkeypatch):
    from ai_news import cli
    from ai_news.models import IssueState
    from ai_news.store import Store

    store = Store(tmp_path)
    store.state.issues[digest.key] = IssueState(deployment="ready", email="sent")
    store.save_digest(digest)
    monkeypatch.setattr(cli, "load_config", lambda path: (settings, []))
    monkeypatch.setattr(cli, "scheduled_issue", lambda *args: digest.issue)
    monkeypatch.setattr(
        cli, "generate", lambda *args: (_ for _ in ()).throw(AssertionError("Unexpected generation"))
    )
    result = tmp_path / "result.json"
    args = cli.parser().parse_args(
        ["--data", str(tmp_path), "generate", "--schedule", "17 * * * *", "--result", str(result)]
    )
    assert cli.execute(args) == 0
    assert '"skipped"' in result.read_text()
