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
    r"(m[ãa]\s*(?:s[ốo]\s*)?(?:tra\s*c[ứu]u(?:\s*h[óo]a\s*[đd][ơo]n)?|nh[ậa]n\s*h[óo]a\s*[đd][ơo]n"
    r"|b[íi]\s*m[ậa]t|b[ảa]o\s*m[ậa]t)"
    r"|m[ãa]\s*tc|fkey|lookup\s*code|search\s*key)"
    r"\s*(?:\([^)]{0,30}\))?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-\*\.]{4,59})", re.I)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
# Tham số trên link tra cứu thường chứa mã (so khớp chữ thường)
_URL_CODE_PARAMS = ("lookupcode", "fkey", "key", "matracuu", "ma_tra_cuu", "mtc", "code", "sobaomat",
                    "transactionid", "invoiceguid", "searchkey", "secureid", "keysearch", "id")
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
        url = html.unescape(raw).rstrip(".,;")
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
        attachment_names=names,
    )


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

class MailError(Exception):
    pass


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

def _token_request(url: str, data: dict, provider: str) -> str:
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
        raise MailError(f"{provider} từ chối cấp token (HTTP {r.status_code}): "
                        f"{js.get('error')} {str(js.get('error_description') or '')[:200]}")
    return js["access_token"]


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


class ImapMailbox:
    """
    Hộp mail IMAP chỉ đọc. Dùng: with ImapMailbox(...) as mb: mb.message_ids_since(7) / mb.fetch(uid)

    auth:
      'password'                       đăng nhập user + mật khẩu (mail công ty, App Password của Gmail/Yahoo)
      'oauth2_microsoft' / 'oauth2_google'   AUTHENTICATE XOAUTH2 với token lấy từ `token_provider()`
    """

    def __init__(self, host: str, port: int, user: str, password: str | None = None,
                 folder: str = "INBOX", timeout: int = 60, auth: str = "password", token_provider=None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.folder, self.timeout = folder, timeout
        self.auth, self.token_provider = auth, token_provider
        self.conn = None

    @property
    def name(self) -> str:
        return f"{self.user}/{self.folder}"

    def _login(self) -> None:
        if self.auth == "password":
            self.conn.login(self.user, self.password)
        else:
            token = self.token_provider()
            self.conn.authenticate("XOAUTH2", lambda _challenge: xoauth2_string(self.user, token))

    def __enter__(self):
        try:
            self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
            self._login()
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"Không đăng nhập được IMAP {self.host}:{self.port} ({self.user}, "
                            f"auth={self.auth}): {e}") from e
        typ, data = self.conn.select(f'"{encode_folder(self.folder)}"', readonly=True)    # EXAMINE
        if typ != "OK":
            raise MailError(f"Không mở được thư mục {self.folder!r}: {data}")
        return self

    def __exit__(self, *exc):
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass

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
