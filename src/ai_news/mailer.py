from __future__ import annotations

import hashlib
import html
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from .drafts import make_drafts
from .models import Digest, IssueState
from .site import digest_url
from .store import Store


@dataclass(repr=False)
class MailConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipient: str

    @classmethod
    def from_env(cls):
        names = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"]
        if any(not os.environ.get(name) for name in names):
            raise ValueError("mail_configuration_missing")
        values = [os.environ[name] for name in names]
        if any("\r" in value or "\n" in value for value in values):
            raise ValueError("Invalid mail header")
        host, username, password, sender, recipient = values
        if not all(re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", value) for value in (sender, recipient)):
            raise ValueError("Expected one sender and one recipient address")
        security = os.environ.get("SMTP_SECURITY") or "ssl"
        port = int(os.environ.get("SMTP_PORT") or "465")
        if (security, port) not in {("ssl", 465), ("starttls", 587)}:
            raise ValueError("Only TLS submission ports 465/587 are allowed")
        return cls(host, port, security, username, password, sender, recipient)


def make_message(digest: Digest, config: MailConfig, base_url: str, deployment_ready: bool) -> EmailMessage:
    url = digest_url(base_url, digest)
    drafts = make_drafts(digest, url)
    sections = [
        digest.label,
        f"{'AI增强版' if digest.mode == 'ai' else '基础规则版：AI增强未完成'} · {len(digest.stories)}条",
    ]
    sections.append(f"完整日报：{url}" if deployment_ready else "网页部署尚未确认，以下邮件内容可独立阅读。")
    if digest.warnings:
        sections.append("本期处理提示：" + ", ".join(digest.warnings))
    for i, story in enumerate(digest.stories, 1):
        body = f"{i}. {story.title}\n{story.summary}"
        if story.impact:
            body += "\n为什么重要：" + story.impact
        if story.uncertainty:
            body += "\n说明：" + story.uncertainty
        if story.update_note:
            body += "\n新增进展：" + story.update_note
        body += "\n" + "\n".join(f"{source.source_name}: {source.url}" for source in story.sources)
        sections.append(body)
    sections.append("以下两版为待人工审核草稿。复制区块内正文或使用附件；系统不会自动发布到 X。")
    if not deployment_ready:
        sections.append("草稿中的日报链接尚未确认可访问，请网页恢复后再发布。")
    sections.extend(
        [
            f"版本 A · 精简版（{drafts.short_weight}/280）",
            drafts.short,
            f"版本 B · Premium 长帖版（{drafts.long_weight}/8000）",
            drafts.long,
        ]
    )
    message = EmailMessage()
    message["Subject"] = (
        f"[AI News][{'AI' if digest.mode == 'ai' else '规则降级'}] {digest.label} · {len(digest.stories)}条"
    )
    message["From"], message["To"] = config.sender, config.recipient
    # Stable per digest revision; RFC Message-ID is advisory, not an exactly-once primitive.
    identity = hashlib.sha256(digest.key.encode()).hexdigest()[:32]
    message["Message-ID"] = f"<{identity}@{config.sender.split('@')[1]}>"
    message.set_content("\n\n".join(sections))
    blocks = "".join(
        f"<pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{html.escape(s)}</pre>" for s in sections
    )
    message.add_alternative(f"<!doctype html><html lang='zh-CN'><body>{blocks}</body></html>", subtype="html")
    for kind, text in (("short", drafts.short), ("premium", drafts.long)):
        message.add_attachment(text, subtype="plain", charset="utf-8", filename=f"{digest.key}-{kind}.txt")
    return message


class SendFailed(RuntimeError):
    """The provider definitely did not accept this message."""


class SendUnknown(RuntimeError):
    """The provider may have accepted DATA; automatic retry could duplicate delivery."""


class NotificationChannel(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SMTPChannel:
    def __init__(self, config: MailConfig):
        self.config = config

    def send(self, message: EmailMessage) -> None:
        config = self.config
        smtp = None
        data_started = False
        try:
            context = ssl.create_default_context()
            if config.security == "ssl":
                smtp = smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context)
            else:
                smtp = smtplib.SMTP(config.host, config.port, timeout=30)
                smtp.ehlo()
                smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(config.username, config.password)
            code, _ = smtp.mail(config.sender)
            if code != 250:
                raise SendFailed("mail_sender_rejected")
            code, _ = smtp.rcpt(config.recipient)
            if code not in {250, 251}:
                raise SendFailed("mail_recipient_rejected")
            data_started = True
            # DATA is intentionally separated from connection/authentication for ambiguity tracking.
            code, _ = smtp.data(message.as_bytes())
            if code != 250:
                raise SendFailed("mail_data_rejected")
        except SendFailed:
            raise
        except smtplib.SMTPDataError:
            raise SendFailed("mail_data_rejected") from None
        except Exception:
            if data_started:
                raise SendUnknown("mail_delivery_unknown") from None
            raise SendFailed("mail_connection_or_auth_failed") from None
        finally:
            if smtp:
                # QUIT failure after a 250 response does not invalidate accepted delivery.
                try:
                    smtp.close()
                except Exception:
                    pass


def deliver(
    digest: Digest,
    store: Store,
    config: MailConfig,
    base_url: str,
    force: bool = False,
    channel: NotificationChannel | None = None,
) -> str:
    state = store.state.issues.setdefault(digest.key, IssueState())
    if state.email == "sent" and not force:
        return "already_sent"
    if state.email in {"sending", "unknown"} and not force:
        state.email = "unknown"
        store.save_state()
        return "delivery_unknown"
    if state.email_attempts >= 3 and not force:
        return "retry_limit"
    message = make_message(digest, config, base_url, state.deployment == "ready")
    attempts = 1 if force else max(0, 3 - state.email_attempts)
    for attempt in range(attempts):
        state.email = "sending"
        state.email_attempts += 1
        store.save_state()
        try:
            (channel or SMTPChannel(config)).send(message)
            break
        except SendFailed:
            state.email = "failed"
            store.save_state()
            if attempt + 1 == attempts:
                return "failed"
            time.sleep(2**attempt)
        except SendUnknown:
            state.email = "unknown"
            store.save_state()
            return "delivery_unknown"
    state.email = "sent"
    try:
        store.save_state()
    except Exception:
        # The remote sending marker is retained: a later process will require explicit resend.
        state.email = "unknown"
        raise RuntimeError("mail_accepted_state_persistence_failed") from None
    return "sent"
