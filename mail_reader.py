# mail_reader.py
"""
Đọc hộp mail hóa đơn qua IMAP (CHỈ ĐỌC) và bóc tách thứ job cần:
  - mail có phải mail hóa đơn không (theo nội dung — mail forward mất người gửi gốc);
  - file XML / PDF đính kèm (kể cả nằm trong .zip, hoặc trong mail được forward dạng đính kèm);
  - link tra cứu + mã tra cứu trong nội dung (mail chỉ có link, không có file).

Không đánh dấu đã đọc (EXAMINE + BODY.PEEK), không xóa/di chuyển mail.
KHÔNG bao giờ tự mở link trong mail: link chỉ được ghi lại, việc tải do adapter của
cổng tra cứu đã biết làm (link lạ có thể là link theo dõi / hủy đăng ký...).
"""
from __future__ import annotations

import base64
import email
import html
import imaplib
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import PurePath
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger("mail_reader")

MAX_FILE_BYTES = 20 * 1024 * 1024      # bỏ file đính kèm / file trong zip lớn hơn (chặn zip bomb)

_KEYWORDS = ("hoa don", "invoice", "hddt", "ma tra cuu", "tra cuu hoa don", "einvoice", "e-invoice")
_CODE_LABEL_RE = re.compile(
    r"(m[ãa]\s*(?:s[ốo]\s*)?(?:tra\s*c[ứu]u(?:\s*h[óo]a\s*[đd][ơo]n|\s*nhanh)?|nh[ậa]n\s*h[óo]a\s*[đd][ơo]n"
    r"|b[íi]\s*m[ậa]t|b[ảa]o\s*m[ậa]t|ki[ểe]m\s*tra)"
    r"|s[ốo]\s*b[ảa]o\s*m[ậa]t|m[ãa]\s*tc|fkey|lookup\s*code|search\s*key)"
    r"\s*(?:\([^)]{0,30}\))?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-\*\.]{4,59})", re.I)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
# Tham số trên link tra cứu thường chứa mã (so khớp chữ thường)
_URL_CODE_PARAMS = ("lookupcode", "fkey", "key", "matracuu", "ma_tra_cuu", "mtc", "code", "sobaomat",
                    "transactionid", "invoiceguid", "searchkey", "secureid", "keysearch", "maktra", "id")
# MST người bán ghi trong mail ("MST người bán: 0313217924" — M-Invoice; "Mã số thuế đơn vị phát hành
# hóa đơn: 0106050554-001" — Vingroup). Chỉ nhãn nói rõ là bên bán, không lấy "Mã số thuế" trơn
# (mail thường in cả MST người mua).
_SELLER_MST_RE = re.compile(
    r"(?:mst|m[ãa]\s*s[ốo]\s*thu[ếe])\s*(?:c[ủu]a\s*)?"
    r"(?:ng[ưu][ờo]i\s*b[áa]n|[đd][ơo]n\s*v[ịi]\s*(?:b[áa]n|ph[áa]t\s*h[àa]nh(?:\s*h[óo]a\s*[đd][ơo]n)?))"
    r"\s*[:：]?\s*(\d{10}(?:-\d{3})?)\b", re.I)
# Link không phải cổng tra cứu: theo dõi mở mail, ảnh, mạng xã hội, chính công ty mình
_SKIP_HOST_PARTS = ("w3.org", "webcashvietnam.com", "schemas.microsoft.com", "google.com", "gstatic",
                    "googleusercontent", "googleapis", "facebook.com", "youtube.com", "zalo.me",
                    "linkedin.com", "awstrack.me", "sendgrid.net", "staticflickr.com", "mailchimp",
                    "list-manage.com", "microsoft.com", "apple.com", "twitter.com", "tiktok.com")
_SKIP_URL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".ico", ".woff", ".woff2")


