"""Cầu nối WeInvoice: giải mã mật khẩu (định dạng của WeInvoiceSecretCipher Java) + API login-test nội bộ."""

import base64
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import weinvoice_bridge  # noqa: E402

KEY = bytes(range(32))


def _encrypt_like_java(plain: str, key: bytes = KEY, iv: bytes = b"\x01" * 12) -> str:
    """'v1:' + base64(iv(12) || ciphertext || tag(16)) — y như WeInvoiceSecretCipher.encrypt."""
    return "v1:" + base64.b64encode(iv + AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)).decode()


def test_decrypt_round_trip_unicode():
    assert weinvoice_bridge.decrypt(_encrypt_like_java("Mật-khẩu#2026"), KEY) == "Mật-khẩu#2026"


def test_decrypt_empty_or_missing_key_is_none(monkeypatch):
    assert weinvoice_bridge.decrypt(None, KEY) is None
    assert weinvoice_bridge.decrypt("", KEY) is None
    monkeypatch.delenv("WEINVOICE_SECRET_KEY", raising=False)
    assert weinvoice_bridge.decrypt(_encrypt_like_java("x")) is None


def test_decrypt_wrong_key_or_format_raises():
    with pytest.raises(ValueError):
        weinvoice_bridge.decrypt(_encrypt_like_java("x"), bytes(32))
    with pytest.raises(ValueError):
        weinvoice_bridge.decrypt("plain-text", KEY)


def test_key_from_env_must_be_32_bytes(monkeypatch):
    monkeypatch.setenv("WEINVOICE_SECRET_KEY", base64.b64encode(KEY).decode())
    assert weinvoice_bridge.decrypt(_encrypt_like_java("abc")) == "abc"
    monkeypatch.setenv("WEINVOICE_SECRET_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        weinvoice_bridge.decrypt(_encrypt_like_java("abc"))


def test_schema_name_is_validated(monkeypatch):
    monkeypatch.setenv("WEINVOICE_APP_SCHEMA", "weinvoice; DROP TABLE x")
    with pytest.raises(ValueError):
        weinvoice_bridge.app_schema()


# ---------------------------------------------------------------- internal API

@pytest.fixture()
def client(monkeypatch):
    import internal_api
    monkeypatch.setenv("SCRAPING_INTERNAL_API_KEY", "k-123")
    calls = []

    def fake_auth(username, password, max_retries=6):
        calls.append((username, password))
        if password == "right":
            return {"ok": True, "token": "jwt-secret", "message": "", "attempts": 2}
        if password == "captcha-hell":
            return {"ok": False, "token": "", "message": "Thử 6 lần không được. Lỗi cuối: captcha",
                    "attempts": 6}
        return {"ok": False, "token": "", "message": "Tên đăng nhập hoặc mật khẩu không đúng", "attempts": 1}

    monkeypatch.setattr(internal_api.api_client, "authenticate_auto", fake_auth)
    monkeypatch.setattr(internal_api, "_guard_store", lambda: None)   # không bao giờ chạm DB thật
    c = internal_api.app.test_client()
    c.calls = calls
    return c


def _post(client, body, key="k-123"):
    return client.post("/internal/gdt/login-test", json=body, headers={"X-Api-Key": key} if key else {})


def test_login_ok_never_returns_token(client):
    r = _post(client, {"mst": "1201496252", "username": "1201496252-U002", "password": "right"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "reason": "ok", "message": "", "attempts": 2}
    assert "jwt-secret" not in r.get_data(as_text=True)
    assert client.calls == [("1201496252-U002", "right")]


def test_wrong_password_is_rejected_with_gdt_message(client):
    j = _post(client, {"mst": "1201496252", "password": "wrong"}).get_json()
    assert j["ok"] is False and j["reason"] == "rejected"
    assert "mật khẩu không đúng" in j["message"]
    assert client.calls == [("1201496252", "wrong")]  # username mặc định = MST


def test_captcha_exhausted_is_unavailable(client):
    j = _post(client, {"mst": "1201496252", "password": "captcha-hell"}).get_json()
    assert j["reason"] == "unavailable"


def test_missing_or_wrong_key_is_401_and_no_login(client):
    assert _post(client, {"mst": "1", "password": "right"}, key=None).status_code == 401
    assert _post(client, {"mst": "1", "password": "right"}, key="bad").status_code == 401
    assert client.calls == []


def test_unconfigured_key_refuses_everything(client, monkeypatch):
    monkeypatch.delenv("SCRAPING_INTERNAL_API_KEY")
    assert _post(client, {"mst": "1", "password": "right"}).status_code == 503


def test_missing_fields_is_400(client):
    assert _post(client, {"mst": "1201496252"}).status_code == 400
