"""Hộp mail người dùng kết nối trên WeInvoice — offline, không IMAP / Microsoft / DB thật.

Sai ở đây thì: lưu bí mật sai định dạng (backend không giải mã được), báo "đã kết nối" cho hộp mail
không đăng nhập được, gắn mail của người này vào HĐ của MST người khác, hoặc 1 hộp lỗi làm dừng cả lượt.

Run::

    python -X utf8 -m pytest tests/test_weinvoice_mailbox.py -q
"""

from __future__ import annotations

import base64
import imaplib
import sys
from collections import Counter
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "web", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mail_ingest  # noqa: E402
import mail_reader  # noqa: E402
import weinvoice_bridge  # noqa: E402

KEY = bytes(range(32))
IMAP_ERROR = imaplib.IMAP4.error


# ---------------------------------------------------------------- encrypt

def _encrypt_like_java(plain: str, key: bytes = KEY, iv: bytes = b"\x01" * 12) -> str:
    """'v1:' + base64(iv(12) || ciphertext || tag(16)) — y như WeInvoiceSecretCipher.encrypt."""
    return "v1:" + base64.b64encode(iv + AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)).decode()


def test_encrypt_matches_java_format_and_round_trips():
    iv = b"\x01" * 12
    assert weinvoice_bridge.encrypt("refresh-Tôken/1", KEY, iv=iv) == _encrypt_like_java("refresh-Tôken/1", iv=iv)
    enc = weinvoice_bridge.encrypt("abcd efgh ijkl mnop", KEY)
    assert enc.startswith("v1:") and "abcd" not in enc
    assert weinvoice_bridge.decrypt(enc, KEY) == "abcd efgh ijkl mnop"
    assert enc != weinvoice_bridge.encrypt("abcd efgh ijkl mnop", KEY)       # iv ngẫu nhiên mỗi lần


def test_encrypt_uses_env_key_and_refuses_without_key(monkeypatch):
    monkeypatch.setenv("WEINVOICE_SECRET_KEY", base64.b64encode(KEY).decode())
    assert weinvoice_bridge.decrypt(weinvoice_bridge.encrypt("x")) == "x"
    monkeypatch.delenv("WEINVOICE_SECRET_KEY")
    with pytest.raises(ValueError):
        weinvoice_bridge.encrypt("x")


def test_update_mailbox_refuses_plaintext_secret():
    with pytest.raises(ValueError):
        weinvoice_bridge.update_mailbox_scan(object(), 1, "CONNECTED", None, secret_enc="plain-token")


# ---------------------------------------------------------------- IMAP giả

