"""Loopback-only one-shot GitHub token input; no credential logging or files."""

import json
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx


def main():
    csrf = secrets.token_urlsafe(32)
    busy = threading.Lock()
    job = {"status": "waiting"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            url = urlsplit(self.path)
            if not self.valid_host() or not secrets.compare_digest(
                parse_qs(url.query).get("token", [""])[0], csrf
            ):
                self.respond(403, "请求无效")
                return
            if url.path == "/status":
                self.respond(
                    200,
                    f"<meta charset='utf-8'><h1>配置状态：{job['status']}</h1><p>成功仅表示限权Token已写入Cloudflare Secret；调度启用和验证由后续步骤完成。</p>",
                )
                return
            self.respond(
                200,
                "<meta charset='utf-8'><h1>Cloudflare独立调度：安全录入</h1>"
                "<p>仅输入新建的GitHub fine-grained token：只选择 ai-news-auto-publisher 仓库，Actions读写权限。不要输入Gemini或邮箱凭据。</p>"
                f"<form method='post' action='/configure' autocomplete='off'><input type='hidden' name='csrf' value='{csrf}'>"
                "<p><input type='password' name='token' required autocomplete='new-password' aria-label='GitHub限权Token'></p>"
                "<p><label><input type='checkbox' name='confirmed' value='yes' required>我确认仅授权本仓库Actions读写，并同意将此Token保存为Cloudflare专用Worker的GITHUB_DISPATCH_TOKEN Secret。先执行一次无AI、无邮件的preview触发以验证权限。</label></p>"
                "<button>安全保存</button></form>",
            )

        def do_POST(self):
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if (
                not self.valid_host()
                or self.path != "/configure"
                or self.headers.get("Origin") not in {None, "null", expected}
            ):
                self.respond(403, "不允许的来源")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16000:
                    raise ValueError
                fields = parse_qs(self.rfile.read(size).decode("utf-8"))
                value = fields.get("token", [""])[0].strip()
                if not secrets.compare_digest(fields.get("csrf", [""])[0], csrf) or fields.get(
                    "confirmed"
                ) != ["yes"]:
                    raise ValueError
                if not value.startswith("github_pat_") or not busy.acquire(False):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.respond(400, "需要有效的限权Token和确认，或请求正在处理。")
                return
            job["status"] = "running"

            def apply():
                try:
                    with httpx.Client(proxy="http://127.0.0.1:10808", trust_env=False, timeout=30) as client:
                        response = client.post(
                            "https://api.github.com/repos/zyc8717757/ai-news-auto-publisher/actions/workflows/news.yml/dispatches",
                            headers={
                                "Authorization": f"Bearer {value}",
                                "Accept": "application/vnd.github+json",
                            },
                            json={"ref": "main", "inputs": {"mode": "preview"}},
                        )
                        if response.status_code != 204:
                            raise ValueError
                    environment = {
                        **os.environ,
                        "HTTPS_PROXY": "http://127.0.0.1:10808",
                        "npm_config_proxy": "http://127.0.0.1:10808",
                        "npm_config_https_proxy": "http://127.0.0.1:10808",
                    }
                    result = subprocess.run(
                        [
                            "pwsh.exe",
                            "-NoProfile",
                            "-Command",
                            "npx.cmd --yes wrangler@4.149.0 secret put GITHUB_DISPATCH_TOKEN --config infra/cloudflare-scheduler/wrangler.jsonc",
                        ],
                        input=value + "\n",
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        cwd=Path(__file__).resolve().parents[1],
                        env=environment,
                        timeout=120,
                    )
                    if result.returncode:
                        raise ValueError
                    job["status"] = "success"
                except Exception:
                    job["status"] = "failed"
                    busy.release()
                finally:
                    fields.clear()

            threading.Thread(target=apply, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", f"/status?token={csrf}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"setup_url": f"http://127.0.0.1:{server.server_port}/?token={csrf}"}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
