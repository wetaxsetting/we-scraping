# api_client.py
"""
Client gọi API hoá đơn điện tử của Tổng cục Thuế (GDT) — hoadondientu.gdt.gov.vn.

Logic HTTP được port từ bản đã chạy thật (xem example/search-data-tax để tham khảo):
    - Base = .../api  (có tiền tố /api)
    - BẮT BUỘC header 'request-id' (UUID) trên mọi request, nếu không GDT trả 403
      "Hệ thống phát hiện hành vi không hợp lệ".
    - Query mua vào cần ttxly (có mã ==5 | không mã ==6 | MTT ==8).
    - Hoá đơn máy tính tiền (MTT) dùng endpoint /sco-query/...
    - Dùng chung 1 session (giữ cookies) để sco-query hoạt động.

Khác với web portal: thêm authenticate_auto() — tự giải captcha bằng model CRNN
đã train (gdt_captcha_solver.py), không cần gõ tay.

Public API:
    fetch_captcha()
    authenticate(username, password, ckey, cvalue)
    authenticate_auto(username, password, max_retries=6)
    fetch_all_invoices(token, invoice_type, from_date, to_date)  -> (rows, warnings)
    export_invoice_list(token, sub_type, from_date, to_date)  -> bytes (.xlsx, nút "Xuất Excel")
    parse_invoice_list_excel(data, sub_type)  -> rows (cùng khóa với API JSON)
    fetch_all_details_parallel(token, rows, ...)  -> [{inv, detail, error}]
    download_invoices_xml_batch(token, rows, output_dir, ...)
    fetch_invoice_zip(token, nbmst, khhdon, shdon, khmshdon, use_sco=False)
"""
import io
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

requests.packages.urllib3.disable_warnings()

log = logging.getLogger(__name__)

API_BASE = "https://hoadondientu.gdt.gov.vn/api"

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://hoadondientu.gdt.gov.vn",
    "Referer": "https://hoadondientu.gdt.gov.vn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Session dùng chung — giữ cookies từ login để sco-query hoạt động
_gdt_session = requests.Session()
_gdt_session.verify = False
_gdt_session.headers.update(_HEADERS)


def _inject_request_id(req, **_):
    """GDT bắt buộc header 'request-id' (UUID) trên MỌI request API.

    Thiếu header này, gateway GDT trả 403 "Hệ thống phát hiện hành vi không hợp lệ.
    Yêu cầu đã bị chặn." cho toàn bộ endpoint (trừ /captcha) — kể cả endpoint công khai.
    Đây là nguyên nhân login luôn thất bại nếu gọi API "ngây thơ".
    """
    req.headers["request-id"] = str(uuid.uuid4())


_gdt_session.hooks.setdefault("request", []).append(_inject_request_id)


def _h(token: str = "") -> dict:
    h = dict(_HEADERS)
    h["request-id"] = str(uuid.uuid4())
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------------

def fetch_captcha() -> dict:
    """GET /captcha → {ckey, image_svg(SVG string)}"""
    r = _gdt_session.get(f"{API_BASE}/captcha", headers=_h(), timeout=15)
    r.raise_for_status()
    data = r.json()
    return {
        "ckey":      data.get("key") or data.get("ckey") or "",
        "image_svg": data.get("content") or data.get("image") or "",
    }


# ---------------------------------------------------------------------------
# ĐĂNG NHẬP
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str, ckey: str, cvalue: str) -> dict:
    """Đăng nhập với captcha đã giải. Returns {ok, token, message}."""
    _gdt_session.cookies.clear()  # không dùng lại cookies cũ

    payload = {"username": username, "password": password,
               "cvalue": cvalue, "ckey": ckey}
    r = _gdt_session.post(f"{API_BASE}/security-taxpayer/authenticate",
                          json=payload, headers=_h(), timeout=15)
    if r.status_code != 200:
        try:
            msg = r.json().get("message") or r.json().get("error") or r.text
        except Exception:
            msg = r.text
        return {"ok": False, "token": "", "message": msg}

    data = r.json()
    token = (data.get("token")
             or data.get("access_token")
             or (data.get("data") or {}).get("token")
             or "")
    if not token:
        return {"ok": False, "token": "", "message": f"Không tìm thấy token. Response: {data}"}
    return {"ok": True, "token": token, "message": ""}


