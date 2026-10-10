import importlib.util
import sys
from pathlib import Path

import pytest
from nacl import encoding, public

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


github_setup = load("github_setup")
configure_local = load("configure_local")


def test_github_secret_is_sealed_before_upload():
    private = public.PrivateKey.generate()
    uploaded = []
    api = object.__new__(github_setup.GitHub)
    api.repo = "owner/news"

    def request(method, path, body=None):
        if method == "GET":
            return {"key": private.public_key.encode(encoding.Base64Encoder).decode(), "key_id": "key-id"}
        uploaded.append(body)

    api.request = request
    api.secret("GEMINI_API_KEY", "PRIVATE_DEMO_KEY")
    assert "PRIVATE_DEMO_KEY" not in str(uploaded)
    ciphertext = encoding.Base64Encoder.decode(uploaded[0]["encrypted_value"])
    assert public.SealedBox(private).decrypt(ciphertext) == b"PRIVATE_DEMO_KEY"


def test_configuration_requires_admission_before_secret_writes():
    class API:
        def admission(self):
            return False

        def secret(self, *args):
            pytest.fail("No secrets may be written before admission")

    fields = {
        "sender": ["demo@qq.com"],
        "recipient": [""],
        "smtp_code": ["code"],
        "gemini_key": ["key"],
        "free_confirmed": ["yes"],
    }
    with pytest.raises(ValueError, match="admission"):
        configure_local.apply_configuration(API(), fields)


def test_configuration_minimal_fields_and_order():
    operations = []

    class API:
        def admission(self):
            return True

        def variable(self, name, value):
            operations.append(("variable", name, value))

        def secret(self, name, value):
            operations.append(("secret", name, value))

        def dispatch(self, mode, issue):
            operations.append(("dispatch", mode, issue))

    fields = {
        "sender": ["demo@qq.com"],
        "recipient": [""],
        "smtp_code": ["code"],
        "gemini_key": ["key"],
        "free_confirmed": ["yes"],
    }
    configure_local.apply_configuration(API(), fields)
    assert operations[0] == ("variable", "PUBLISHER_ENABLED", "false")
    assert ("secret", "SMTP_HOST", "smtp.qq.com") in operations
    assert ("secret", "MAIL_TO", "demo@qq.com") in operations
    assert operations[-2] == ("variable", "PUBLISHER_ENABLED", "true")
    assert operations[-1][0:2] == ("dispatch", "run")