def fold(s: str) -> str:
    """Chữ thường, bỏ dấu tiếng Việt — để so từ khóa."""
    s = unicodedata.normalize("NFD", s or "").lower()
    return "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def _decode(v) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:  # noqa: BLE001
        return str(v)


# ---------------------------------------------------------------------------
# Bóc tách 1 mail
# ---------------------------------------------------------------------------

@dataclass
class MailFile:
    filename: str
    kind: str          # 'xml' | 'pdf'
    data: bytes
    container: str = ""  # tên file .zip chứa nó (nếu có)


@dataclass
class MailLink:
    url: str
    host: str
    code: str | None = None


@dataclass
class ParsedMail:
    message_id: str
    subject: str
    from_addr: str
    received_at: datetime | None
    text: str                                       # nội dung đã bỏ thẻ HTML
    raw_text: str = ""                              # nội dung gốc (HTML) — để lấy link trong href
    files: list = field(default_factory=list)       # [MailFile]
    links: list = field(default_factory=list)       # [MailLink]
    codes: list = field(default_factory=list)       # mã tra cứu ghi trong nội dung
    seller_msts: list = field(default_factory=list) # MST người bán ghi trong nội dung
    attachment_names: list = field(default_factory=list)

    @property
    def is_invoice(self) -> bool:
        return is_invoice_mail(self.subject, self.text, self.attachment_names)


def is_invoice_mail(subject: str, text: str, attachment_names: list) -> bool:
    """
    Mail hóa đơn? Theo nội dung, không theo người gửi (mail forward mất người gửi gốc):
    có XML đính kèm, tiêu đề có từ khóa HĐ, PDF/ZIP kèm nội dung nói về HĐ, hoặc có mã tra cứu.
    """
    exts = {PurePath(n).suffix.lower() for n in attachment_names}
    subj, body = fold(subject), fold(text)
    if ".xml" in exts or any(k in subj for k in _KEYWORDS):
        return True
    if exts & {".pdf", ".zip"} and any(k in body for k in _KEYWORDS):
        return True
    return bool(_CODE_LABEL_RE.search(text))


def _files_from_part(filename: str, data: bytes) -> list:
    ext = PurePath(filename).suffix.lower()
    if len(data) > MAX_FILE_BYTES:
        return []
    if ext in (".xml", ".pdf"):
        return [MailFile(filename, ext[1:], data)]
    if ext == ".zip":
        out = []
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError, ValueError):
            return []
        for info in z.infolist():
            inner_ext = PurePath(info.filename).suffix.lower()
            if inner_ext in (".xml", ".pdf") and 0 < info.file_size <= MAX_FILE_BYTES:
                try:
                    out.append(MailFile(PurePath(info.filename).name, inner_ext[1:], z.read(info), filename))
                except Exception as e:  # noqa: BLE001  có mật khẩu / hỏng / kiểu nén lạ → chỉ bỏ file này
                    log.warning("Bỏ %s trong %s: %s", info.filename, filename, e)
        return out
    return []


def extract_links(text: str) -> list:
    """Link có thể là cổng tra cứu (bỏ link theo dõi/ảnh/mạng xã hội), kèm mã nếu nằm trên URL."""
    seen, out = set(), []
    for raw in _URL_RE.findall(text):
        # &nbsp; dính sau link thành \xa0 sau unescape → cắt ở khoảng trắng đầu tiên
        url = html.unescape(raw).split(maxsplit=1)[0].rstrip(".,;")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host or any(p in host for p in _SKIP_HOST_PARTS):
            continue
        if parts.path.lower().endswith(_SKIP_URL_EXT) or url in seen:
            continue
        seen.add(url)
        code = next((v.strip() for k, v in parse_qsl(parts.query, keep_blank_values=False)
                     if k.lower() in _URL_CODE_PARAMS and len(v.strip()) >= 5), None)
        out.append(MailLink(url, host, code))
    return out