# ---------------------------------------------------------------------------
# ĐĂNG NHẬP TỰ ĐỘNG  (giải captcha bằng model CRNN đã train)
# ---------------------------------------------------------------------------

_SOLVER = None  # load model 1 lần, tái sử dụng


def _get_solver():
    """Lazy-load GDTCaptchaSolver (chỉ nạp torch/model khi thực sự auto-login)."""
    global _SOLVER
    if _SOLVER is None:
        from gdt_captcha_solver import GDTCaptchaSolver
        _SOLVER = GDTCaptchaSolver()
    return _SOLVER


def authenticate_auto(username: str, password: str,
                      max_retries: int = 6, delay: float = 1.0) -> dict:
    """
    Đăng nhập GDT tự động — tự giải captcha, thử lại nếu sai.
    Returns {ok, token, message, attempts, confidence}.
    """
    solver = _get_solver()
    last_msg = "Chưa thử lần nào"
    last_conf = 0.0

    for attempt in range(1, max_retries + 1):
        try:
            cap = fetch_captcha()
            ckey, svg = cap["ckey"], cap["image_svg"]
            if not svg:
                last_msg = "GDT không trả captcha"
                time.sleep(delay)
                continue

            text, conf = solver.solve(svg)
            last_conf = conf
            if not text:
                last_msg = f"Model không chắc captcha (conf={conf:.2f})"
                time.sleep(delay)
                continue

            log.info("[auto-login %d] captcha=%r conf=%.3f", attempt, text, conf)
            result = authenticate(username, password, ckey, text)
            if result["ok"]:
                return {"ok": True, "token": result["token"], "message": "",
                        "attempts": attempt, "confidence": conf}

            msg = (result.get("message") or "").lower()
            if any(kw in msg for kw in
                   ("captcha", "mã xác nhận", "xác thực", "cvalue", "ckey", "wrong")):
                last_msg = result.get("message", "")
                time.sleep(delay)
                continue

            # Sai user/pass hoặc lỗi khác → không retry
            return {"ok": False, "token": "", "message": result.get("message", ""),
                    "attempts": attempt, "confidence": conf}

        except Exception as e:  # noqa: BLE001
            last_msg = f"{type(e).__name__}: {e}"
            log.exception("[auto-login %d] exception", attempt)
            time.sleep(delay)

    return {"ok": False, "token": "",
            "message": f"Thử {max_retries} lần không được. Lỗi cuối: {last_msg}",
            "attempts": max_retries, "confidence": last_conf}


_CAPTCHA_WORDS = ("captcha", "mã xác nhận", "xác thực", "cvalue", "ckey", "wrong")


def _is_captcha_error(msg: str) -> bool:
    return any(kw in (msg or "").lower() for kw in _CAPTCHA_WORDS)


def sco_captcha(max_retries: int = 4, delay: float = 1.0) -> dict:
    """
    Captcha cho sco-query (HĐ máy tính tiền) → {ckey, cvalue} gửi kèm làm query param.
    Từ 10/2026 cổng GDT bật popup captcha cho MỌI request sco-query (tìm, sang trang, xuất Excel —
    `withCaptcha` trong JS cổng); thiếu thì GDT trả 500 "Required request parameter 'ckey' ... is not
    present". Cùng captcha SVG với đăng nhập → model g_600x120. export-xml thì không cần.
    """
    solver = _get_solver()
    for _ in range(max_retries):
        cap = fetch_captcha()
        if cap["image_svg"]:
            text, conf = solver.solve(cap["image_svg"])
            if text:
                return {"ckey": cap["ckey"], "cvalue": text}
        time.sleep(delay)
    raise GDTError(f"Không giải được captcha GDT cho sco-query sau {max_retries} lần", kind="server")


# ---------------------------------------------------------------------------
# LẤY DANH SÁCH HÓA ĐƠN  (pagination bằng state token)
# ---------------------------------------------------------------------------

