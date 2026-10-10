"""Loopback-only, one-shot setup UI. No secret persistence, logging, screenshots or artifacts."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from github_setup import GitHub

from ai_news.models import Settings

SCHEDULE_FIELDS = """<fieldset><legend>北京时间定时设置</legend>
<label>早报时间<input name="morning_time" type="time" value="{morning}" required></label>
<label>晚报时间<input name="evening_time" type="time" value="{evening}" required></label>
<small>默认每天08:00、20:00；早报时间须早于晚报。保存后自动同步云端调度。</small></fieldset>"""

FORM = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI News 私人配置</title><style>body{font:16px/1.8 system-ui;max-width:650px;margin:35px auto;padding:20px;background:#f5f8f8;color:#182c35}
label{display:block;margin:18px 0}input:not([type=checkbox]){display:block;width:100%;box-sizing:border-box;padding:12px;font:inherit}
button{padding:12px 22px;background:#006d77;color:white;border:0;font:inherit;border-radius:5px}small{color:#52666e}</style>
<h1>AI News 私人配置</h1><p>目标仓库：<strong>{repository}</strong></p>
<p>仅需填写以下信息。密钥在本机内存中加密后提交 GitHub Secrets，不保存到代码、日志或聊天。
这一步会配置并启用早晚任务，发布公开日报，向指定邮箱发送完整日报和私人 X 草稿。</p>
<form method="post" action="/configure" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label>发信邮箱（QQ 或 163）<input name="sender" type="email" required placeholder="你的邮箱@qq.com" autocomplete="off"></label>
<label>SMTP 授权码<input name="smtp_code" type="password" required autocomplete="new-password"></label>
<label>Gemini 免费项目 API Key<input name="gemini_key" type="password" required autocomplete="new-password"></label>
<label><input name="free_confirmed" type="checkbox" value="yes" required> 我已确认此专用 Gemini 项目未关联付费账单，并具有可用免费额度。</label>
<details><summary>可选：发送到其他邮箱</summary><label>接收邮箱（留空则发送给发信邮箱自己）<input name="recipient" type="email" autocomplete="off"></label></details>
{schedule_fields}<button type="submit">安全保存配置并启用</button></form>
<p><small>不会接入 X，也不会自动发帖。配置页仅绑定本机回环地址，完成后自动关闭服务。</small></p></html>"""


def selected_schedule(fields):
    morning = fields.get("morning_time", ["08:00"])
    evening = fields.get("evening_time", ["20:00"])
    if len(morning) != 1 or len(evening) != 1:
        raise ValueError("Duplicate schedule field")
    Settings(base_url="https://example.com", morning_time=morning[0], evening_time=evening[0])
    return morning[0], evening[0]


def sync_schedule(api, morning, evening):
    if not api.update_schedule(morning, evening):
        return
    api.dispatch("check-sources")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if api.admission():
            return
        time.sleep(10)
    raise RuntimeError("New schedule cloud checks have not passed")


def apply_configuration(api: GitHub, fields: dict[str, list[str]]) -> None:
    def value(name):
        values = fields.get(name, [])
        if len(values) != 1:
            raise ValueError("Missing or duplicate field")
        return values[0].strip()

    sender, code, key = value("sender"), value("smtp_code"), value("gemini_key")
    morning, evening = selected_schedule(fields)
    recipient = fields.get("recipient", [""])[0].strip() or sender
    if value("free_confirmed") != "yes":
        raise ValueError("Free-tier project confirmation required")
    if any("\r" in text or "\n" in text for text in [sender, recipient, code, key]):
        raise ValueError("Invalid input")
    import re

    if not all(re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address) for address in (sender, recipient)):
        raise ValueError("Invalid email address")
    host = {"qq.com": "smtp.qq.com", "foxmail.com": "smtp.qq.com", "163.com": "smtp.163.com"}.get(
        sender.split("@")[1].lower()
    )
    if not host or not code or not key:
        raise ValueError("QQ/163 sender and both keys required")
    if not api.admission():
        raise ValueError("Cloud tests and source-health admission must pass for the current commit")
    # Check the complete form before making any changes. Disable until every encrypted write succeeds.
    api.variable("PUBLISHER_ENABLED", "false")
    sync_schedule(api, morning, evening)
    for name, text in {
        "SMTP_HOST": host,
        "SMTP_PORT": "465",
        "SMTP_SECURITY": "ssl",
        "SMTP_USERNAME": sender,
        "SMTP_PASSWORD": code,
        "MAIL_FROM": sender,
        "MAIL_TO": recipient,
        "GEMINI_API_KEY": key,
    }.items():
        api.secret(name, text)
    for name, text in {
        "AI_FREE_TIER_CONFIRMED": "true",
        "AI_ENABLED": "true",
        "EMAIL_ENABLED": "true",
        "PUBLISHER_PAUSED": "false",
        "GEMINI_RPM": "5",
        "GEMINI_TPM": "250000",
        "GEMINI_RPD": "20",
        "PUBLISHER_ENABLED": "true",
    }.items():
        api.variable(name, text)
    local = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    current = local.strftime("%H:%M")
    day = local.date() - timedelta(days=1) if current < morning else local.date()
    slot = "am" if morning <= current < evening else "pm"
    api.dispatch("run", f"{day.isoformat()}-{slot}")


