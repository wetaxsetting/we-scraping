# pdf_providers/minvoice.py
"""
M-invoice — MSTTCGP 0106026495 (cả chi nhánh 0106026495-001, registry tự quy về công ty mẹ).
Tra cứu: https://tracuuhoadon.minvoice.com.vn/tra-cuu-hoa-don

Mỗi người bán có subdomain riêng (<mst>.minvoice.com.vn / .minvoice.app) nhưng KHÔNG cần:
cổng tra cứu chung tra được HĐ của mọi người bán theo MST + số bảo mật. Không captcha
(form trên trang không có), không cookie. Khảo sát 10/2026 trên HĐ thật C26TCA-11001
(0104957749-001) và C26MBH-2703 (máy tính tiền, 0110097153):

    GET /api/Search/SearchInvoice?masothue=<MST người bán>&sobaomat=<mã>&type=PDF&inchuyendoi=false
      → 200 application/pdf                          : thành công (PDF server dựng sẵn, có chữ)
      → 500 JSON {"code":"5000","message":"Thông tin tra cứu không chính xác..."}
                                                       : sai MST/mã → 'no_pdf' (KHÔNG phải lỗi server)
      → 400 JSON (thiếu masothue)

MST người bán giữ hay bỏ dấu '-' đều được. Mã phân biệt hoa thường (viết thường → 5000).
Mã: TTKhac "Số bảo mật" (HĐ thường) hoặc "Mã tra cứu" (HĐ máy tính tiền), cả hai 16 ký tự hex.

Mail "THÔNG BÁO XUẤT HÓA ĐƠN SỐ 2848" (27/10, user kiểm tay): link chỉ là trang chủ
tracuuhoadon.minvoice.vn, nội dung ghi "MST người bán: 0313217924" + "Số bảo mật: E864CA9903996CF4"
→ đủ để gọi API trên (mail_reader lưu MST người bán, process_links truyền vào mail_text). Thật 2/2
(Quán Nem 1C24TYY-2848, -3581).

**Từ tuần 02/11/2026 cổng tra cứu chung bắt reCAPTCHA v3** (`Search/SearchInvoiceCaptcha`, server kiểm thật:
thiếu token → 400 "Thiếu token Google reCAPTCHA.", kể cả API cũ `SearchInvoice`). Không vượt reCAPTCHA →
gặp lỗi đó thì HĐ vào `no_portal` (nằm chờ, không tốn lượt); trong cùng tiến trình không gọi cổng chung nữa.

**Đường chính thức: tài khoản phát hành của người bán** (trang `<MST>.minvoice.com.vn`, đọc từ mã JS của trang 19/11):

    POST <host>/api/Account/Login {username, password, ma_dvcs}  → {"token": ...} | {"error": "..."}
    header các request sau: Authorization: "Bear <token>;<ma_dvcs>;vi"   (đúng chữ "Bear"; token sống 6 giờ)
    GET  <host>/api/Invoice68/PrintInvoice?id=<id HĐ>&type=PDF&inchuyendoi=false  → PDF
    POST <host>/api/Pattern/GetData {command: "CM0009", filter: [{columnName, columnType, value}], ...}
                                                      → lưới HĐ (tìm id theo ký hiệu + số)

`id` HĐ thử trước bằng `DLHDon/@Id` của XML (GUID, `dlhdon_id`), không ra mới tìm trên lưới. Cấu hình trong .env
theo MST người bán (dấu '-' thành '_'): MINVOICE_USER_<MST>, MINVOICE_PASSWORD_<MST>, tùy chọn MINVOICE_DVCS_<MST>
(mặc định VP) và MINVOICE_HOST_<MST> (mặc định https://<MST>.minvoice.com.vn). Mật khẩu bị từ chối → ghi vân tay
(sha256, không lưu mật khẩu) vào <INVOICE_OUTPUT_DIR>/.minvoice_login_blocks.json, không thử lại đúng mật khẩu đó
(tránh khóa tài khoản); đổi mật khẩu trong .env là được thử lại. CHƯA chạy thật với tài khoản nào (19/11).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import ROOT, TIMEOUT, check_pdf_is_invoice, is_pdf, new_session

BASE = "https://tracuuhoadon.minvoice.com.vn"
API = f"{BASE}/api/Search/SearchInvoice"
CODE_KEYS = ("Số bảo mật", "Mã tra cứu", "Mã bảo mật", "sobaomat")
_CODE_RE = re.compile(r"^[0-9A-F]{16}$")
_MAIL_MST_RE = re.compile(r"^MST người bán:\s*(\d{10}(?:-\d{3})?)$", re.M)
_SUBJECT_NO_RE = re.compile(r"H[ÓO]A\s*[ĐD][ƠO]N\s*S[ỐO]\s*0*(\d+)", re.I)
_RECAPTCHA_RE = re.compile(r"recaptcha", re.I)
TOKEN_TTL = 5 * 3600            # token sống 6 giờ (expires_in 21600 trong JS) → đăng nhập lại sau 5 giờ
_GRID_COLUMNS = (("khieu", "nvarchar"), ("shdon", "numeric"))


def _env_suffix(mst: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "_", (mst or "").strip())


def account_for(seller_mst: str) -> dict | None:
    """Tài khoản M-invoice của người bán trong .env (khóa theo đúng MST, kể cả đuôi -001). Thiếu → None."""
    sfx = _env_suffix(seller_mst)
    user = (os.getenv(f"MINVOICE_USER_{sfx}") or "").strip()
    password = os.getenv(f"MINVOICE_PASSWORD_{sfx}") or ""
    if not sfx or not user or not password:
        return None
    host = (os.getenv(f"MINVOICE_HOST_{sfx}") or "").strip().rstrip("/") \
        or f"https://{seller_mst.strip()}.minvoice.com.vn"
    if not host.startswith("http"):
        host = "https://" + host
    return {"mst": seller_mst.strip(), "username": user, "password": password,
            "ma_dvcs": (os.getenv(f"MINVOICE_DVCS_{sfx}") or "VP").strip(), "host": host}


def configured_sellers() -> list[str]:
    """Hậu tố khóa .env (MST, '-' → '_') có đủ MINVOICE_USER_ + MINVOICE_PASSWORD_."""
    prefix = "MINVOICE_USER_"
    return sorted(k[len(prefix):] for k in os.environ
                  if k.startswith(prefix) and os.environ[k].strip()
                  and os.getenv("MINVOICE_PASSWORD_" + k[len(prefix):]))


def _blocks_file() -> Path:
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / ".minvoice_login_blocks.json"


def drop_dir() -> Path:
    """Hộp chờ PDF do `scripts/minvoice_batch.py` tải sẵn (cổng có reCAPTCHA v3, job không tự gọi)."""
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "manual" / "minvoice"


def local_name(invoice: dict) -> str:
    """Tên file trong hộp chờ — script và adapter dùng chung công thức này."""
    return (f"{str(invoice.get('nbmst') or '').strip()}_"
            f"{invoice.get('khmshdon')}_{str(invoice.get('khhdon') or '').strip().upper()}_"
            f"{invoice.get('shdon')}.pdf")


def local_pdf(invoice: dict) -> Path | None:
    """PDF có sẵn trong hộp chờ (hoặc `used/` — lượt trước lưu DB lỗi)."""
    d = drop_dir()
    name = local_name(invoice)
    for folder in (d, d / "used"):
        p = folder / name
        if p.is_file():
            return p
    return None


def _fingerprint(acct: dict) -> str:
    raw = "\n".join([acct["host"], acct["ma_dvcs"], acct["username"], acct["password"]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_blocks() -> dict:
    try:
        data = json.loads(_blocks_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def login_blocked(acct: dict) -> str | None:
    """Lý do nếu đúng bộ tài khoản/mật khẩu này đã bị M-invoice từ chối trước đây (mọi tiến trình)."""
    entry = _load_blocks().get(_fingerprint(acct))
    return (entry.get("error") or "bị từ chối") if isinstance(entry, dict) else None


def _block_login(acct: dict, error: str) -> None:
    blocks = _load_blocks()
    blocks[_fingerprint(acct)] = {"mst": acct["mst"], "username": acct["username"], "error": error,
                                  "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    path = _blocks_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass                            # vẫn chặn trong tiến trình (_rejected)


class _Unauthorized(Exception):
    """Token bị từ chối (hết hạn / bị đăng xuất) → đăng nhập lại 1 lần."""


def _looks_json(r) -> bool:
    try:
        r.json()
        return True
    except ValueError:
        return False


def _raise_for_auth(r) -> None:
    if r.status_code == 401:
        raise _Unauthorized(f"HTTP 401: {r.text[:200]}")
    if r.status_code in (200, 400, 403) and "authorization has been denied" in (r.text or "").lower():
        raise _Unauthorized(r.text[:200])


class MinvoiceAdapter(PdfAdapter):
    source_id = "minvoice"
    name = "M-invoice"
    # -001 khai rõ: registry tự quy chi nhánh về công ty mẹ, nhưng requeue HĐ 'no_portal' khớp MSTTCGP chính xác
    mst_tcgp = ("0106026495", "0106026495-001")
    hosts = ("tracuuhoadon.minvoice.vn", "tracuuhoadon.minvoice.com.vn")

    def __init__(self):
        self._tokens: dict = {}         # vân tay tài khoản → (session, token, lúc đăng nhập)
        self._rejected: dict = {}       # vân tay tài khoản → lỗi đăng nhập trong tiến trình này
        self._public_blocked = ""       # cổng chung đã trả lỗi reCAPTCHA trong tiến trình này
        self._announced = False

    def has_pending_input(self) -> bool:
        """Có PDF trong hộp chờ (scripts/minvoice_batch.py) hoặc có tài khoản .env → đưa HĐ về hộp chờ.

        Tài khoản: 1 lần mỗi tiến trình (đủ để `_prepare_queues` requeue). PDF: còn file là còn nhắc.
        """
        d = drop_dir()
        if d.is_dir() and any(d.glob("*.pdf")):
            return True
        if self._announced or not configured_sellers():
            return False
        self._announced = True
        return True

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link trang chủ + "MST người bán" + "Số bảo mật" trong mail (không mở link). Thiếu 1 trong 2 → None."""
        lines = (mail_text or "").splitlines()
        codes = [c for c in dict.fromkeys([code or ""] + [ln.strip() for ln in lines[1:]]) if _CODE_RE.match(c)]
        msts = _MAIL_MST_RE.findall(mail_text or "")
        if len(codes) != 1 or len(set(msts)) != 1:
            return None
        inv = {"nbmst": msts[0], "ma_tra_cuu": codes[0], "tt_khac": {"Số bảo mật": codes[0]}}
        m = _SUBJECT_NO_RE.search(lines[0] if lines else "")
        if m:
            inv["shdon"] = int(m.group(1))
        return inv

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for key in CODE_KEYS:
            if str(tt_khac.get(key) or "").strip():
                return str(tt_khac[key]).strip()
        return (invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        local = local_pdf(invoice)
        if local is not None:
            data = local.read_bytes()
            if not is_pdf(data):
                raise PdfFetchError(f"PDF hộp chờ {local.name} không hợp lệ", "bad_content")
            check_pdf_is_invoice(data, invoice)
            d = drop_dir()
            if local.parent == d:               # giữ bản trong used/ để lượt lại vẫn dùng được
                used = d / "used"
                used.mkdir(exist_ok=True)
                local.replace(used / local.name)
            return data
        mst = (invoice.get("nbmst") or "").strip()
        acct = account_for(mst) if mst else None
        if acct:
            return self._fetch_by_account(acct, invoice)
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Số bảo mật' / 'Mã tra cứu' (M-invoice)", "no_code")
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — M-invoice cần MST + số bảo mật", "no_code")
        if self._public_blocked:
            raise PdfFetchError(self._public_blocked, "no_portal")
        s = new_session()
        try:
            r = self._get(s, {"masothue": mst, "sobaomat": code, "type": "PDF", "inchuyendoi": "false"})
        except PdfFetchError as e:
            if e.kind == "other" and _RECAPTCHA_RE.search(str(e)):
                self._public_blocked = (
                    f"Cổng tra cứu M-invoice bắt reCAPTCHA ({e}) → cần tài khoản phát hành của người bán "
                    f"(MINVOICE_USER_<MST> / MINVOICE_PASSWORD_<MST> trong .env)")
                raise PdfFetchError(self._public_blocked, "no_portal") from e
            raise
        pdf = r.content
        if not is_pdf(pdf):
            raise PdfFetchError(
                f"M-invoice trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    # ------------------------------------------------------------- tài khoản phát hành
    def _fetch_by_account(self, acct: dict, invoice: dict) -> bytes:
        api = acct["host"] + "/api"
        dl_id = str(invoice.get("dlhdon_id") or "").strip()
        ids = [dl_id] if dl_id else []
        last = None
        for attempt in range(2):            # lần 2: token hết hạn / bị từ chối → đăng nhập lại
            s, headers = self._session(acct, fresh=attempt > 0)
            try:
                tried = list(ids)
                for inv_id in ids:
                    pdf = self._print(s, api, headers, inv_id)
                    if pdf:
                        check_pdf_is_invoice(pdf, invoice)
                        return pdf
                found = self._find_id(s, api, headers, invoice)
                if found and found not in tried:
                    tried.append(found)
                    pdf = self._print(s, api, headers, found)
                    if pdf:
                        check_pdf_is_invoice(pdf, invoice)
                        return pdf
                raise PdfFetchError(
                    f"M-invoice ({acct['host']}) không có HĐ {invoice.get('khhdon')}-{invoice.get('shdon')} "
                    f"(id đã thử: {', '.join(tried) or '-'})", "no_pdf")
            except _Unauthorized as e:
                last = e
                self._tokens.pop(_fingerprint(acct), None)
        raise PdfFetchError(f"M-invoice từ chối token sau khi đăng nhập lại: {last}", "auth")

    def _session(self, acct: dict, fresh: bool = False):
        fp = _fingerprint(acct)
        cached = self._tokens.get(fp)
        if cached and not fresh and time.time() - cached[2] < TOKEN_TTL:
            s, token = cached[0], cached[1]
        else:
            s, token = self._login(acct)
            self._tokens[fp] = (s, token, time.time())
        return s, {"Authorization": f"Bear {token};{acct['ma_dvcs']};vi",
                   "Accept": "application/json, text/plain, */*"}

    def _login(self, acct: dict):
        fp = _fingerprint(acct)
        why = self._rejected.get(fp) or login_blocked(acct)
        if why:
            raise PdfFetchError(
                f"Tài khoản M-invoice {acct['username']} ({acct['mst']}) đã bị từ chối ({why}) — không thử lại "
                f"cùng mật khẩu; sửa MINVOICE_PASSWORD_{_env_suffix(acct['mst'])} trong .env", "no_portal")
        url = acct["host"] + "/api/Account/Login"
        s = new_session()
        try:
            r = s.request("POST", url, json={"username": acct["username"], "password": acct["password"],
                                             "ma_dvcs": acct["ma_dvcs"]},
                          timeout=TIMEOUT, headers={"Accept": "application/json, text/plain, */*"})
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
        if r.status_code >= 500 or r.status_code == 404:
            raise PdfFetchError(f"HTTP {r.status_code} từ {url} (kiểm MINVOICE_HOST_?)", "server")
        try:
            js = r.json()
        except ValueError:
            js = None
        if isinstance(js, dict) and js.get("token") and r.status_code == 200:
            return s, str(js["token"])
        if isinstance(js, dict) and js.get("error"):
            # M-invoice trả lời rõ là từ chối → chặn đúng bộ mật khẩu này (không để bị khóa tài khoản)
            err = str(js["error"]).strip()
            self._rejected[fp] = err
            _block_login(acct, err)
            raise PdfFetchError(f"M-invoice từ chối đăng nhập {acct['username']} ({acct['mst']}): {err}",
                                "no_portal")
        raise PdfFetchError(f"Đăng nhập M-invoice trả HTTP {r.status_code}: {(r.text or '')[:200]}", "other")

    @staticmethod
    def _print(s, api: str, headers: dict, inv_id: str) -> bytes | None:
        """PDF của HĐ `inv_id`; None = M-invoice không có HĐ id này (trả JSON/HTML thay vì PDF)."""
        url = f"{api}/Invoice68/PrintInvoice"
        try:
            r = s.request("GET", url, params={"id": inv_id, "type": "PDF", "inchuyendoi": "false"},
                          headers=headers, timeout=TIMEOUT)
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
        _raise_for_auth(r)
        if r.status_code == 429:
            raise PdfFetchError(f"429 từ {url}", "rate_limit")
        if r.status_code == 200 and is_pdf(r.content):
            return r.content
        if r.status_code >= 500 and not _looks_json(r):
            raise PdfFetchError(f"HTTP {r.status_code} từ {url}: {(r.text or '')[:200]}", "server")
        return None

    @staticmethod
    def _find_id(s, api: str, headers: dict, invoice: dict) -> str | None:
        """Tìm id HĐ trên lưới hóa đơn (như ô lọc "Ký hiệu" + "Số hóa đơn" của trang)."""
        khhdon = str(invoice.get("khhdon") or "").strip()
        try:
            shdon = int(invoice.get("shdon"))
        except (TypeError, ValueError):
            return None
        if not khhdon:
            return None
        values = {"khieu": khhdon, "shdon": str(shdon)}
        body = {"command": "CM0009", "start": 0, "count": 50, "continue": None, "tlbparam": [],
                "isListAll": True,
                "filter": [{"columnName": c, "columnType": t, "value": values[c]} for c, t in _GRID_COLUMNS]}
        url = f"{api}/Pattern/GetData"
        try:
            r = s.request("POST", url, json=body, headers=headers, timeout=TIMEOUT)
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
        _raise_for_auth(r)
        if r.status_code >= 500:
            raise PdfFetchError(f"HTTP {r.status_code} từ {url}: {(r.text or '')[:200]}", "server")
        try:
            js = r.json()
        except ValueError:
            return None
        rows = js.get("data") if isinstance(js, dict) else js
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("rows")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                no = int(row.get("shdon"))
            except (TypeError, ValueError):
                continue
            kh = str(row.get("khieu") or "").strip().upper()
            if no == shdon and kh.endswith(khhdon.upper()):
                found = row.get("hoadon68_id") or row.get("id")
                if found:
                    return str(found)
        return None

    # ------------------------------------------------------------- cổng tra cứu chung
    @staticmethod
    def _get(s, params: dict):
        """Như common.request() nhưng đọc JSON lỗi trước: 'không tìm thấy' của M-invoice là HTTP 500."""
        try:
            r = s.request("GET", API, params=params, timeout=TIMEOUT,
                          headers={"Accept": "application/json, text/plain, */*",
                                   "Referer": f"{BASE}/tra-cuu-hoa-don"})
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {API}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {API}: {e}", "network") from e
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            raise PdfFetchError(f"429 từ {API}", "rate_limit")
        try:
            err = r.json()
        except ValueError:
            err = None
        msg = str((err or {}).get("message") or (err or {}).get("title") or "").strip()
        if isinstance(err, dict) and (str(err.get("code")) == "5000" or "không chính xác" in msg.lower()):
            raise PdfFetchError(f"M-invoice không tìm thấy HĐ (MST {params['masothue']}, "
                                f"mã {params['sobaomat']}): {msg}", "no_pdf")
        if r.status_code >= 500:
            raise PdfFetchError(f"HTTP {r.status_code} từ M-invoice: {msg or r.text[:200]}", "server")
        raise PdfFetchError(f"M-invoice trả HTTP {r.status_code}: {msg or r.text[:200]}", "other")


register(MinvoiceAdapter())