_ENDPOINTS = {
    "ban":        "/query/invoices/sold",
    "mua":        "/query/invoices/purchase",
    "ban-khma":   "/query/invoices/sold",           # không có mã — cùng path, khác ttxly
    "mua-khma":   "/query/invoices/purchase",
    "ban-mtt":    "/sco-query/invoices/sold",        # MTT dùng sco-query
    "mua-mtt":    "/sco-query/invoices/purchase",
}

# /query/invoices/purchase BẮT BUỘC có ttxly. GDT portal gửi:
#   mua (có mã): ttxly==5 | mua-khma: ttxly==6 | mua-mtt: ttxly==8
_EXTRA_SEARCH = {
    "mua":       ";ttxly==5",
    "ban-khma":  ";ttxly==6",
    "mua-khma":  ";ttxly==6",
    "mua-mtt":   ";ttxly==8",
}

# "tất cả" = gộp 3 loại: có mã + không mã + MTT
_ALL_TYPES = {
    "ban-all": ["ban", "ban-khma", "ban-mtt"],
    "mua-all": ["mua", "mua-khma", "mua-mtt"],
}

_TYPE_LABEL = {
    "ban": "Có mã", "ban-khma": "Không có mã", "ban-mtt": "MTT",
    "mua": "Có mã", "mua-khma": "Không có mã", "mua-mtt": "MTT",
}

_TYPE_WARN_LABEL = {
    "ban": "HĐ có mã", "ban-khma": "HĐ không có mã", "ban-mtt": "HĐ MTT (sco-query)",
    "mua": "HĐ mua — có mã", "mua-khma": "HĐ mua — không có mã", "mua-mtt": "HĐ mua — MTT",
}


def _build_search(from_date: str, to_date: str, extra: str = "") -> str:
    """yyyy-MM-dd hoặc dd/MM/yyyy → RSQL search string."""
    def to_vn(d: str) -> str:
        if "-" in d:
            y, m, day = d.split("-")
            return f"{day}/{m}/{y}"
        return d
    return (f"tdlap=ge={to_vn(from_date)}T00:00:00;"
            f"tdlap=le={to_vn(to_date)}T23:59:59{extra}")


class _RateLimitError(RuntimeError):
    """GDT trả 429 — cần chờ rồi retry."""


class _TimeoutError(RuntimeError):
    """GDT không trả lời trong thời gian timeout — cần retry."""