def apply_schedule(api, fields):
    morning, evening = selected_schedule(fields)
    was_enabled = api.request("GET", f"/repos/{api.repo}/actions/variables/PUBLISHER_ENABLED")["value"]
    api.variable("PUBLISHER_ENABLED", "false")
    sync_schedule(api, morning, evening)
    api.variable("PUBLISHER_ENABLED", was_enabled)


def create_server(api, token: str, port: int = 0, configure=apply_configuration, schedule_only=False):
    submitted = threading.Lock()
    job = {"state": "idle"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if not self.valid_host() or query.get("token") != [token]:
                self.respond(403, "配置入口无效。")
                return
            if urlsplit(self.path).path == "/status":
                if job["state"] == "running":
                    self.respond(
                        200,
                        "<meta charset='utf-8'><meta http-equiv='refresh' content='2'><h1>正在安全保存配置</h1><p>正在加密保存与核验云端设置。调整时间后需等待测试通过，页面会自动更新，请勿重复提交。</p>",
                    )
                elif job["state"] == "success":
                    self.respond(
                        200,
                        "<meta charset='utf-8'><h1>配置已加密保存</h1><p>云端配置已更新。首次完整运行和实际收信请查看 GitHub Actions。</p>",
                    )
                    closer = threading.Timer(15, self.server.shutdown)
                    closer.daemon = True
                    closer.start()
                else:
                    self.respond(
                        400,
                        f"<meta charset='utf-8'><h1>配置未完成</h1><p>请检查字段和云端状态。密钥未写入日志。</p><a href='/?token={token}'>重新填写</a>",
                    )
                return
            if not api.admission():
                self.respond(
                    200,
                    "<meta charset='utf-8'><meta http-equiv='refresh' content='10'><h1>正在完成云端准入检查</h1><p>当前代码的测试与信源检查通过后，会自动显示安全配置表单。无需重复操作。</p>",
                )
                return
            # Escape before using a plain template; CSS braces are intentionally not format placeholders.
            morning, evening = getattr(api, "schedule", lambda: ("08:00", "20:00"))()
            schedule_fields = SCHEDULE_FIELDS.replace("{morning}", morning).replace("{evening}", evening)
            page = (
                FORM.replace("{repository}", html.escape(api.repo))
                .replace("{csrf}", token)
                .replace("{schedule_fields}", schedule_fields)
            )
            if schedule_only:
                page = (
                    "<meta charset='utf-8'><h1>调整早晚报时间</h1><p>只修改北京时间调度，无需再次输入密钥。</p>"
                    f"<form method='post' action='/configure'><input type='hidden' name='csrf' value='{token}'>"
                    + schedule_fields
                    + "<button type='submit'>保存时间</button></form>"
                )
            self.respond(200, page)

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if not self.valid_host() or self.path != "/configure" or origin not in {None, "null", expected}:
                self.respond(403, "不允许的来源。")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond(400, "请求大小无效。")
                return
            if not 0 < size <= 16000:
                self.respond(400, "请求大小无效。")
                return
            fields = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            supplied = fields.pop("csrf", [])
            # Sandboxed in-app browsers may omit Origin or send null. Host and the secret
            # per-session form token remain mandatory; explicit foreign origins are rejected.
            if (
                len(supplied) != 1
                or not secrets.compare_digest(supplied[0], token)
                or not submitted.acquire(blocking=False)
            ):
                self.respond(403, "请求无效或正在处理。")
                return
            job["state"] = "running"

            def worker():
                try:
                    (apply_schedule if schedule_only else configure)(api, fields)
                    job["state"] = "success"
                except Exception:
                    try:
                        api.variable("PUBLISHER_ENABLED", "false")
                    except Exception:
                        pass
                    job["state"] = "failed"
                    submitted.release()
                finally:
                    fields.clear()

            threading.Thread(target=worker, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", f"/status?token={token}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    return LocalServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--port", type=int, default=0, help="0 selects a free loopback port")
    parser.add_argument("--schedule-only", action="store_true", help="Change times without re-entering keys")
    args = parser.parse_args()
    api = GitHub(args.owner)
    token = secrets.token_urlsafe(32)
    server = create_server(api, token, args.port, schedule_only=args.schedule_only)
    print(json.dumps({"setup_url": f"http://127.0.0.1:{server.server_port}/?token={token}"}), flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "setup_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
