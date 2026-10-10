"""Real local TLS/SMTP and Git persistence; no external provider or real recipient."""

import ipaddress
import socketserver
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_delivery import config

from ai_news.mailer import SMTPChannel, deliver
from ai_news.store import Store


def test_real_tls_smtp_capture(digest, tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    captured = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 local test SMTP\r\n")
            while line := self.rfile.readline():
                verb = line.split(b" ", 1)[0].strip().upper()
                if verb in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250-localhost\r\n250 AUTH PLAIN\r\n")
                elif verb == b"AUTH":
                    self.wfile.write(b"235 authenticated\r\n")
                elif verb in {b"MAIL", b"RCPT"}:
                    self.wfile.write(b"250 accepted\r\n")
                elif verb == b"DATA":
                    self.wfile.write(b"354 data follows\r\n")
                    body = []
                    while (chunk := self.rfile.readline()) != b".\r\n":
                        if not chunk:
                            return
                        body.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                    captured.append(b"".join(body))
                    self.wfile.write(b"250 accepted\r\n")
                else:
                    self.wfile.write(b"250 ok\r\n")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

        def get_request(self):
            connection, address = super().get_request()
            return context.wrap_socket(connection, server_side=True), address

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda: original(cafile=str(cert_path)))
    mail = config()
    mail.host, mail.port = "127.0.0.1", server.server_address[1]
    try:
        store = Store(tmp_path / "data")
        assert (
            deliver(digest, store, mail, "https://reader.github.io/news", channel=SMTPChannel(mail)) == "sent"
        )
        assert len(captured) == 1
        message = BytesParser(policy=policy.default).parsebytes(captured[0])
        assert digest.stories[0].summary in message.get_body(preferencelist=("plain",)).get_content()
        assert len(list(message.iter_attachments())) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_remote_state_survives_fresh_checkout(digest, tmp_path):
    bare, checkout, fresh = tmp_path / "remote.git", tmp_path / "data", tmp_path / "fresh"
    git("init", "--bare", str(bare), cwd=tmp_path)
    checkout.mkdir()
    git("init", "--initial-branch=news-data", cwd=checkout)
    git("remote", "add", "origin", str(bare), cwd=checkout)
    store = Store(checkout, remote=True)
    store.state.ai_attempts.append(datetime.now(UTC))
    store.save_state()
    store.save_digest(digest)
    git("clone", "--branch=news-data", str(bare), str(fresh), cwd=tmp_path)
    recovered = Store(fresh)
    assert recovered.latest(digest.issue).key == digest.key
    assert len(recovered.state.ai_attempts) == 1
    assert not list(fresh.glob("*.eml"))
