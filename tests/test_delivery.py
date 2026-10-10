from email import policy
from email.parser import BytesParser

import pytest

from ai_news.mailer import MailConfig, SendFailed, SendUnknown, deliver, make_message
from ai_news.models import IssueState
from ai_news.store import Store


def config():
    return MailConfig(
        "smtp.example.com",
        465,
        "ssl",
        "test",
        "PRIVATE_PASSWORD",
        "sender@example.com",
        "private@example.net",
    )


def test_mime_full_content_and_attachments(digest):
    message = make_message(digest, config(), "https://reader.github.io/news", True)
    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert digest.stories[0].summary in parsed.get_body(preferencelist=("plain",)).get_content()
    assert "版本 A" in parsed.get_body(preferencelist=("html",)).get_content()
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 3
    assert all(a.get_filename().endswith(".txt") for a in attachments[:2])
    assert attachments[2].get_content_type() == "image/png"
    assert attachments[2].get_payload(decode=True).startswith(b"\x89PNG")
    assert "演示" in attachments[0].get_content()
    assert (
        parsed["Message-ID"]
        == make_message(digest, config(), "https://reader.github.io/news", True)["Message-ID"]
    )


def test_sent_is_idempotent_and_private(digest, tmp_path):
    store = Store(tmp_path)
    calls = []

    class Capture:
        def send(self, message):
            assert Store(tmp_path).state.issues[digest.key].email == "sending"
            calls.append(message.as_bytes())

    assert deliver(digest, store, config(), "https://reader.github.io/news", channel=Capture()) == "sent"
    assert (
        deliver(digest, store, config(), "https://reader.github.io/news", channel=Capture()) == "already_sent"
    )
    assert len(calls) == 1
    persisted = (tmp_path / "state.json").read_text()
    for value in ("PRIVATE_PASSWORD", "private@example.net", "版本 A", digest.stories[0].summary):
        assert value not in persisted


@pytest.mark.parametrize("error, expected", [(SendUnknown, "delivery_unknown"), (SendFailed, "failed")])
def test_failure_classification(digest, tmp_path, error, expected):
    class Channel:
        def send(self, message):
            raise error("PRIVATE_PROVIDER_RESPONSE")

    store = Store(tmp_path)
    assert deliver(digest, store, config(), "https://reader.github.io/news", channel=Channel()) == expected
    assert "PRIVATE_PROVIDER" not in (tmp_path / "state.json").read_text()


def test_crash_sending_marker_requires_explicit_resend(digest, tmp_path):
    store = Store(tmp_path)
    store.state.issues[digest.key] = IssueState(email="sending")
    store.save_state()

    class Channel:
        def send(self, message):
            pytest.fail("Ambiguous mail must not be automatically resent")

    assert (
        deliver(digest, store, config(), "https://reader.github.io/news", channel=Channel())
        == "delivery_unknown"
    )


def test_accepted_but_persistence_failed(digest, tmp_path, monkeypatch):
    store = Store(tmp_path)
    real_save = store.save_state

    def save():
        if store.state.issues[digest.key].email == "sent":
            raise RuntimeError("disk failed")
        real_save()

    monkeypatch.setattr(store, "save_state", save)

    class Channel:
        def send(self, message):
            pass

    with pytest.raises(RuntimeError, match="mail_accepted"):
        deliver(digest, store, config(), "https://reader.github.io/news", channel=Channel())
    assert Store(tmp_path).state.issues[digest.key].email == "sending"


def test_configuration_rejects_injection(monkeypatch):
    for key, value in {
        "SMTP_HOST": "smtp.qq.com",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "code",
        "MAIL_FROM": "sender@example.com",
        "MAIL_TO": "private@example.com\r\nBcc: thief@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        MailConfig.from_env()
