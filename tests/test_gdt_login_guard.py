"""Không đăng nhập lại GDT bằng mật khẩu Thuế đã từ chối (Thuế khóa tài khoản sau 5 lần sai).

Không gọi GDT, không chạm DB thật: api_client / gdt_login_guard / weinvoice_bridge đều được thay.
"""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import api_client  # noqa: E402
import gdt_login_guard  # noqa: E402
import invoice_job  # noqa: E402

WRONG_PW = "Tên đăng nhập hoặc mật khẩu không đúng"
LOCKED = "Tài khoản đã bị khoá vì đã nhập sai thông tin quá số lần quy định"


# --- phân loại câu lỗi ------------------------------------------------------

@pytest.mark.parametrize("msg,expected", [
    (WRONG_PW, "bad_credentials"),
    (LOCKED, "locked"),
    ("Tài khoản đã bị khóa", "locked"),
    ("Mã captcha không đúng.", "captcha"),
    ("Mã xác nhận không chính xác", "captcha"),
    ("Xác thực thất bại: sai mật khẩu", "bad_credentials"),   # có chữ "xác thực" vẫn là sai mật khẩu
    ("Internal Server Error", "other"),
    ("", "other"),
])
def test_classify_login_failure(msg, expected):
    assert api_client.classify_login_failure(msg) == expected


# --- authenticate_auto: chỉ thử lại khi sai captcha -------------------------

class _Solver:
    def solve(self, svg):
        return "ABCDEF", 0.99


@pytest.fixture()
def gdt(monkeypatch):
    answers = []
    calls = []
    monkeypatch.setattr(api_client, "_get_solver", lambda: _Solver())
    monkeypatch.setattr(api_client, "fetch_captcha", lambda: {"ckey": "k", "image_svg": "<svg/>"})

    def fake_auth(username, password, ckey, cvalue):
        calls.append(password)
        return answers.pop(0)
    monkeypatch.setattr(api_client, "authenticate", fake_auth)
    return answers, calls


def test_wrong_captcha_is_retried_then_ok(gdt):
    answers, calls = gdt
    answers += [{"ok": False, "message": "Mã captcha không đúng."}, {"ok": True, "token": "t"}]
    r = api_client.authenticate_auto("u", "p", delay=0)
    assert r["ok"] and r["attempts"] == 2 and r["reason"] == "ok" and len(calls) == 2


@pytest.mark.parametrize("msg,reason", [(WRONG_PW, "bad_credentials"), (LOCKED, "locked")])
def test_wrong_password_or_locked_is_never_retried(gdt, msg, reason):
    answers, calls = gdt
    answers += [{"ok": False, "message": msg}] * 6
    r = api_client.authenticate_auto("u", "p", delay=0)
    assert not r["ok"] and r["reason"] == reason and len(calls) == 1


def test_captcha_exhausted_reason(gdt):
    answers, calls = gdt
    answers += [{"ok": False, "message": "Mã captcha không đúng."}] * 3
    r = api_client.authenticate_auto("u", "p", max_retries=3, delay=0)
    assert not r["ok"] and r["reason"] == "captcha" and len(calls) == 3


# --- TokenProvider: chặn trước khi gọi Thuế ---------------------------------

class _Store:
    def __init__(self):
        self.tokens = {}

    def get_token(self, mst):
        return None

    def save_token(self, mst, token, expires):
        self.tokens[mst] = token

    def invalidate_token(self, mst):
        pass

    def login_lock(self, mst):
        import contextlib
        return contextlib.nullcontext()


@pytest.fixture()
def guard(monkeypatch):
    state = {"blocks": {}, "recorded": [], "cleared": [], "marked": [], "auth": []}

    def check(store, username, password):
        b = state["blocks"].get(username)
        return b if b and b["pw"] == password else None

    def record(store, username, mst, reason, message, password):
        state["recorded"].append((username, mst, reason))
        state["blocks"][username] = {"pw": password, "reason": reason, "message": message,
                                     "blocked_at": datetime(2026, 11, 10, 9, 0)}

    monkeypatch.setattr(gdt_login_guard, "check", check)
    monkeypatch.setattr(gdt_login_guard, "record", record)
    monkeypatch.setattr(gdt_login_guard, "clear", lambda store, u: state["cleared"].append(u))
    monkeypatch.setattr(invoice_job.weinvoice_bridge, "mark_login_failed",
                        lambda store, mst, u, msg: state["marked"].append(mst))
    return state