def _do_get(token: str, ep: str, params: dict, timeout: int = 60) -> requests.Response:
    try:
        r = _gdt_session.get(f"{API_BASE}{ep}", headers=_h(token),
                             params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        raise _TimeoutError(f"GDT không phản hồi sau {timeout}s")
    if r.status_code in (401, 403):
        raise RuntimeError("__AUTH__Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại.")
    if r.status_code == 429:
        raise _RateLimitError("429")
    if not r.ok:
        try:
            body = r.json()
            api_msg = body.get("message") or body.get("error") or r.text[:300]
        except Exception:
            api_msg = r.text[:300]
        raise RuntimeError(f"API GDT trả lỗi {r.status_code}: {api_msg}")
    return r


def _fetch_single_type(token: str, invoice_type: str,
                       from_date: str, to_date: str,
                       size: int = 50) -> list:
    """Lấy toàn bộ hóa đơn 1 loại, tự retry khi 429/timeout."""
    ep    = _ENDPOINTS.get(invoice_type) or _ENDPOINTS["ban"]
    extra = _EXTRA_SEARCH.get(invoice_type, "")

    all_rows: list = []
    state = ""
    retry_wait = 5
    sco = ep.startswith("/sco-query")

    while True:
        params = {"sort": "tdlap:desc", "size": size,
                  "search": _build_search(from_date, to_date, extra)}
        if state:
            params["state"] = state

        captcha_left, attempt = 4, 0
        while True:
            try:
                r = _do_get(token, ep, {**params, **sco_captcha()} if sco else params)
                break
            except _RateLimitError:
                if attempt == 3:
                    raise RuntimeError(
                        f"Rate limit 429 sau 4 lần thử ({invoice_type}). "
                        f"Vui lòng đợi vài phút rồi thử lại.")
                time.sleep(retry_wait * (2 ** attempt))  # 5,10,20,40s
                attempt += 1
            except _TimeoutError:
                if attempt == 3:
                    raise RuntimeError(
                        f"GDT không phản hồi sau 4 lần thử ({invoice_type}).")
                time.sleep(10 * (attempt + 1))
                attempt += 1
            except RuntimeError as e:
                if not (sco and captcha_left and _is_captcha_error(str(e))):
                    raise
                captcha_left -= 1                 # model đọc sai captcha → xin ảnh mới

        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Body không phải JSON: {r.text[:300]}")

        records = data.get("datas") or data.get("content") or data.get("data") or []
        if not records:
            break
        all_rows.extend(records)
        total = int(data.get("total") or data.get("totalElements") or 0)
        if total and len(all_rows) >= total:
            break
        next_state = data.get("state") or ""
        if not next_state or next_state == state or len(records) < size:
            break
        state = next_state
    return all_rows


SUB_TYPES = tuple(_ENDPOINTS)            # ban, mua, ban-khma, mua-khma, ban-mtt, mua-mtt
SUB_TYPE_LABEL = _TYPE_LABEL


def expand_invoice_types(types) -> list:
    """'mua-all' → ['mua','mua-khma','mua-mtt']; giữ nguyên sub-type lẻ."""
    out = []
    for t in types:
        for st in _ALL_TYPES.get(t, [t]):
            if st not in _ENDPOINTS:
                raise ValueError(f"Loại hóa đơn không hợp lệ: {st!r}")
            if st not in out:
                out.append(st)
    return out


def fetch_invoices_subtype(token: str, sub_type: str,
                           from_date: str, to_date: str, size: int = 50) -> list:
    """
    Lấy toàn bộ HĐ của 1 sub-type trong 1 khoảng ngày (≤ 30 ngày theo giới hạn GDT).
    Mỗi dòng được gắn _sub_type và _loai_hd. Lỗi 401/403 → GDTAuthError.
    """
    try:
        rows = _fetch_single_type(token, sub_type, from_date, to_date, size)
    except RuntimeError as e:
        if str(e).startswith("__AUTH__"):
            raise GDTAuthError(str(e)[8:]) from e
        raise
    for r in rows:
        r["_sub_type"] = sub_type
        r["_loai_hd"] = _TYPE_LABEL.get(sub_type, sub_type)
    return rows


def jwt_expiry(token: str):
    """
    Đọc thời điểm hết hạn (claim 'exp') của JWT → datetime UTC, hoặc None.

    Chỉ giải mã base64 phần payload, không kiểm chữ ký (không cần — ta chỉ muốn
    biết còn hạn hay không; GDT vẫn là bên quyết định cuối qua 401).
    """
    import base64
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:
        return None


def fetch_all_invoices(token: str, invoice_type: str,
                       from_date: str, to_date: str,
                       size: int = 50):
    """
    Lấy toàn bộ hóa đơn.
    - invoice_type = 'ban'/'mua'         → 1 endpoint
    - invoice_type = 'ban-all'/'mua-all' → gộp 3 loại (có mã + không mã + MTT)
    Returns (rows, warnings).
    """
    if invoice_type in _ALL_TYPES:
        return _fetch_all_combined(token, _ALL_TYPES[invoice_type],
                                   from_date, to_date, size)
    return _fetch_single_type(token, invoice_type, from_date, to_date, size), []


def _fetch_all_combined(token: str, type_list: list,
                        from_date: str, to_date: str, size: int = 50):
    """Gộp nhiều loại, bỏ trùng theo nbmst+khhdon+shdon, gắn _loai_hd."""
    seen: set = set()
    combined: list = []
    warnings: list = []

    for itype in type_list:
        warn_label = _TYPE_WARN_LABEL.get(itype, itype)
        loai_label = _TYPE_LABEL.get(itype, itype)
        try:
            rows = _fetch_single_type(token, itype, from_date, to_date, size)
            for r in rows:
                key = f"{r.get('nbmst','')}/{r.get('khhdon','')}/{r.get('shdon','')}"
                if key not in seen:
                    seen.add(key)
                    r["_loai_hd"] = loai_label
                    combined.append(r)
        except RuntimeError as e:
            if str(e).startswith("__AUTH__"):
                raise
            warnings.append(f"[{warn_label}] Lỗi: {e}")
        except Exception as e:
            warnings.append(f"[{warn_label}] Lỗi không xác định: {e}")

    return combined, warnings


# ---------------------------------------------------------------------------
# DANH SÁCH HÓA ĐƠN qua nút "Xuất Excel" của cổng — 1 request / sub-type / khoảng ngày
# ---------------------------------------------------------------------------

# URL chép từ trình duyệt (cổng GDT, 10/2026) cho ban, mua, mua-mtt: bán ra gọi export-excel,
# mua vào gọi export-excel-sold kèm type=purchase (tên endpoint của GDT đặt vậy).
# ban-khma, mua-khma, ban-mtt chưa có URL mẫu: suy theo cùng quy tắc; chạy thật 20/10 cả 3 đều trả
# file Excel đúng mẫu (mua-khma 30 dòng; ban-khma, ban-mtt 0 dòng vì công ty chưa có HĐ loại này).
_EXPORT_ENDPOINTS = {
    "ban":      "/query/invoices/export-excel",
    "ban-khma": "/query/invoices/export-excel",
    "ban-mtt":  "/sco-query/invoices/export-excel",
    "mua":      "/query/invoices/export-excel-sold",
    "mua-khma": "/query/invoices/export-excel-sold",
    "mua-mtt":  "/sco-query/invoices/export-excel-sold",
}


def _export_once(token: str, ep: str, params: dict, timeout: int) -> bytes:
    try:
        r = _gdt_session.get(f"{API_BASE}{ep}", headers=_h(token), params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        raise GDTError(f"GDT không phản hồi (timeout {timeout}s)", kind="network")
    except requests.exceptions.ConnectionError as e:
        raise GDTError(f"Lỗi kết nối GDT: {e}", kind="network")
    if r.status_code in (401, 403):
        raise GDTAuthError()
    if r.status_code == 429:
        raise GDTError("429 – Quá nhiều request, cần chờ", kind="rate_limit")
    if r.status_code in (502, 503, 504):
        raise GDTError(f"GDT quá tải ({r.status_code})", kind="server")
    if not r.ok or not r.content.startswith(b"PK"):       # .xlsx là file ZIP
        try:
            api_msg = r.json().get("message") or r.json().get("error") or r.text[:300]
        except Exception:
            api_msg = r.text[:200] if r.text else "(rỗng)"
        raise GDTError(f"GDT không trả file Excel ({r.status_code}): {api_msg}",
                       kind="bad_content" if r.ok else "other")
    return r.content


def export_invoice_list(token: str, sub_type: str, from_date: str, to_date: str,
                        timeout: int = 120, retries: int = 2) -> bytes:
    """
    File .xlsx danh sách HĐ của 1 sub-type trong 1 khoảng ngày (≤ 30 ngày), như nút "Xuất Excel".
    Lỗi → GDTError có kind. Lỗi tạm thời (mạng, GDT quá tải, 429) thử lại tối đa `retries` lần.
    """
    ep = _EXPORT_ENDPOINTS.get(sub_type)
    if ep is None:
        raise ValueError(f"Loại hóa đơn không hợp lệ: {sub_type!r}")
    extra = _EXTRA_SEARCH.get(sub_type, "")
    if extra:
        extra += "    "                  # cổng gửi "ttxly==5" kèm 4 dấu cách; giữ nguyên như cổng
    params = {"sort": "tdlap:desc", "search": _build_search(from_date, to_date, extra)}
    if sub_type.startswith("mua"):
        params["type"] = "purchase"
    sco = ep.startswith("/sco-query")
    captcha_left, attempt = 4, 0
    while True:
        try:
            return _export_once(token, ep, {**params, **sco_captcha()} if sco else params, timeout)
        except GDTError as e:
            if sco and captcha_left and e.kind == "other" and _is_captcha_error(str(e)):
                captcha_left -= 1                 # captcha sai → xin ảnh mới
                continue
            if not e.retryable or attempt == retries:
                raise
            time.sleep(15 * (attempt + 1))       # 15s, 30s
            attempt += 1


def parse_invoice_list_excel(data: bytes, sub_type: str) -> list:
    """File từ export_invoice_list → dòng cùng khóa với API JSON, gắn _sub_type/_loai_hd."""
    import invoice_list_excel
    try:
        rows = invoice_list_excel.parse(data)
    except ValueError as e:
        raise GDTError(f"File Excel danh sách không đúng mẫu: {e}", kind="bad_content") from e
    for r in rows:
        r["_sub_type"] = sub_type
        r["_loai_hd"] = _TYPE_LABEL.get(sub_type, sub_type)
    return rows


# ---------------------------------------------------------------------------
# CHI TIẾT HÓA ĐƠN — tải ZIP chứa XML
# ---------------------------------------------------------------------------

class GDTError(Exception):
    """
    Lỗi gọi GDT, kèm loại (kind) để hộp chờ quyết định chờ bao lâu:
        network | server | rate_limit  → tạm thời, thử lại lượt sau
        not_ready                      → GDT 500 "XML chưa có", thường có sau vài ngày
        auth                           → token hỏng, đăng nhập lại
        bad_content | other            → không phải lỗi tạm thời
    """
    TRANSIENT = frozenset({"network", "server", "rate_limit"})

    def __init__(self, msg: str, kind: str = "other"):
        super().__init__(msg)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        """Có đáng thử lại ngay trong cùng lượt chạy không."""
        return self.kind in self.TRANSIENT


class GDTAuthError(GDTError):
    def __init__(self, msg: str = "Phiên đăng nhập hết hạn."):
        super().__init__(msg, kind="auth")


_GDTError = GDTError  # tên cũ, giữ cho code đang dùng


def _download_zip(token: str, nbmst: str, khhdon: str,
                  shdon: int, khmshdon: int, use_sco: bool = False) -> bytes:
    params = {"nbmst": nbmst, "khhdon": khhdon, "shdon": shdon, "khmshdon": khmshdon}
    ep_prefix = "sco-query" if use_sco else "query"
    try:
        r = _gdt_session.get(f"{API_BASE}/{ep_prefix}/invoices/export-xml",
                             headers=_h(token), params=params, timeout=60)
    except requests.exceptions.Timeout:
        raise GDTError("GDT không phản hồi (timeout 60s)", kind="network")
    except requests.exceptions.ConnectionError as e:
        raise GDTError(f"Lỗi kết nối GDT: {e}", kind="network")
    if r.status_code in (401, 403):
        raise GDTAuthError()
    if r.status_code == 429:
        raise GDTError("429 – Quá nhiều request, cần chờ", kind="rate_limit")
    if r.status_code == 500:
        raise GDTError("XML không có sẵn trên GDT (500)", kind="not_ready")
    if r.status_code in (502, 503, 504):
        raise GDTError(f"GDT quá tải ({r.status_code})", kind="server")
    if not r.ok:
        raise GDTError(f"GDT lỗi {r.status_code}", kind="other")
    content = r.content
    if not content.startswith(b"PK"):  # ZIP magic
        try:
            api_msg = r.json().get("message") or r.json().get("error") or r.text[:300]
        except Exception:
            api_msg = r.text[:200] if r.text else "(rỗng)"
        raise GDTError(f"GDT không trả về ZIP: {api_msg}", kind="bad_content")
    return content


def download_invoice_zip(token: str, inv: dict) -> bytes:
    """Tải ZIP của 1 hóa đơn từ dòng của API danh sách. Lỗi → GDTError có kind."""
    return _download_zip(
        token, inv.get("nbmst", ""), inv.get("khhdon", ""),
        int(inv.get("shdon") or 0), int(inv.get("khmshdon") or 1),
        use_sco=is_sco_row(inv))


def is_sco_row(inv: dict) -> bool:
    """HĐ máy tính tiền → endpoint sco-query."""
    return inv.get("_loai_hd") == "MTT" or str(inv.get("_sub_type", "")).endswith("-mtt")


def fetch_invoice_zip(token: str, nbmst: str, khhdon: str,
                      shdon: int, khmshdon: int, use_sco: bool = False) -> bytes:
    """Trả về raw ZIP bytes."""
    return _download_zip(token, nbmst, khhdon, shdon, khmshdon, use_sco)


def fetch_invoice_detail(token: str, nbmst: str, khhdon: str,
                         shdon: int, khmshdon: int, use_sco: bool = False) -> dict:
    """Tải ZIP → parse XML → dict chi tiết."""
    from xml_parser import parse_zip
    zip_bytes = _download_zip(token, nbmst, khhdon, shdon, khmshdon, use_sco)
    return parse_zip(zip_bytes)


def fetch_all_details_parallel(token: str, invoice_list: list,
                               max_workers: int = 2,
                               on_progress=None) -> list:
    """
    Tải ZIP + parse XML cho toàn bộ hóa đơn (rate-limited: ≤2 worker, ≥0.7s/request,
    retry 429 với backoff). Returns list[{inv, detail|None, error|None}] đúng thứ tự.
    """
    from xml_parser import parse_zip
    import threading

    _lock = threading.Lock()
    _last_t = [0.0]
    MIN_INTERVAL = 0.7

    def _throttled_download(inv: dict) -> bytes:
        with _lock:
            elapsed = time.monotonic() - _last_t[0]
            if elapsed < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - elapsed)
            _last_t[0] = time.monotonic()
        return _download_zip(
            token, inv.get("nbmst", ""), inv.get("khhdon", ""),
            int(inv.get("shdon", 0)), int(inv.get("khmshdon", 1)),
            use_sco=inv.get("_loai_hd") == "MTT")

    def _fetch_one(idx: int, inv: dict):
        for attempt in range(4):
            try:
                zip_bytes = _throttled_download(inv)
                return idx, {"inv": inv, "detail": parse_zip(zip_bytes), "error": None}
            except _GDTError as e:
                if e.retryable:
                    time.sleep(10 * (2 ** attempt))  # 10,20,40,80s
                    continue
                return idx, {"inv": inv, "detail": None, "error": str(e)}
            except Exception as e:
                return idx, {"inv": inv, "detail": None, "error": str(e)}
        return idx, {"inv": inv, "detail": None,
                     "error": "Quá nhiều request (429) – thử lại sau vài phút."}

    total = len(invoice_list)
    _done = [0]
    _done_lock = threading.Lock()
    results = [None] * total

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one, i, inv): i
                   for i, inv in enumerate(invoice_list)}
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result
            with _done_lock:
                _done[0] += 1
                if on_progress:
                    on_progress(_done[0], total)
    return results


