"""Loopback-only, one-shot setup UI. No secret persistence, logging, screenshots or artifacts."""

from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from github_setup import GitHub

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
<button type="submit">安全保存配置并启用</button></form>
<p><small>不会接入 X，也不会自动发帖。配置页仅绑定本机回环地址，完成后自动关闭服务。</small></p></html>"""


def apply_configuration(api: GitHub, fields: dict[str, list[str]]) -> None:
    def value(name):
        values = fields.get(name, [])
        if len(values) != 1:
            raise ValueError("Missing or duplicate field")
        return values[0].strip()

    sender, code, key = value("sender"), value("smtp_code"), value("gemini_key")
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
        "GEMINI_RPM": "1",
        "GEMINI_TPM": "16000",
        "GEMINI_RPD": "10",
        "PUBLISHER_ENABLED": "true",
    }.items():
        api.variable(name, text)
    local = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    day = local.date() - timedelta(days=1) if local.hour < 8 else local.date()
    slot = "am" if 8 <= local.hour < 20 else "pm"
    api.dispatch("run", f"{day.isoformat()}-{slot}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    api = GitHub(args.owner)
    token = secrets.token_urlsafe(32)
    submitted = threading.Lock()

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
            return self.headers.get("Host") == f"127.0.0.1:{args.port}"

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if not self.valid_host() or query.get("token") != [token]:
                self.respond(403, "配置入口无效。")
                return
            # Escape before using a plain template; CSS braces are intentionally not format placeholders.
            page = FORM.replace("{repository}", html.escape(api.repo)).replace("{csrf}", token)
            self.respond(200, page)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (
                not self.valid_host()
                or self.path != "/configure"
                or origin != f"http://127.0.0.1:{args.port}"
            ):
                self.respond(403, "不允许的来源。")
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16000:
                self.respond(400, "请求大小无效。")
                return
            fields = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            if fields.pop("csrf", None) != [token] or not submitted.acquire(blocking=False):
                self.respond(403, "请求无效或正在处理。")
                return
            try:
                apply_configuration(api, fields)
                self.respond(
                    200,
                    "<meta charset='utf-8'><h1>配置已加密保存</h1><p>已启用早晚任务，并启动首期完整运行。请在 GitHub Actions 查看结果，核验 AI 与邮箱实际收信。此配置服务现在关闭。</p>",
                )
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            except Exception:
                # Keep credentials out of errors and disable live runs on partial configuration failure.
                try:
                    api.variable("PUBLISHER_ENABLED", "false")
                except Exception:
                    pass
                self.respond(
                    400,
                    "<meta charset='utf-8'><h1>配置未完成</h1><p>任务保持关闭。请检查字段、仓库权限及网络；密钥没有写入日志。</p>",
                )
                submitted.release()
            finally:
                fields.clear()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"setup_url": f"http://127.0.0.1:{args.port}/?token={token}"}), flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "setup_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