class FakeImap:
    """imaplib.IMAP4 / IMAP4_SSL giả. Mật khẩu 'right' đăng nhập được; thư mục có sẵn: INBOX, HDDT."""
    instances: list = []
    folders = {'"INBOX"', '"HDDT"'}

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout, self.calls = host, port, timeout, []
        FakeImap.instances.append(self)
        if host == "down.example":
            raise OSError("connection refused")

    def starttls(self, ssl_context=None):
        self.calls.append(("starttls", ssl_context is not None))

    def login(self, user, pw):
        self.calls.append(("login", user))
        if pw != "right":
            raise IMAP_ERROR(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    def authenticate(self, mech, cb):
        self.calls.append(("authenticate", mech, cb(b"")))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return ("OK", [b"3"]) if folder in self.folders else ("NO", [b"Mailbox doesn't exist"])

    def logout(self):
        self.calls.append(("logout",))


@pytest.fixture
def fake_imap(monkeypatch):
    FakeImap.instances = []
    monkeypatch.setattr(mail_reader.imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(mail_reader.imaplib, "IMAP4", FakeImap)
    return FakeImap


def test_starttls_connects_plain_then_upgrades(fake_imap):
    opened, missing = mail_reader.check_login("mail.cty.vn", 143, "u@cty.vn", ["INBOX", "Không có"],
                                              password="right", tls_mode="STARTTLS")
    assert opened == ["INBOX"] and missing == ["Không có"]
    conn = fake_imap.instances[0]
    assert conn.calls[0] == ("starttls", True) and conn.calls[1] == ("login", "u@cty.vn")
    assert ("select", '"INBOX"', True) in conn.calls                     # chỉ đọc
    assert conn.calls[-1] == ("logout",)


def test_login_refused_is_its_own_error(fake_imap):
    with pytest.raises(mail_reader.MailLoginRefused) as ei:
        mail_reader.check_login("imap.gmail.com", 993, "u@gmail.com", ["INBOX"], password="wrong")
    assert "AUTHENTICATIONFAILED" in str(ei.value) and "wrong" not in str(ei.value)
    assert not isinstance(mail_reader.MailError("x"), mail_reader.MailLoginRefused)


def test_bad_tls_mode_rejected():
    with pytest.raises(mail_reader.MailError):
        mail_reader.ImapMailbox("h", 993, "u", "p", tls_mode="PLAIN")


# ---------------------------------------------------------------- token Microsoft (ủy quyền)

class _Resp:
    def __init__(self, code, js):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


def test_microsoft_refresh_token_scope_rotation_and_invalid_grant(monkeypatch):
    import requests
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data, timeout))
        if data["refresh_token"] == "RT-REVOKED":
            return _Resp(400, {"error": "invalid_grant", "error_description": "AADSTS70008: expired"})
        if data["refresh_token"] == "RT-BADAPP":
            return _Resp(401, {"error": "invalid_client", "error_description": "bad secret"})
        if data["refresh_token"] == "RT-SAME":
            return _Resp(200, {"access_token": "AT", "refresh_token": "RT-SAME"})
        return _Resp(200, {"access_token": "AT", "refresh_token": "RT-NEW"})

    monkeypatch.setattr(requests, "post", fake_post)
    assert mail_reader.microsoft_refresh_token("cid", "csec", "RT-OLD") == ("AT", "RT-NEW")
    url, data, timeout = calls[0]
    assert url == "https://login.microsoftonline.com/common/oauth2/v2.0/token" and timeout == 30
    assert data["grant_type"] == "refresh_token"
    assert data["scope"] == "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
    assert mail_reader.microsoft_refresh_token("cid", "csec", "RT-SAME") == ("AT", None)
    with pytest.raises(mail_reader.MailAuthExpired) as ei:
        mail_reader.microsoft_refresh_token("cid", "csec", "RT-REVOKED")
    assert "invalid_grant" in str(ei.value) and "RT-REVOKED" not in str(ei.value) and "csec" not in str(ei.value)
    with pytest.raises(mail_reader.MailError) as ei:
        mail_reader.microsoft_refresh_token("cid", "csec", "RT-BADAPP")
    assert not isinstance(ei.value, mail_reader.MailAuthExpired)


# ---------------------------------------------------------------- POST /internal/mail/login-test