# ---------------------------------------------------------------------------
# TẢI XML VÀ LƯU VÀO THƯ MỤC (theo cấu trúc <MST>/<năm>/<tháng>)
# ---------------------------------------------------------------------------

_VN_TZ = timezone(timedelta(hours=7))


def tdlap_vn(inv: dict):
    """
    Ngày lập HĐ theo giờ Việt Nam (date) hoặc None.

    GDT trả tdlap theo UTC: '2026-10-11T17:00:00Z' = 00:00 ngày 12/10 giờ VN.
    Cắt chuỗi [:10] sẽ lệch 1 ngày → phải đổi múi giờ trước.
    """
    s = inv.get("tdlap") or ""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # không có múi giờ → coi như đã là giờ VN
        return dt.date()
    return dt.astimezone(_VN_TZ).date()


def invoice_rel_dir(inv: dict, company_mst: str = "") -> str:
    """
    Thư mục con cho 1 hóa đơn: <MST_công_ty>/<năm>/<tháng>/<MST_người_bán>.
    - MST: lấy theo công ty mình (company_mst). Nếu rỗng → "unknown".
    - năm/tháng: theo ngày lập HĐ (tdlap) đã đổi sang giờ VN.
    - MST người bán: tách riêng vì {khmshdon}_{khhdon}_{shdon} không duy nhất
      giữa các người bán (2 NCC cùng phát hành 1_C26TAA_123 sẽ ghi đè nhau).
    """
    import os
    mst = (company_mst or "unknown").strip() or "unknown"
    d = tdlap_vn(inv)
    year = f"{d.year}" if d else "unknown"
    month = f"{d.month:02d}" if d else "00"
    nbmst = (inv.get("nbmst") or "unknown").strip() or "unknown"
    return os.path.join(mst, year, month, nbmst)