def extract_codes(text: str) -> list:
    """Mã tra cứu ghi trong nội dung ("Mã tra cứu: ABC123", "Mã số bí mật: ...")."""
    out = []
    for m in _CODE_LABEL_RE.finditer(text):
        code = m.group(2).strip(".-")
        if code not in out and not code.isalpha():          # bỏ chữ thường gặp (vd "hoadon")
            out.append(code)
    return out


def extract_seller_msts(text: str) -> list:
    """MST người bán ghi trong nội dung (cổng tra theo MST bán + mã, vd M-Invoice)."""
    return list(dict.fromkeys(_SELLER_MST_RE.findall(text or "")))


def parse_message(raw: bytes, _depth: int = 0) -> ParsedMail:
    msg = email.message_from_bytes(raw)
    texts, raws, files, names = [], [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = _decode(part.get_filename()) if part.get_filename() else ""
        ctype = part.get_content_type()
        # Mail gốc được forward dạng đính kèm nhưng mã hóa (base64 / file .eml): walk() không
        # đi vào được → tự parse mail bên trong
        if (ctype == "message/rfc822" or fn.lower().endswith(".eml")) and _depth < 3:
            data = part.get_payload(decode=True) or b""
            if data:
                inner = parse_message(data, _depth + 1)
                files.extend(inner.files)
                names.extend(inner.attachment_names)
                texts.append(inner.text)
                raws.append(inner.raw_text)
            continue
        if fn:
            names.append(fn)
            files.extend(_files_from_part(fn, part.get_payload(decode=True) or b""))
            continue
        if ctype in ("text/plain", "text/html"):
            try:
                body = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", "replace")
            except (LookupError, AttributeError):
                continue
            raws.append(body)                                   # link nằm trong href của HTML
            texts.append(html_to_text(body) if ctype == "text/html" else body)
        elif ctype in ("application/xml", "text/xml"):           # XML đính kèm không có tên file
            data = part.get_payload(decode=True) or b""
            if data and len(data) <= MAX_FILE_BYTES:
                files.append(MailFile("noname.xml", "xml", data))
                names.append("noname.xml")
    text = "\n".join(texts)
    raw_text = "\n".join(raws)
    try:
        received = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    except (TypeError, ValueError):
        received = None
    return ParsedMail(
        message_id=(msg.get("Message-ID") or "").strip(),
        subject=_decode(msg.get("Subject")),
        from_addr=parseaddr(_decode(msg.get("From")))[1],
        received_at=received,
        text=text,
        raw_text=raw_text,
        files=files,
        links=extract_links(raw_text),
        codes=extract_codes(text),
        seller_msts=extract_seller_msts(text),
        attachment_names=names,
    )


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

class MailError(Exception):
    pass


class MailLoginRefused(MailError):
    """Máy chủ IMAP từ chối đăng nhập (sai mật khẩu / App Password, IMAP bị tắt...)."""


class MailAuthExpired(MailError):
    """Microsoft trả invalid_grant: refresh token bị thu hồi / hết hạn → người dùng phải đăng nhập lại."""


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(d: date) -> str:
    """Ngày cho IMAP SEARCH (vd 07-Oct-2026). Không dùng strftime('%b'): phụ thuộc locale máy."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def encode_folder(name: str) -> str:
    """Tên thư mục → modified UTF-7 (RFC 3501) để gửi IMAP, vd 'Hóa đơn' → 'H&APM-a &AREBoQ-n'."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def xoauth2_string(user: str, token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


# --- Lấy access token OAuth2 (gọi HTTP thường, không cần thư viện riêng) -------

def _token_response(url: str, data: dict, provider: str) -> dict:
    import requests
    try:
        r = requests.post(url, data=data, timeout=30)
    except requests.RequestException as e:
        raise MailError(f"Không lấy được token {provider}: {e}") from e
    try:
        js = r.json()
    except ValueError:
        js = {}
    if r.status_code != 200 or not js.get("access_token"):
        # không in client_secret / refresh_token; chỉ in mã lỗi server trả về
        msg = (f"{provider} từ chối cấp token (HTTP {r.status_code}): "
               f"{js.get('error')} {str(js.get('error_description') or '')[:200]}")
        raise (MailAuthExpired if js.get("error") == "invalid_grant" else MailError)(msg)
    return js


def _token_request(url: str, data: dict, provider: str) -> str:
    return _token_response(url, data, provider)["access_token"]


MS_DELEGATED_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_DELEGATED_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"


def microsoft_refresh_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str | None]:
    """Microsoft (ủy quyền của người dùng, hộp mail WeInvoice): refresh token → (access token, refresh token mới).

    Refresh token mới = None khi Microsoft không cấp lại (giữ cái cũ). invalid_grant (token bị thu hồi,
    hết hạn, đổi mật khẩu) → MailAuthExpired; lỗi khác → MailError. Không bao giờ in token / client secret.
    """
    js = _token_response(MS_DELEGATED_TOKEN_URL,
                         {"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
                          "refresh_token": refresh_token, "scope": MS_DELEGATED_SCOPE},
                         "Microsoft")
    new_rt = js.get("refresh_token")
    return js["access_token"], (new_rt if new_rt and new_rt != refresh_token else None)