@pytest.fixture
def client(monkeypatch, fake_imap):
    import internal_api
    monkeypatch.setenv("SCRAPING_INTERNAL_API_KEY", "k-123")
    monkeypatch.delenv("WEINVOICE_MS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WEINVOICE_MS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(internal_api, "_guard_store", lambda: None)
    return internal_api.app.test_client()


BODY = {"auth": "APP_PASSWORD", "host": "imap.gmail.com", "port": 993, "tls_mode": "SSL",
        "login_id": "ketoan@gmail.com", "password": "right", "folders": ["INBOX", "HDDT"]}


def _mail_post(client, body, key="k-123"):
    return client.post("/internal/mail/login-test", json=body, headers={"X-Api-Key": key} if key else {})


def test_mail_login_ok(client, fake_imap):
    r = _mail_post(client, BODY)
    assert r.status_code == 200
    j = r.get_json()
    assert j == {"ok": True, "message": "", "missing_folders": [], "reason": "ok"}
    assert fake_imap.instances[0].calls[0] == ("login", "ketoan@gmail.com")


def test_mail_login_missing_folder_still_ok(client):
    j = _mail_post(client, {**BODY, "folders": ["INBOX", "Hóa đơn"]}).get_json()
    assert j["ok"] is True and j["missing_folders"] == ["Hóa đơn"] and "Hóa đơn" in j["message"]


def test_mail_login_no_folder_opened_is_not_ok(client):
    j = _mail_post(client, {**BODY, "folders": ["Nope"]}).get_json()
    assert j["ok"] is False and j["missing_folders"] == ["Nope"]


def test_mail_login_refused_never_echoes_password(client):
    r = _mail_post(client, {**BODY, "password": "sai-mat-khau-123"})
    j = r.get_json()
    assert j["ok"] is False and j["reason"] == "rejected"
    assert "AUTHENTICATIONFAILED" in j["message"] and "Mật khẩu ứng dụng" in j["message"]
    assert "sai-mat-khau-123" not in r.get_data(as_text=True)


def test_mail_login_network_error_is_unavailable(client):
    j = _mail_post(client, {**BODY, "host": "down.example", "auth": "PASSWORD"}).get_json()
    assert j["ok"] is False and j["reason"] == "unavailable" and "down.example" in j["message"]


def test_mail_login_starttls(client, fake_imap):
    j = _mail_post(client, {**BODY, "auth": "PASSWORD", "host": "mail.cty.vn", "port": 143,
                            "tls_mode": "STARTTLS", "folders": []}).get_json()
    assert j["ok"] is True                                                # folders trống → INBOX
    assert fake_imap.instances[0].calls[0] == ("starttls", True)


def test_mail_login_auth_and_validation(client):
    assert _mail_post(client, BODY, key=None).status_code == 401
    assert _mail_post(client, BODY, key="bad").status_code == 401
    r = _mail_post(client, {**BODY, "password": "", "host": "", "tls_mode": "TLS13"})
    assert r.status_code == 400
    assert all(k in r.get_json()["message"] for k in ("host", "password", "tls_mode"))
    assert _mail_post(client, {**BODY, "auth": "OAUTH_MS", "password": None}).status_code == 400  # thiếu refresh_token


def test_mail_login_oauth_needs_engine_config(client):
    j = _mail_post(client, {**BODY, "auth": "OAUTH_MS", "host": "outlook.office365.com",
                            "refresh_token": "RT"}).get_json()
    assert j["ok"] is False and j["reason"] == "config" and "WEINVOICE_MS_CLIENT_ID" in j["message"]


def test_mail_login_oauth_xoauth2_and_expired(client, fake_imap, monkeypatch):
    monkeypatch.setenv("WEINVOICE_MS_CLIENT_ID", "cid")
    monkeypatch.setenv("WEINVOICE_MS_CLIENT_SECRET", "csec")
    seen = []

    def fake_refresh(cid, csec, rt):
        seen.append((cid, csec, rt))
        if rt == "RT-REVOKED":
            raise mail_reader.MailAuthExpired("Microsoft từ chối cấp token (HTTP 400): invalid_grant")
        return "ACCESS", "RT-NEW"

    monkeypatch.setattr(mail_reader, "microsoft_refresh_token", fake_refresh)
    body = {**BODY, "auth": "OAUTH_MS", "host": "outlook.office365.com", "login_id": "a@cty.onmicrosoft.com",
            "password": None, "refresh_token": "RT-1", "folders": ["INBOX"]}
    r = _mail_post(client, body)
    assert r.get_json()["ok"] is True and seen == [("cid", "csec", "RT-1")]
    assert fake_imap.instances[0].calls[0] == ("authenticate", "XOAUTH2",
                                               b"user=a@cty.onmicrosoft.com\x01auth=Bearer ACCESS\x01\x01")
    assert "RT-NEW" not in r.get_data(as_text=True) and "ACCESS" not in r.get_data(as_text=True)
    j = _mail_post(client, {**body, "refresh_token": "RT-REVOKED"}).get_json()
    assert j["ok"] is False and j["reason"] == "expired"


# ---------------------------------------------------------------- mail_ingest giới hạn theo MST

import test_mail as tm  # noqa: E402  (store giả + dựng mail/XML của test_mail)

OTHER = "0319999999"


class _ScopedStore(tm._Store):
    """Store giả có lọc theo tài khoản mail như InvoiceStore (rematch / link của đúng hộp)."""

    def _account_of(self, email_id):
        return next(mb.split("/")[0] for (mb, _), e in self.emails.items() if e["id"] == email_id)

    def unmatched_email_files(self, mailboxes=None):
        rows = super().unmatched_email_files()
        return rows if mailboxes is None else [f for f in rows if self._account_of(f["email_id"]) in mailboxes]


def test_restricted_ingest_only_attaches_to_mailbox_msts(tmp_path):
    raw = tm._mail(attachments=[("hd.xml", tm._xml())])          # HĐ mua của OUR
    # Hộp của người dùng chỉ quản lý OTHER → XML của OUR là "không phải của mình", không lưu file
    store = _ScopedStore([tm._inv()])
    ing = mail_ingest.MailIngest(store, tmp_path, restrict_msts={OTHER})
    stats = Counter()
    ing.process("a@x/INBOX", mail_reader.parse_message(raw), stats)
    assert stats["xml_not_ours"] == 1 and not store.xml_saved and ing.filled == set()
    assert not (tmp_path / "mail").exists()
    # Hộp quản lý OUR → gắn vào HĐ của OUR, đếm 1 HĐ có file mới
    store = _ScopedStore([tm._inv()])
    ing = mail_ingest.MailIngest(store, tmp_path, restrict_msts={tm.OUR})
    ing.process("a@x/INBOX", mail_reader.parse_message(raw), Counter())
    assert store.xml_saved and ing.filled == {1}


def test_restricted_ingest_never_attaches_to_another_companys_invoice(tmp_path):
    # Cùng HĐ nhưng DB chỉ có bản của bên bán (công ty khác trong engine): hộp của OUR không được gắn vào đó
    inv_other = {**tm._inv(), "company_mst": "0100112233"}
    store = _ScopedStore([inv_other])
    stats = Counter()
    mail_ingest.MailIngest(store, tmp_path, restrict_msts={tm.OUR}).process(
        "a@x/INBOX", mail_reader.parse_message(tm._mail(attachments=[("hd.xml", tm._xml())])), stats)
    assert stats["xml_unmatched"] == 1 and not store.xml_saved


def test_default_ingest_unchanged_and_counts_filled(tmp_path):
    store = _ScopedStore([tm._inv()])
    box = tm._Box({"1": tm._mail(attachments=[("hd.xml", tm._xml()), ("hd.pdf", tm._pdf("C26TBV So 238805"))])})
    filled = mail_ingest.run(store, box, tmp_path, None, 7, Counter())
    assert filled == {1} and store.xml_saved and store.pdf_saved


def test_restricted_rematch_only_touches_own_mailbox_files(tmp_path):
    store = _ScopedStore([])
    raw = tm._mail(attachments=[("hd.xml", tm._xml())])
    mail_ingest.MailIngest(store, tmp_path).process("webcash@x/INBOX", mail_reader.parse_message(raw), Counter())
    assert store.files[0]["status"] == "unmatched"
    store.invoices[1] = tm._inv()                     # HĐ đã lên danh sách GDT
    stats = Counter()
    ing = mail_ingest.MailIngest(store, tmp_path, restrict_msts={tm.OUR}, mailboxes=["user@y"])
    ing.rematch(stats)
    assert stats["rematched"] == 0 and store.files[0]["status"] == "unmatched"   # file của hộp khác: không đụng
    ing = mail_ingest.MailIngest(store, tmp_path, restrict_msts={tm.OUR}, mailboxes=["webcash@x"])
    ing.rematch(stats)
    assert stats["rematched"] == 1 and ing.filled == {1}


def test_env_flow_skips_files_and_links_of_user_mailboxes(tmp_path, monkeypatch):
    """Luồng `.env` (Webcash) không khớp lại file / link của hộp mail người dùng WeInvoice."""
    import weinvoice_bridge
    calls = {}

    class _Store(_ScopedStore):
        def unmatched_email_files(self, mailboxes=None, exclude=None):
            calls["files"] = (mailboxes, exclude)
            return []

        def pending_email_links(self, max_attempts=3, mailboxes=None, exclude=None):
            calls["links"] = (mailboxes, exclude)
            return []

    monkeypatch.setattr(weinvoice_bridge, "user_mailbox_accounts", lambda store: ["user@y"])
    ing = mail_ingest.MailIngest(_Store([]), tmp_path)
    ing.rematch(Counter())
    ing.process_links(Counter())
    assert calls == {"files": (None, ["user@y"]), "links": (None, ["user@y"])}
    monkeypatch.setattr(weinvoice_bridge, "user_mailbox_accounts", lambda store: [])
    calls.clear()
    mail_ingest.MailIngest(_Store([]), tmp_path).rematch(Counter())
    assert calls["files"] == (None, None)                 # chưa có hộp người dùng → như cũ


# ---------------------------------------------------------------- worker: quét hộp mail

import weinvoice_worker as ww  # noqa: E402

MBX = {"mlbx_seq": 5, "owner_id": "user-1", "mlbx_kind": "GMAIL", "mail_addr": "KeToan@Gmail.com",
       "auth_type": "APP_PASSWORD", "imap_host": "imap.gmail.com", "imap_port": 993, "tls_mode": "SSL",
       "login_id": "ketoan@gmail.com", "secret_enc": "v1:xxx", "fldr_list": "INBOX, HDDT ,Nope",
       "cmpn_scope": "ONE", "cmpn_mst": tm.OUR, "conn_stts": "CONNECTED"}


class FakeMailbox:
    """mail_reader.ImapMailbox giả cho worker."""
    instances: list = []
    behaviour = "ok"            # ok | refused | expired | down

    def __init__(self, host, port, user, password=None, folder="INBOX", timeout=60, auth="password",
                 token_provider=None, tls_mode="SSL", label=None):
        self.kw = dict(host=host, port=port, user=user, password=password, auth=auth, tls_mode=tls_mode,
                       label=label)
        self.token_provider, self.folder, self.label = token_provider, folder, label
        self.closed = False
        FakeMailbox.instances.append(self)

    @property
    def name(self):
        return f"{self.label}/{self.folder}"

    def connect(self):
        if self.token_provider:
            self.token_provider()
        if self.behaviour == "refused":
            raise mail_reader.MailLoginRefused("Máy chủ mail imap.gmail.com:993 từ chối đăng nhập: Invalid credentials")
        if self.behaviour == "down":
            raise mail_reader.MailError("Không kết nối được máy chủ mail")
        return self

    def select_readonly(self, folder):
        return folder in ("INBOX", "HDDT")

    def _close(self):
        self.closed = True


@pytest.fixture
def scan_env(monkeypatch):
    FakeMailbox.instances, FakeMailbox.behaviour = [], "ok"
    updates, runs = [], []
    monkeypatch.setattr(ww.mail_reader, "ImapMailbox", FakeMailbox)
    monkeypatch.setattr(ww.weinvoice_bridge, "decrypt", lambda v, key=None: "app-pass-16" if v else None)
    monkeypatch.setattr(ww.weinvoice_bridge, "encrypt", lambda v, key=None: "v1:ENC(" + v + ")")
    monkeypatch.setattr(ww.weinvoice_bridge, "mailbox_msts",
                        lambda store, m: {m["cmpn_mst"]} if m.get("cmpn_mst") else set())
    monkeypatch.setattr(ww.weinvoice_bridge, "update_mailbox_scan",
                        lambda store, mid, status, msg, cnt=None, scanned=False, secret_enc=None:
                        updates.append({"id": mid, "status": status, "msg": msg, "cnt": cnt,
                                        "scanned": scanned, "secret": secret_enc}))

    def fake_run(store, mb, out_dir, run_id, days, stats, limit=None, restrict_msts=None):
        runs.append(("run", mb.name, restrict_msts, days))
        stats["mail_new"] += 1
        return {10, 11} if mb.folder == "INBOX" else {11, 12}

    def fake_links(store, out_dir, run_id, stats, limit=None, restrict_msts=None, mailboxes=None):
        runs.append(("links", mailboxes, restrict_msts))
        return {13}

    monkeypatch.setattr(ww.mail_ingest, "run", fake_run)
    monkeypatch.setattr(ww.mail_ingest, "process_links", fake_links)
    return updates, runs


def test_scan_mailbox_ok_restricted_and_counted(scan_env, tmp_path):
    updates, runs = scan_env
    res = ww.scan_mailbox(object(), tmp_path, dict(MBX), days=10)
    assert res.status == "ok" and res.invoices == 4 and res.missing_folders == ["Nope"]
    assert runs == [("run", "ketoan@gmail.com/INBOX", {tm.OUR}, 10), ("run", "ketoan@gmail.com/HDDT", {tm.OUR}, 10),
                    ("links", ["ketoan@gmail.com"], {tm.OUR})]
    assert FakeMailbox.instances[0].kw["password"] == "app-pass-16" and FakeMailbox.instances[0].closed
    assert updates == [{"id": 5, "status": "CONNECTED", "msg": "Không có thư mục: Nope", "cnt": 4,
                        "scanned": True, "secret": None}]


@pytest.mark.parametrize("behaviour,status,expected_conn", [
    ("refused", "rejected", "ERROR"), ("down", "unavailable", None)])
def test_scan_mailbox_login_failures(scan_env, tmp_path, behaviour, status, expected_conn):
    updates, runs = scan_env
    FakeMailbox.behaviour = behaviour
    res = ww.scan_mailbox(object(), tmp_path, dict(MBX))
    assert res.status == status and runs == []
    assert updates[0]["status"] == expected_conn and updates[0]["scanned"] is False and updates[0]["msg"]


def test_scan_mailbox_microsoft_rotation_and_expiry(scan_env, tmp_path, monkeypatch):
    updates, _ = scan_env
    monkeypatch.setenv("WEINVOICE_MS_CLIENT_ID", "cid")
    monkeypatch.setenv("WEINVOICE_MS_CLIENT_SECRET", "csec")
    calls = []

    def fake_refresh(cid, csec, rt):
        calls.append(rt)
        if rt == "app-pass-16" and FakeMailbox.behaviour == "expired":
            raise mail_reader.MailAuthExpired("invalid_grant")
        return "ACCESS", "RT-ROTATED"

    monkeypatch.setattr(ww.mail_reader, "microsoft_refresh_token", fake_refresh)
    mbx = {**MBX, "mlbx_kind": "MICROSOFT", "auth_type": "OAUTH_MS", "fldr_list": "INBOX"}
    res = ww.scan_mailbox(object(), tmp_path, mbx)
    assert res.status == "ok" and calls == ["app-pass-16"]                 # đổi token 1 lần / lượt
    assert FakeMailbox.instances[0].kw["auth"] == "oauth2_microsoft"
    assert updates[-1]["status"] == "CONNECTED" and updates[-1]["secret"] == "v1:ENC(RT-ROTATED)"
    FakeMailbox.behaviour = "expired"
    res = ww.scan_mailbox(object(), tmp_path, mbx)
    assert res.status == "expired" and updates[-1]["status"] == "EXPIRED" and updates[-1]["secret"] is None


def test_scan_mailbox_microsoft_not_configured(scan_env, tmp_path, monkeypatch):
    updates, _ = scan_env
    monkeypatch.delenv("WEINVOICE_MS_CLIENT_ID", raising=False)
    res = ww.scan_mailbox(object(), tmp_path, {**MBX, "auth_type": "OAUTH_MS"})
    assert res.status == "config" and updates[0]["status"] is None and FakeMailbox.instances == []


def test_scan_mailbox_without_msts_does_not_log_in(scan_env, tmp_path):
    updates, _ = scan_env
    res = ww.scan_mailbox(object(), tmp_path, {**MBX, "cmpn_mst": None})
    assert res.status == "no_mst" and FakeMailbox.instances == [] and updates[0]["cnt"] == 0


class _RunStore:
    def __init__(self):
        self.runs, self.finished = [], []

    def start_run(self, command):
        self.runs.append(command)
        return len(self.runs)

    def finish_run(self, run_id, status, stats):
        self.finished.append((run_id, status, stats))


def _mail_worker(row):
    w = object.__new__(ww.Worker)
    from test_weinvoice_worker import FakeRequests
    w.reqs = FakeRequests(row)
    w.store = _RunStore()
    w.out_dir = Path(".")
    w.handlers = {"MAIL_SCAN": w.do_mail_scan}
    w._next_mail_check = 0.0
    return w


ROW = {"rqst_seq": 9, "owner_id": "user-1", "cmpn_mst": tm.OUR, "rqst_type": "MAIL_SCAN",
       "rqst_prmt": {"mailbox_id": 5}}


def test_mail_scan_request_done(scan_env, monkeypatch):
    monkeypatch.setattr(ww.weinvoice_bridge, "get_mailbox", lambda store, mid: dict(MBX) if mid == 5 else None)
    w = _mail_worker(dict(ROW))
    assert w.run_one() is True
    status, result, error, run_id = w.reqs.finished
    assert status == "PARTIAL" and error is None and run_id == 1               # thư mục "Nope" không có
    assert result["invoices"] == 4 and result["mailbox_id"] == 5 and result["missing_folders"] == ["Nope"]
    assert [s for s, _ in w.reqs.reports] == ["login", "scan", "scan", "scan", "scan", "done"]
    assert w.store.runs == ["weinvoice-mail"] and w.store.finished[0][1] == "partial"


def test_mail_scan_request_other_owner_or_login_refused(scan_env, monkeypatch):
    monkeypatch.setattr(ww.weinvoice_bridge, "get_mailbox", lambda store, mid: dict(MBX))
    w = _mail_worker({**ROW, "owner_id": "someone-else"})
    w.run_one()
    assert w.reqs.finished[0] == "FAILED" and w.reqs.finished[2] == "Không tìm thấy hộp mail"
    FakeMailbox.behaviour = "refused"
    w = _mail_worker(dict(ROW))
    w.run_one()
    assert w.reqs.finished[0] == "FAILED" and "từ chối đăng nhập" in w.reqs.finished[2]
    assert w.store.finished[0][1] == "error"
    w = _mail_worker({**ROW, "rqst_prmt": {}})
    w.run_one()
    assert w.reqs.finished[2] == "MAIL_SCAN thiếu mailbox_id"


def test_periodic_scan_continues_after_a_bad_mailbox(scan_env, monkeypatch):
    updates, _ = scan_env
    boxes = [{**MBX, "mlbx_seq": 1}, {**MBX, "mlbx_seq": 2}, {**MBX, "mlbx_seq": 3}]
    seen = {}
    monkeypatch.setattr(ww.weinvoice_bridge, "list_mailboxes", lambda store, st, scanned_before=None:
                        seen.setdefault("args", (st, scanned_before)) and boxes)
    monkeypatch.setattr(ww.weinvoice_bridge, "claim_mailbox_scan", lambda store, mid, before: mid != 3)
    real = ww.scan_mailbox

    def flaky(store, out_dir, mbx, run_id=None, days=None, progress=None):
        if mbx["mlbx_seq"] == 1:
            raise RuntimeError("boom")
        return real(store, out_dir, mbx, run_id, days, progress)

    monkeypatch.setattr(ww, "scan_mailbox", flaky)
    monkeypatch.setenv("WEINVOICE_MAIL_SCAN_MINUTES", "30")
    w = _mail_worker(None)
    assert w.maybe_scan_mailboxes() == 2                       # #1 lỗi, #2 quét, #3 vừa có lượt khác quét
    assert seen["args"][0] == "CONNECTED" and len(seen["args"][1]) == 14
    assert [u["id"] for u in updates] == [2]
    assert [f[1] for f in w.store.finished] == ["error", "partial"]
    assert w.maybe_scan_mailboxes() == 0                       # chưa tới lần kiểm tra sau


def test_periodic_scan_disabled(monkeypatch):
    monkeypatch.setenv("WEINVOICE_MAIL_SCAN_MINUTES", "0")
    monkeypatch.setattr(ww.weinvoice_bridge, "list_mailboxes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("không được quét")))
    assert _mail_worker(None).maybe_scan_mailboxes() == 0


def test_mail_scan_days_bounds(monkeypatch):
    monkeypatch.delenv("WEINVOICE_MAIL_DAYS", raising=False)
    assert ww.mail_scan_days() == 30 and ww.mail_scan_days(5000) == 365 and ww.mail_scan_days("x") == 30
    monkeypatch.setenv("WEINVOICE_MAIL_DAYS", "7")
    assert ww.mail_scan_days() == 7 and ww.mail_scan_days(3) == 3