def _provider(monkeypatch, state, answer, env_pw="pw1", winv_pw=None):
    monkeypatch.setattr(invoice_job, "gdt_password", lambda company: env_pw or "")
    monkeypatch.setattr(invoice_job.weinvoice_bridge, "password_for", lambda store, mst, u: winv_pw)

    def fake_auto(username, password, max_retries=6):
        state["auth"].append(password)
        return answer
    monkeypatch.setattr(invoice_job.api_client, "authenticate_auto", fake_auto)
    monkeypatch.setattr(invoice_job.api_client, "jwt_expiry", lambda t: None)
    company = {"company_mst": "3600823561", "gdt_username": "3600823561-U002"}
    return invoice_job.TokenProvider(_Store(), company, Counter())


def test_wrong_password_blocks_next_runs_without_calling_gdt(monkeypatch, guard):
    bad = {"ok": False, "message": WRONG_PW, "attempts": 1, "reason": "bad_credentials"}
    with pytest.raises(invoice_job.LoginError):
        _provider(monkeypatch, guard, bad).get()
    assert guard["recorded"] == [("3600823561-U002", "3600823561", "bad_credentials")]
    # lượt sau (tick / sync-lists / worker), cùng mật khẩu → không gọi Thuế
    for _ in range(5):
        with pytest.raises(invoice_job.LoginError, match="Không đăng nhập"):
            _provider(monkeypatch, guard, bad).get()
    assert guard["auth"] == ["pw1"]
    assert guard["marked"] == []          # mật khẩu từ .env → không đụng trạng thái WeInvoice


def test_updated_password_is_tried_again(monkeypatch, guard):
    bad = {"ok": False, "message": WRONG_PW, "attempts": 1, "reason": "bad_credentials"}
    with pytest.raises(invoice_job.LoginError):
        _provider(monkeypatch, guard, bad).get()
    good = {"ok": True, "token": "jwt", "message": "", "attempts": 1, "reason": "ok"}
    assert _provider(monkeypatch, guard, good, env_pw="pw2").get() == "jwt"
    assert guard["auth"] == ["pw1", "pw2"] and guard["cleared"] == ["3600823561-U002"]


def test_captcha_failure_does_not_block(monkeypatch, guard):
    cap = {"ok": False, "message": "Thử 6 lần không được", "attempts": 6, "reason": "captcha"}
    for _ in range(2):
        with pytest.raises(invoice_job.LoginError):
            _provider(monkeypatch, guard, cap).get()
    assert guard["recorded"] == [] and guard["auth"] == ["pw1", "pw1"]


def test_weinvoice_password_rejected_marks_connection_error(monkeypatch, guard):
    locked = {"ok": False, "message": LOCKED, "attempts": 1, "reason": "locked"}
    with pytest.raises(invoice_job.LoginError):
        _provider(monkeypatch, guard, locked, env_pw=None, winv_pw="winv").get()
    assert guard["recorded"][0][2] == "locked" and guard["marked"] == ["3600823561"]


# --- vân tay mật khẩu -------------------------------------------------------

def test_fingerprint_depends_on_user_and_password(monkeypatch):
    monkeypatch.setenv("WEINVOICE_SECRET_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    a = gdt_login_guard.fingerprint("u", "p")
    assert a == gdt_login_guard.fingerprint("u", "p")
    assert a != gdt_login_guard.fingerprint("u", "p2") != gdt_login_guard.fingerprint("u2", "p")
    assert "p" not in a and len(a) == 64


# --- internal API: mật khẩu đã bị từ chối thì không gửi lại ------------------

def test_internal_api_refuses_known_rejected_password(monkeypatch):
    import internal_api
    monkeypatch.setenv("SCRAPING_INTERNAL_API_KEY", "k-123")
    calls, recorded = [], []
    monkeypatch.setattr(internal_api, "_guard_store", lambda: object())
    blocked = {"reason": "locked", "message": LOCKED}
    monkeypatch.setattr(gdt_login_guard, "check", lambda s, u, p: blocked if p == "old" else None)
    monkeypatch.setattr(gdt_login_guard, "record", lambda s, u, m, r, msg, p: recorded.append((u, r)))
    monkeypatch.setattr(gdt_login_guard, "clear", lambda s, u: None)

    def fake_auth(username, password, max_retries=6):
        calls.append(password)
        return {"ok": False, "token": "", "message": WRONG_PW, "attempts": 1, "reason": "bad_credentials"}
    monkeypatch.setattr(internal_api.api_client, "authenticate_auto", fake_auth)
    c = internal_api.app.test_client()
    hdr = {"X-Api-Key": "k-123"}
    j = c.post("/internal/gdt/login-test", json={"mst": "3603512483", "password": "old"}, headers=hdr).get_json()
    assert j["reason"] == "rejected" and "khóa" in j["message"] and calls == []
    j = c.post("/internal/gdt/login-test", json={"mst": "3603512483", "password": "new"}, headers=hdr).get_json()
    assert j["reason"] == "rejected" and calls == ["new"] and recorded == [("3603512483", "bad_credentials")]