def invoice_filename(inv: dict, ext: str = "xml") -> str:
    """Tên file: {khmshdon}_{khhdon}_{shdon}.{ext}  (dùng chung cho XML/PDF)."""
    khmshdon = int(inv.get("khmshdon") or 1)
    khhdon = (inv.get("khhdon", "") or "").replace("/", "_")
    shdon = int(inv.get("shdon") or 0)
    return f"{khmshdon}_{khhdon}_{shdon}.{ext}"


def _write_file(path: str, data: bytes) -> None:
    """Ghi .tmp rồi đổi tên — bị ngắt giữa chừng không để lại file dở."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def download_invoices_xml_batch(token: str, invoice_list: list,
                                output_dir: str = "./downloads",
                                company_mst: str = "",
                                delay: float = 0.5,
                                max_retry: int = 3,
                                on_progress=None,
                                zip_dir: str | None = None) -> list:
    """
    Tải XML nhiều hóa đơn, lưu theo:
        <output_dir>/<MST_công_ty>/<năm>/<tháng>/<MST người bán>/{khmshdon}_{khhdon}_{shdon}.xml
    zip_dir: nếu có, lưu thêm ZIP gốc từ CQT (trước khi giải nén) cùng cấu trúc thư mục:
        <zip_dir>/<MST_công_ty>/<năm>/<tháng>/<MST người bán>/{khmshdon}_{khhdon}_{shdon}.zip
    (overwrite nếu đã tồn tại). Returns list[{inv, filepath|None, zip_path|None, error|None}].
    """
    import os
    import zipfile

    os.makedirs(output_dir, exist_ok=True)
    results = []
    total = len(invoice_list)

    for i, inv in enumerate(invoice_list):
        khmshdon = int(inv.get("khmshdon") or 1)
        shdon = int(inv.get("shdon") or 0)
        sub_dir = os.path.join(output_dir, invoice_rel_dir(inv, company_mst))
        os.makedirs(sub_dir, exist_ok=True)
        filepath = os.path.join(sub_dir, invoice_filename(inv, "xml"))

        err = None
        zip_bytes = None
        for attempt in range(max_retry):
            try:
                zip_bytes = _download_zip(
                    token, inv.get("nbmst", ""), inv.get("khhdon", ""),
                    shdon, khmshdon, use_sco=inv.get("_loai_hd") == "MTT")
                break
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < max_retry - 1:
                    time.sleep(2 ** attempt)
                    continue
                err = msg
                break

        zip_path = None
        if zip_bytes and zip_dir:
            # Lưu ZIP gốc TRƯỚC khi giải nén: XML lỗi vẫn còn bản gốc để đối chiếu
            zip_path = os.path.join(zip_dir, invoice_rel_dir(inv, company_mst),
                                    invoice_filename(inv, "zip"))
            _write_file(zip_path, zip_bytes)

        if zip_bytes and not err:
            try:
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                    xmls = [f for f in z.namelist() if f.endswith(".xml")]
                    if xmls:
                        with open(filepath, "wb") as f:
                            f.write(z.read(xmls[0]))
                    else:
                        err = "ZIP không chứa file XML"
            except Exception as e:
                err = f"Lỗi giải nén: {e}"

        results.append({"inv": inv,
                        "filepath": filepath if zip_bytes and not err else None,
                        "zip_path": zip_path,
                        "error": err})
        if on_progress:
            on_progress(i + 1, total)
        if zip_bytes and i < total - 1:
            time.sleep(delay)
    return results