def microsoft_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Microsoft 365: client credentials (app-only), quyền IMAP.AccessAsApp."""
    return _token_request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
         "scope": "https://outlook.office365.com/.default"},
        "Microsoft")


def google_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Gmail / Google Workspace: đổi refresh token (đã cấp quyền 1 lần) lấy access token."""
    return _token_request(
        "https://oauth2.googleapis.com/token",
        {"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
         "refresh_token": refresh_token},
        "Google")


TLS_MODES = ("SSL", "STARTTLS")
_IMAP_ERROR = imaplib.IMAP4.error      # giữ tham chiếu: test thay imaplib.IMAP4 bằng lớp giả


class ImapMailbox:
    """
    Hộp mail IMAP chỉ đọc. Dùng: with ImapMailbox(...) as mb: mb.message_ids_since(7) / mb.fetch(uid)

    auth:
      'password'                       đăng nhập user + mật khẩu (mail công ty, App Password của Gmail/Yahoo)
      'oauth2_microsoft' / 'oauth2_google'   AUTHENTICATE XOAUTH2 với token lấy từ `token_provider()`
    tls_mode: 'SSL' (IMAPS, cổng 993 — mặc định) | 'STARTTLS' (IMAP thường rồi nâng TLS, thường cổng 143).
    label: phần trước '/' của `name` (khóa `email_messages.mailbox`); mặc định = user đăng nhập.
    """

    def __init__(self, host: str, port: int, user: str, password: str | None = None,
                 folder: str = "INBOX", timeout: int = 60, auth: str = "password", token_provider=None,
                 tls_mode: str = "SSL", label: str | None = None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.folder, self.timeout = folder, timeout
        self.auth, self.token_provider = auth, token_provider
        self.tls_mode = (tls_mode or "SSL").upper()
        if self.tls_mode not in TLS_MODES:
            raise MailError(f"tls_mode {tls_mode!r} không hợp lệ — chọn SSL hoặc STARTTLS")
        self.label = label
        self.conn = None

    @property
    def name(self) -> str:
        return f"{self.label or self.user}/{self.folder}"

    def _login(self) -> None:
        if self.auth == "password":
            self.conn.login(self.user, self.password)
        else:
            token = self.token_provider()
            self.conn.authenticate("XOAUTH2", lambda _challenge: xoauth2_string(self.user, token))

    def connect(self) -> "ImapMailbox":
        """Kết nối + đăng nhập (chưa mở thư mục).

        Máy chủ từ chối đăng nhập → MailLoginRefused; mạng / TLS lỗi → MailError;
        lỗi lấy token (MailAuthExpired / MailError) đi thẳng ra ngoài.
        """
        import ssl
        where = f"{self.host}:{self.port}"
        try:
            if self.tls_mode == "STARTTLS":
                self.conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
                self.conn.starttls(ssl_context=ssl.create_default_context())
            else:
                self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except (_IMAP_ERROR, OSError) as e:
            self._close()
            raise MailError(f"Không kết nối được máy chủ mail {where} ({self.tls_mode}): {e}") from e
        try:
            self._login()
        except _IMAP_ERROR as e:
            self._close()
            raise MailLoginRefused(f"Máy chủ mail {where} từ chối đăng nhập ({self.user}): {_imap_text(e)}") from e
        except OSError as e:
            self._close()
            raise MailError(f"Mất kết nối máy chủ mail {where} khi đăng nhập ({self.user}): {e}") from e
        except MailError:
            self._close()
            raise
        return self

    def select_readonly(self, folder: str) -> bool:
        """EXAMINE 1 thư mục (chỉ đọc). Không có / không mở được → False."""
        try:
            typ, _ = self.conn.select(f'"{encode_folder(folder)}"', readonly=True)
        except _IMAP_ERROR:
            return False
        return typ == "OK"

    def __enter__(self):
        self.connect()
        typ, data = self.conn.select(f'"{encode_folder(self.folder)}"', readonly=True)    # EXAMINE
        if typ != "OK":
            self._close()
            raise MailError(f"Không mở được thư mục {self.folder!r}: {data}")
        return self

    def _close(self):
        try:
            if self.conn is not None:
                self.conn.logout()
        except Exception:  # noqa: BLE001
            pass

    def __exit__(self, *exc):
        self._close()

    def _uid(self, *args):
        typ, data = self.conn.uid(*args)
        if typ != "OK":
            raise MailError(f"IMAP {args[0]} lỗi: {data}")
        return data

    def message_ids_since(self, days: int, today: date | None = None) -> list:
        """[(uid, message_id)] của mail nhận trong `days` ngày gần nhất — chỉ đọc header."""
        since = imap_date((today or date.today()) - timedelta(days=days))
        uids = self._uid("search", None, "SINCE", since)[0].split()
        out = []
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i:i + 100])
            data = self._uid("fetch", chunk, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for item in data:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                mid = email.message_from_bytes(item[1]).get("Message-ID", "").strip()
                if m:
                    out.append((m.group(1).decode(), mid))
        return out

    def fetch(self, uid: str) -> bytes:
        data = self._uid("fetch", uid, "(BODY.PEEK[])")
        for item in data:
            if isinstance(item, tuple):
                return item[1]
        raise MailError(f"Không tải được mail UID {uid}")


def _imap_text(e: Exception) -> str:
    """Câu máy chủ IMAP trả (imaplib bọc bytes trong str: "b'[AUTHENTICATIONFAILED] ...'")."""
    text = str(e)
    m = re.fullmatch(r"b(['\"])(.*)\1", text, re.S)
    return (m.group(2) if m else text)[:300]


def check_login(host: str, port: int, user: str, folders: list, password: str | None = None,
                tls_mode: str = "SSL", auth: str = "password", token_provider=None,
                timeout: int = 30) -> tuple[list, list]:
    """Đăng nhập thử + EXAMINE từng thư mục → (thư mục mở được, thư mục không có). Không đọc mail nào.

    Lỗi đăng nhập ném như `ImapMailbox.connect` (MailLoginRefused / MailAuthExpired / MailError).
    """
    mb = ImapMailbox(host, port, user, password, timeout=timeout, auth=auth, token_provider=token_provider,
                     tls_mode=tls_mode)
    mb.connect()
    opened, missing = [], []
    try:
        for f in folders:
            (opened if mb.select_readonly(f) else missing).append(f)
    finally:
        mb._close()
    return opened, missing
