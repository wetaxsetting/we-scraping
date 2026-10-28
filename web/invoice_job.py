# invoice_job.py
"""
Job tự động lấy hóa đơn: XML từ GDT → parse → NCC; rồi PDF bản gốc từ site tra cứu.
Có hộp chờ: lỗi → hẹn lượt sau; HĐ đã xử lý → không lấy lại.

Lệnh:
    python web/invoice_job.py init-db
    python web/invoice_job.py add-company --mst 1201496252 --username 1201496252-U002 [--name ...]
    python web/invoice_job.py tick                 # Task Scheduler gọi 2–3 lần/ngày = xml + mail + pdf
    python web/invoice_job.py xml [--company MST] [--limit N]   # B1: chỉ GDT (danh sách + XML)
    python web/invoice_job.py pdf [--company MST] [--limit N]   # B2: chỉ PDF từ site NCC, không gọi GDT
    python web/invoice_job.py mail [--days 7] [--limit N]       # chỉ đọc mail hóa đơn (IMAP, chỉ đọc)
    python web/invoice_job.py sweep [--month 2026-09]
    # Mô hình hàng đợi (thay cho tick): sync-lists sinh việc, worker làm việc liên tục
    python web/invoice_job.py sync-lists [--company MST]        # 3 lần/ngày: danh sách → hộp chờ XML
    python web/invoice_job.py worker --type xml|pdf [--once]    # chạy mãi, xoay vòng qua các công ty
    python web/invoice_job.py status
    python web/invoice_job.py requeue --status dead|no_source [--type xml|pdf] [--mst-tcgp X]
    python web/invoice_job.py set-provider --mst-tcgp X --source-id Y [--name ...]

Cấu hình (biến môi trường hoặc file .env ở thư mục gốc project):
    DATABASE_URL          postgresql://user:pass@host:5432/dbname
    HDT_PASSWORD_<MST>    mật khẩu GDT từng công ty, vd HDT_PASSWORD_1201496252
    HDT_PASSWORD          dùng khi chỉ có 1 công ty
    INVOICE_OUTPUT_DIR    mặc định <root>/output
    INVOICE_TYPES         mặc định "mua-all,ban-all"
    INVOICE_SAVE_ZIP      mặc định 1: lưu ZIP gốc từ GDT vào <output>/temp/ (cùng cấu trúc xml/)
    INVOICE_LIST_SOURCE   excel (mặc định): danh sách lấy bằng nút "Xuất Excel" của cổng, 1 request
                          mỗi loại HĐ, file gốc lưu ở <output>/list/ | json: API danh sách phân trang
    INVOICE_MAIL_USER/HOST/PORT/FOLDER/DAYS   hộp mail hóa đơn (IMAP SSL; không có USER → bỏ bước mail)
    INVOICE_MAIL_AUTH     password (mặc định) | oauth2_microsoft | oauth2_google — xem .env.example

Một lượt `tick`, cho từng công ty:
    1. Token: dùng JWT còn hạn trong DB; hết hạn / bị 401 mới đăng nhập (giải captcha).
    2. Lấy NGUYÊN danh sách 30 ngày gần nhất (+ lấp khoảng trống nếu job nghỉ lâu) cho mọi loại HĐ
       trước, rồi mới tải XML. HĐ đã có → bỏ qua. HĐ mới → vào hộp chờ (task XML).
    3. Hàng đợi XML (cũ trước, nên HĐ lỗi lượt trước được xử lý trước HĐ mới).
    4. Mail hóa đơn (nếu cấu hình): XML cho HĐ Thuế không có XML, PDF đính kèm, link tra cứu.
    5. Hàng đợi PDF (không cần GDT, vẫn chạy khi đăng nhập GDT lỗi).
    GDT lỗi tạm thời 5 lần liên tiếp → dừng phần GDT, phần còn lại chờ lượt sau.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests                                   # noqa: E402
from dotenv import load_dotenv                    # noqa: E402

load_dotenv(ROOT / ".env")

import api_client                                 # noqa: E402
import job_policy                                 # noqa: E402
import mail_ingest                                # noqa: E402
import mail_reader                                # noqa: E402
import pdf_providers                              # noqa: E402
import xml_parser                                 # noqa: E402
from api_client import GDTAuthError, GDTError     # noqa: E402
from invoice_store import InvoiceStore            # noqa: E402
from pdf_providers import PdfFetchError           # noqa: E402

log = logging.getLogger("invoice_job")

VN_TZ = timezone(timedelta(hours=7))
XML_THROTTLE_SECONDS = 0.7      # nhịp gọi GDT, giữ như app tham khảo
RATE_LIMIT_PAUSE_SECONDS = 60   # GDT trả 429 khi tải XML: nghỉ 60, 120, 240, 300 s rồi mới ngắt mạch
RATE_LIMIT_PAUSE_MAX = 300
CLAIM_BATCH = 50
DEFAULT_XML_LIMIT = 300         # mỗi công ty mỗi lượt
DEFAULT_PDF_LIMIT = 50


class LoginError(Exception):
    """Không đăng nhập được GDT → dừng phần GDT của công ty trong lượt này."""


class TaskError(Exception):
    def __init__(self, msg: str, kind: str):
        super().__init__(msg)
        self.kind = kind


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------

def today_vn():
    return datetime.now(VN_TZ).date()


def output_dir() -> Path:
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output")


def save_zip_enabled() -> bool:
    """Lưu ZIP gốc từ GDT vào <out>/temp/ (mặc định bật; INVOICE_SAVE_ZIP=0 để tắt)."""
    return os.getenv("INVOICE_SAVE_ZIP", "1").strip().lower() not in ("0", "false", "no", "off")


def atomic_write(path: Path, data: bytes) -> None:
    """Ghi .tmp rồi đổi tên: job bị ngắt giữa chừng không để lại file dở."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def invoice_path(out_dir: Path, kind_dir: str, task: dict, company_mst: str, ext: str) -> Path:
    """<out>/<xml|pdf>/<MST công ty>/<năm>/<tháng>/<MST người bán>/{khmshdon}_{khhdon}_{shdon}.<ext>"""
    inv = {
        "tdlap": task["ngay_lap"].isoformat() if task.get("ngay_lap") else "",
        "nbmst": task["nbmst"], "khmshdon": task["khmshdon"],
        "khhdon": task["khhdon"], "shdon": task["shdon"],
    }
    return (out_dir / kind_dir / api_client.invoice_rel_dir(inv, company_mst)
            / api_client.invoice_filename(inv, ext))


def classify(exc: Exception) -> str:
    """Exception → error_kind cho hộp chờ / ngắt sớm."""
    if isinstance(exc, (GDTError, PdfFetchError, TaskError)):
        return exc.kind
    if isinstance(exc, requests.RequestException):
        return "network"
    msg = str(exc)
    if "429" in msg:
        return "rate_limit"
    if "không phản hồi" in msg:
        return "network"
    if isinstance(exc, RuntimeError):     # lỗi API GDT khác từ api_client
        return "server"
    return "other"


def gdt_password(company: dict) -> str:
    key = "HDT_PASSWORD_" + re.sub(r"[^0-9A-Za-z]", "_", company["company_mst"])
    return os.getenv(key) or os.getenv("HDT_PASSWORD", "")


# ---------------------------------------------------------------------------
# Token: dùng lại JWT còn hạn, hạn chế đăng nhập (= giải captcha)
# ---------------------------------------------------------------------------

class TokenProvider:
    MAX_LOGINS_PER_RUN = 2
    FALLBACK_TTL = timedelta(minutes=30)   # khi token không có claim exp

    def __init__(self, store: InvoiceStore, company: dict, stats: Counter):
        self.store = store
        self.company = company
        self.stats = stats
        self.logins = 0
        self._token = None

    @property
    def mst(self) -> str:
        return self.company["company_mst"]

    def get(self) -> str:
        if self._token is None:
            self._token = self.store.get_token(self.mst)
            if self._token:
                self.stats["token_reused"] += 1
            else:
                self._token = self._login_locked(rejected=None)
        return self._token

    def refresh(self) -> str:
        """GDT từ chối token (401/403) → bỏ token, đăng nhập lại."""
        self._token = self._login_locked(rejected=self._token)
        return self._token

    def _login_locked(self, rejected: str | None) -> str:
        """Đăng nhập dưới khóa theo công ty.

        Có khóa rồi thì đọc lại token trong DB trước: worker khác vừa đăng nhập xong
        (token mới, khác cái bị từ chối) thì dùng luôn, không giải captcha lần nữa.
        """
        with self.store.login_lock(self.mst):
            current = self.store.get_token(self.mst)
            if current and current != rejected:
                self.stats["token_reused"] += 1
                return current
            if rejected:
                self.store.invalidate_token(self.mst)
            return self._login()

    def _login(self) -> str:
        if self.logins >= self.MAX_LOGINS_PER_RUN:
            raise LoginError(f"Đã đăng nhập {self.logins} lần trong lượt này, dừng để tránh lặp captcha")
        password = gdt_password(self.company)
        if not password:
            raise LoginError(f"Thiếu mật khẩu: đặt HDT_PASSWORD_{self.mst} (hoặc HDT_PASSWORD)")
        self.logins += 1
        self.stats["logins"] += 1
        res = api_client.authenticate_auto(self.company["gdt_username"], password)
        if not res["ok"]:
            raise LoginError(f"Đăng nhập GDT thất bại sau {res['attempts']} lần: {res['message']}")
        token = res["token"]
        expires = api_client.jwt_expiry(token) or datetime.now(timezone.utc) + self.FALLBACK_TTL
        self.store.save_token(self.mst, token, expires)
        log.info("[%s] đăng nhập OK (captcha %d lần), token hết hạn %s",
                 self.mst, res["attempts"], expires.astimezone(VN_TZ).strftime("%d/%m %H:%M"))
        return token


def call_gdt(tokens: TokenProvider, fn):
    """Gọi fn(token). Token bị từ chối → đăng nhập lại 1 lần rồi gọi lại."""
    try:
        return fn(tokens.get())
    except GDTAuthError:
        log.info("[%s] GDT từ chối token → đăng nhập lại", tokens.mst)
        return fn(tokens.refresh())


# ---------------------------------------------------------------------------
# Bước 1: đồng bộ danh sách
# ---------------------------------------------------------------------------

def list_source() -> str:
    """Nguồn danh sách HĐ: 'excel' (mặc định) | 'json' (INVOICE_LIST_SOURCE)."""
    return "json" if os.getenv("INVOICE_LIST_SOURCE", "").strip().lower() == "json" else "excel"


def list_path(out_dir: Path, company_mst: str, sub_type: str, start, end) -> Path:
    """<out>/list/<MST công ty>/<sub_type>_<từ ngày>_<đến ngày>.xlsx — file gốc GDT xuất."""
    return out_dir / "list" / company_mst / f"{sub_type}_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"


def fetch_list(tokens, company_mst: str, sub_type: str, start, end, stats) -> list:
    """
    Danh sách HĐ 1 sub-type trong 1 khoảng ngày (≤ 30 ngày). Mặc định lấy file Excel như nút
    "Xuất Excel" của cổng (1 request), lưu file gốc vào <out>/list/ rồi mới đọc.
    GDT từ chối xuất / file không đúng mẫu → lấy lại khoảng đó qua API danh sách JSON.
    """
    if list_source() == "excel":
        try:
            data = call_gdt(tokens, lambda tok: api_client.export_invoice_list(
                tok, sub_type, start.isoformat(), end.isoformat()))
            try:
                atomic_write(list_path(output_dir(), company_mst, sub_type, start, end), data)
            except OSError as e:         # file đang mở trong Excel... → không chặn việc lấy danh sách
                log.warning("[%s] không lưu được file danh sách %s: %s", company_mst, sub_type, e)
            return api_client.parse_invoice_list_excel(data, sub_type)
        except GDTError as e:
            if e.kind not in ("other", "bad_content"):
                raise
            stats["list_json_fallback"] += 1
            log.warning("[%s] xuất Excel %s %s→%s không dùng được (%s): %s → lấy qua API JSON",
                        company_mst, sub_type, start, end, e.kind, e)
    return call_gdt(tokens, lambda tok: api_client.fetch_invoices_subtype(
        tok, sub_type, start.isoformat(), end.isoformat()))


def sync_lists(store, company, tokens, sub_types, windows_for, breaker, stats,
               move_cursor: bool = True) -> None:
    mst = company["company_mst"]
    for st in sub_types:
        last_ok = None
        for start, end in windows_for(st):
            if breaker.open:
                return
            try:
                rows = fetch_list(tokens, mst, st, start, end, stats)
            except LoginError:
                raise
            except Exception as e:  # noqa: BLE001
                kind = classify(e)
                breaker.record(False, kind)
                stats["sync_errors"] += 1
                log.warning("[%s] lỗi lấy danh sách %s %s→%s (%s): %s",
                            mst, st, start, end, kind, e)
                break       # đoạn sau phụ thuộc đoạn trước (mốc đồng bộ) → dừng sub-type này
            breaker.record(True)
            normalized = [n for n in (job_policy.normalize_list_row(r, mst, st) for r in rows) if n]
            if len(normalized) < len(rows):
                log.warning("[%s] bỏ %d dòng thiếu nbmst/khhdon/shdon", mst, len(rows) - len(normalized))
            # Danh sách GDT mỗi lần một khác (nhiều/ít/thiếu/trùng): hộp chờ chỉ nhận HĐ chưa có trong DB
            # (upsert_invoices bỏ qua HĐ đã có, kể cả đã xong); HĐ vắng mặt lần này không bị xóa.
            distinct = len({(n["nbmst"], n["khmshdon"], n["khhdon"], n["shdon"]) for n in normalized})
            new = store.upsert_invoices(normalized)
            stats["listed"] += len(rows)
            stats["new_invoices"] += new
            log.info("[%s] %s %s→%s: GDT trả %d HĐ%s → đã có %d (bỏ qua), mới %d (vào hộp chờ)",
                     mst, st, start, end, distinct,
                     f" ({len(normalized) - distinct} dòng trùng)" if len(normalized) > distinct else "",
                     distinct - new, new)
            last_ok = end
        if move_cursor and last_ok:
            store.set_cursor(mst, st, last_ok)


# ---------------------------------------------------------------------------
# Bước 2: hàng đợi XML
# ---------------------------------------------------------------------------

def _do_xml_task(store, company, tokens, task, run_id, out_dir) -> None:
    api_row = {"nbmst": task["nbmst"], "khhdon": task["khhdon"], "shdon": task["shdon"],
               "khmshdon": task["khmshdon"], "_sub_type": task["sub_type"]}
    zip_bytes = call_gdt(tokens, lambda tok: api_client.download_invoice_zip(tok, api_row))
    if save_zip_enabled():
        # Lưu ZIP gốc TRƯỚC khi giải nén/parse: XML lỗi vẫn còn bản gốc để đối chiếu.
        atomic_write(invoice_path(out_dir, "temp", task, company["company_mst"], "zip"), zip_bytes)
    try:
        xml_bytes = xml_parser.extract_xml(zip_bytes)
    except ValueError as e:
        raise TaskError(str(e), "bad_content") from e
    try:
        parsed = xml_parser.parse_xml_bytes(xml_bytes)
    except Exception as e:  # noqa: BLE001
        raise TaskError(f"XML không parse được: {e}", "parse") from e

    if parsed.get("canh_bao"):
        log.warning("[%s] XML %s_%s_%s lệch quy định QĐ 1233: %s", company["company_mst"],
                    task["khmshdon"], task["khhdon"], task["shdon"], "; ".join(parsed["canh_bao"]))

    path = invoice_path(out_dir, "xml", task, company["company_mst"], "xml")
    atomic_write(path, xml_bytes)
    partner = parsed.get("nguoi_ban") if task["direction"] == "mua" else parsed.get("nguoi_mua")
    store.complete_xml(task, run_id, path.relative_to(out_dir).as_posix(),
                       hashlib.sha256(xml_bytes).hexdigest(), parsed, partner)
    _log_xml(company["company_mst"], task, parsed, path.relative_to(out_dir).as_posix())


def _label(task: dict) -> str:
    return f"{task['khmshdon']}_{task['khhdon']}_{task['shdon']}"


def _money(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _log_xml(mst: str, task: dict, parsed: dict, path_rel: str) -> None:
    """1 dòng log / HĐ để đối chiếu: người bán, ngày, tổng tiền, số dòng hàng, NCC HĐĐT, mã tra cứu."""
    tc = parsed.get("thong_tin_chung") or {}
    nb = parsed.get("nguoi_ban") or {}
    xml_total = (parsed.get("thanh_toan") or {}).get("tong_tien_tt")
    log.info("[%s] XML OK %s | bán %s %s | ngày %s | tổng %s | %d dòng hàng | MSTTCGP %s | mã tra cứu %s | %s",
             mst, _label(task), nb.get("mst") or task["nbmst"], (nb.get("ten") or "")[:40],
             tc.get("ngay_lap") or task.get("ngay_lap"), _money(xml_total), len(parsed.get("hang_hoa") or []),
             tc.get("mst_tcgp") or "-", parsed.get("ma_tra_cuu") or "-", path_rel)
    try:                                    # danh sách GDT và XML phải khớp tổng tiền
        if task.get("tong_tien") is not None and xml_total is not None \
                and abs(float(task["tong_tien"]) - float(xml_total)) >= 1:
            log.warning("[%s] XML %s lệch tổng tiền: danh sách GDT %s, XML %s", mst, _label(task),
                        _money(task["tong_tien"]), _money(xml_total))
    except (TypeError, ValueError):
        pass


def log_box(store) -> None:
    """Cuối lượt: hộp chờ còn bao nhiêu việc, theo loại / trạng thái."""
    counts = store.status_report()["counts"]
    for tt in ("xml", "pdf"):
        parts = [f"{r['status']} {r['n']}" for r in counts if r["task_type"] == tt]
        log.info("Hộp chờ %s: %s", tt.upper(), ", ".join(parts) or "trống")


def _no_xml_on_gdt(task: dict, kind: str) -> bool:
    """
    HĐ lấy từ danh sách Excel (không có hthdon): HĐ không mã (ký hiệu 'K...') mà GDT trả 500 khi tải
    XML = HĐ gửi Thuế theo bảng tổng hợp. Dữ liệu thật 10/2026: 19/19 HĐ hthdon=2 trả 500, 30/30 HĐ
    không mã hthdon=0 tải được → cho nằm chờ như hthdon=2, không thử lại suốt 10 ngày.
    """
    return (kind == "not_ready" and task.get("hthdon") is None
            and str(task.get("khhdon") or "").upper().startswith("K"))


def process_xml(store, company, tokens, breaker, run_id, limit, stats, out_dir) -> None:
    mst = company["company_mst"]
    handled = 0
    while handled < limit and not breaker.open:
        batch = store.claim_tasks("xml", mst, min(CLAIM_BATCH, limit - handled))
        if not batch:
            break
        handled += xml_batch(store, company, tokens, breaker, run_id, batch, stats, out_dir)


def xml_batch(store, company, tokens, breaker, run_id, batch, stats, out_dir,
              release_delay: int = 0) -> int:
    """
    Tải XML cho 1 mẻ task đã nhận (dùng chung cho `tick`/`xml` và worker). Trả về số
    task đã thử. GDT lỗi liên tiếp → phần còn lại trả về hộp chờ (sau `release_delay`
    phút); không đăng nhập được → trả phần còn lại rồi ném LoginError.
    """
    mst = company["company_mst"]
    handled = 0
    for i, task in enumerate(batch):
        if breaker.open:
            store.release_tasks([t["task_id"] for t in batch[i:]], **_delay(release_delay))
            break
        try:
            _do_xml_task(store, company, tokens, task, run_id, out_dir)
            breaker.record(True)
            stats["xml_ok"] += 1
        except LoginError:
            store.release_tasks([t["task_id"] for t in batch[i:]], **_delay(release_delay))
            raise
        except Exception as e:  # noqa: BLE001
            kind = classify(e)
            breaker.record(False, kind)
            if kind == "rate_limit" and not breaker.open:
                # GDT 429: nghỉ tăng dần thay vì gọi tiếp 0,7 s/HĐ — trước đây 5 lần 429 trong 4 giây
                # làm ngắt mạch dừng cả lượt main_auto (26/10/2026, còn 44/47 đoạn chưa chạy)
                wait = min(RATE_LIMIT_PAUSE_SECONDS * 2 ** (breaker.consecutive - 1), RATE_LIMIT_PAUSE_MAX)
                log.warning("[%s] GDT 429 → nghỉ %d s", mst, wait)
                time.sleep(wait)
            if _no_xml_on_gdt(task, kind):
                store.mark_no_source(task, "HĐ không mã, Thuế không có XML (500) → coi như gửi "
                                     "theo bảng tổng hợp; chờ XML/PDF từ mail", run_id,
                                     kind="summary_only")
                stats["xml_summary_only"] += 1
                log.info("[%s] XML %s_%s_%s: HĐ không mã, Thuế không có XML → chờ nguồn khác",
                         mst, task["khmshdon"], task["khhdon"], task["shdon"])
            else:
                status = store.fail_task(task, kind, f"{type(e).__name__}: {e}", run_id)
                stats["xml_failed"] += 1
                if status == "dead":
                    stats["xml_dead"] += 1
                log.warning("[%s] XML %s_%s_%s lỗi %s (lần %d) → %s: %s", mst,
                            task["khmshdon"], task["khhdon"], task["shdon"],
                            kind, task["attempts"] + 1, status, e)
        handled += 1
        time.sleep(XML_THROTTLE_SECONDS)
    return handled


def _delay(minutes: int) -> dict:
    """release_tasks(..., delay_minutes=N) chỉ khi N > 0 — giữ chữ ký cũ cho lối `tick`."""
    return {"delay_minutes": minutes} if minutes else {}


# ---------------------------------------------------------------------------
# Bước 3: hàng đợi PDF
# ---------------------------------------------------------------------------

def _backfill_from_xml(task: dict, out_dir: Path) -> None:
    """XML parse trước khi có khóa dlhdon_id (BKAV cần) → đọc lại từ file XML đã lưu."""
    if task.get("dlhdon_id") is not None or not task.get("xml_path"):
        return
    try:
        parsed = xml_parser.parse_xml_bytes((out_dir / task["xml_path"]).read_bytes())
    except (OSError, ValueError) as e:
        log.warning("Không đọc lại được %s: %s", task["xml_path"], e)
        return
    task["dlhdon_id"] = parsed.get("dlhdon_id") or ""


def process_pdf(store, company, run_id, limit, stats, out_dir) -> None:
    mst = company["company_mst"]
    breakers: dict = {}     # mỗi site tra cứu một bộ ngắt riêng
    deferred: list = []     # task bỏ qua vì site đang lỗi → trả lại cuối bước
    handled = 0
    try:
        while handled < limit:
            batch = store.claim_tasks("pdf", mst, min(CLAIM_BATCH, limit - handled))
            if not batch:
                break
            handled += pdf_batch(store, mst, run_id, batch, stats, out_dir, breakers, deferred,
                                 limit - handled)
    finally:
        store.release_tasks(deferred)


def pick_adapter(store, task: dict):
    """
    source_id gắn tay (set-provider) → MSTTCGP. XML không có MSTTCGP (HĐ máy tính tiền) thì
    theo host link tra cứu trong XML, rồi theo NCC quen của người bán (HĐ khác của họ có MSTTCGP:
    vd 294 HĐ MTT của Grab đi cổng Grab của HILO, 23/10).
    """
    adapter = (pdf_providers.get_adapter(task.get("source_id"))
               or pdf_providers.adapter_for_mst_tcgp(task.get("mst_tcgp")))
    if adapter is not None or task.get("mst_tcgp"):
        return adapter
    tt_khac = task.get("tt_khac") if isinstance(task.get("tt_khac"), dict) else {}
    for url in (task.get("lookup_url"), tt_khac.get("PortalLink")):
        adapter = pdf_providers.adapter_for_host(urlsplit(url or "").hostname)
        if adapter is not None:
            return adapter
    nbmst = task.get("nbmst")
    if not nbmst:
        return None
    cache = store.__dict__.setdefault("_seller_tcgp", {})       # 1 truy vấn / người bán / tiến trình
    if nbmst not in cache:
        found = store.seller_mst_tcgp(nbmst)
        if found is None:
            return None                     # chưa biết thì không nhớ: HĐ thường của họ có thể về sau
        cache[nbmst] = found
    return pdf_providers.adapter_for_mst_tcgp(cache[nbmst])


def pdf_batch(store, mst, run_id, batch, stats, out_dir, breakers, deferred,
              limit: int | None = None) -> int:
    """
    Lấy PDF cho 1 mẻ task đã nhận (dùng chung cho `tick`/`pdf` và worker). Site NCC đang
    ngắt mạch, hoặc đã đủ `limit` HĐ gọi site → task vào `deferred` để người gọi trả lại.
    Trả về số HĐ thật sự gọi site NCC.
    """
    handled = 0
    for task in batch:
        adapter = pick_adapter(store, task)
        if adapter is None:
            reason = (f"Chưa có adapter cho MSTTCGP {task.get('mst_tcgp') or '?'}"
                      if not task.get("source_id")
                      else f"source_id {task['source_id']!r} chưa đăng ký trong pdf_providers")
            store.mark_no_source(task, reason, run_id)
            stats["pdf_no_source"] += 1
            continue
        br = breakers.setdefault(adapter.source_id, job_policy.CircuitBreaker())
        if br.open or (limit is not None and handled >= limit):
            deferred.append(task["task_id"])
            continue
        handled += 1        # --limit chỉ tính HĐ thật sự gọi site NCC
        try:
            _backfill_from_xml(task, out_dir)
            pdf = adapter.fetch_pdf(dict(task))
            if not pdf.startswith(b"%PDF") or len(pdf) < 1000:
                raise TaskError("Nội dung tải về không phải PDF hợp lệ", "bad_content")
            path = invoice_path(out_dir, "pdf", task, mst, "pdf")
            atomic_write(path, pdf)
            store.complete_pdf(task, run_id, path.relative_to(out_dir).as_posix(),
                               hashlib.sha256(pdf).hexdigest(), adapter.source_id)
            br.record(True)
            stats["pdf_ok"] += 1
            log.info("[%s] PDF OK %s | bán %s | %s → %s", mst, _label(task), task["nbmst"],
                     adapter.source_id, path.relative_to(out_dir).as_posix())
        except Exception as e:  # noqa: BLE001
            kind = classify(e)
            if kind == "no_portal":
                # Adapter có nhưng chưa biết cổng của người bán: nằm chờ, không tốn lượt
                store.mark_no_source(task, str(e), run_id, kind="no_portal")
                stats["pdf_no_portal"] += 1
                continue
            br.record(False, kind)
            status = store.fail_task(task, kind, f"{type(e).__name__}: {e}", run_id)
            stats["pdf_failed"] += 1
            log.warning("[%s] PDF %s_%s_%s (%s) lỗi %s → %s: %s", mst, task["khmshdon"],
                        task["khhdon"], task["shdon"], adapter.source_id, kind, status, e)
    return handled


# ---------------------------------------------------------------------------
# Lệnh
# ---------------------------------------------------------------------------

def _sub_types() -> list:
    raw = os.getenv("INVOICE_TYPES", "mua-all,ban-all")
    return api_client.expand_invoice_types([t.strip() for t in raw.split(",") if t.strip()])


def _companies(store, only: str | None) -> list:
    companies = store.active_companies()
    if only:
        companies = [c for c in companies if c["company_mst"] == only]
    if not companies:
        log.warning("Chưa có công ty nào: chạy add-company trước")
    return companies


def run_sweep(store, companies, month: str | None) -> bool:
    """Quét lại toàn bộ 1 tháng (mặc định tháng trước) để bắt HĐ lên GDT muộn."""
    first, last = job_policy.month_bounds(month, today_vn())
    run_id = store.start_run("sweep")
    all_stats, ok = {}, True
    try:
        for company in companies:
            stats = Counter()
            store.begin_run_company(run_id, company["company_mst"])
            breaker = job_policy.CircuitBreaker()
            try:
                sync_lists(store, company, TokenProvider(store, company, stats), _sub_types(),
                           lambda _st: job_policy.chunk_range(first, last), breaker, stats,
                           move_cursor=False)
            except LoginError as e:
                stats["login_error"] = str(e)
                log.error("[%s] sweep: %s", company["company_mst"], e)
            ok = ok and not stats["sync_errors"] and "login_error" not in stats and not breaker.open
            all_stats[company["company_mst"]] = dict(stats)
            store.finish_run_company(run_id, company["company_mst"],
                                     _company_status(stats, breaker), dict(stats))
            log.info("[%s] sweep %s→%s: %d HĐ mới", company["company_mst"], first, last,
                     stats["new_invoices"])
    finally:
        store.finish_run(run_id, "ok" if ok else "partial",
                         {"month": f"{first:%Y-%m}", "companies": all_stats})
    return ok


def _company_status(stats, breaker=None) -> str:
    """Trạng thái 1 công ty trong 1 lượt, cho job_run_companies.

    'error' = công ty đó không làm được gì (không đăng nhập được GDT); 'partial' = có
    phần chạy được nhưng có HĐ lỗi hoặc GDT/site NCC lỗi liên tiếp phải dừng sớm.
    """
    if "login_error" in stats:
        return "error"
    if (stats["sync_errors"] or stats["xml_failed"] or stats["pdf_failed"]
            or (breaker is not None and breaker.open)):
        return "partial"
    return "ok"


def _prepare_queues(store) -> None:
    reclaimed = store.reclaim_stale()
    if reclaimed:
        log.info("Thu hồi %d task kẹt từ lượt trước", reclaimed)
    # Đơn vị cung cấp HĐĐT vừa có adapter (code mới) → HĐ đang chờ 'no_source' chạy lại
    for mst_tcgp in pdf_providers.supported_mst_tcgp():
        n = store.requeue("no_source", task_type="pdf", mst_tcgp=mst_tcgp,
                          error_kind="no_source")      # 'no_portal' chờ bổ sung cổng thủ công
        if n:
            log.info("MSTTCGP %s đã có adapter → %d HĐ quay lại hàng đợi PDF", mst_tcgp, n)
    # HĐ máy tính tiền không có MSTTCGP: định tuyến được (link / NCC quen của người bán) → chạy lại
    routable = [t["task_id"] for t in store.no_source_without_tcgp() if pick_adapter(store, t) is not None]
    n = store.requeue_tasks(routable)
    if n:
        log.info("HĐ không có MSTTCGP đã định tuyến được NCC → %d HĐ quay lại hàng đợi PDF", n)


def cmd_pdf(store, args) -> int:
    """Chỉ chạy hàng đợi PDF: không đăng nhập GDT, không tốn captcha GDT."""
    if not store.try_lock():
        log.info("Đang có lượt chạy khác → thoát")
        return 0
    try:
        _prepare_queues(store)
        out_dir = output_dir()
        run_id = store.start_run("pdf")
        all_stats, overall = {}, "ok"
        try:
            for company in _companies(store, args.company):
                mst = company["company_mst"]
                stats = Counter()
                store.begin_run_company(run_id, mst)
                process_pdf(store, company, run_id, args.limit, stats, out_dir)
                if stats["pdf_failed"]:
                    overall = "partial"
                all_stats[mst] = dict(stats)
                store.finish_run_company(run_id, mst, _company_status(stats), dict(stats))
                log.info("[%s] PDF xong: %s", mst, dict(stats))
        except Exception:
            overall = "error"
            raise
        finally:
            store.finish_run(run_id, overall, {"companies": all_stats})
        log_box(store)
        return 0
    finally:
        store.unlock()


def run_mail_step(store, run_id, out_dir, days: int | None = None, limit: int | None = None):
    """Đọc mail hóa đơn (nếu .env có INVOICE_MAIL_*). Trả về Counter, hoặc None khi chưa cấu hình."""
    stats = Counter()
    try:
        cfg = mail_ingest.mail_config()
    except mail_ingest.MailConfigError as e:          # cấu hình sai: báo, không làm hỏng cả lượt tick
        stats["login_error"] = f"Cấu hình mail: {e}"
        log.error("Mail: %s", e)
        return stats
    if cfg is None:
        return None
    for folder in cfg["folders"]:
        try:
            with mail_ingest.open_mailbox(cfg, folder) as mb:
                mail_ingest.run(store, mb, out_dir, run_id, days or cfg["days"], stats, limit)
        except mail_reader.MailError as e:
            stats["login_error"] = str(e)
            log.error("Mail [%s]: %s", folder, e)
    # Mail chỉ có link/mã → adapter của cổng tra cứu (1 lần mỗi lượt, sau khi đọc xong mọi thư mục)
    mail_ingest.process_links(store, out_dir, run_id, stats, limit)
    log.info("Mail xong: %s", dict(stats))
    return stats


def cmd_mail(store, args) -> int:
    """Chỉ đọc mail hóa đơn (không GDT, không site NCC)."""
    try:
        if mail_ingest.mail_config() is None:
            log.error("Chưa cấu hình mail: thiếu INVOICE_MAIL_USER trong .env (xem .env.example)")
            return 2
    except mail_ingest.MailConfigError as e:
        log.error("Cấu hình mail: %s", e)
        return 2
    if not store.try_lock():
        log.info("Đang có lượt chạy khác → thoát")
        return 0
    try:
        run_id = store.start_run("mail")
        stats = Counter()
        try:
            stats = run_mail_step(store, run_id, output_dir(), args.days, args.limit)
        finally:
            bad = stats["mail_error"] or "login_error" in stats
            store.finish_run(run_id, "partial" if bad else "ok", {"mail": dict(stats)})
        return 1 if "login_error" in stats else 0
    finally:
        store.unlock()


def run_xml_step(store, companies, run_id, out_dir, xml_limit: int) -> tuple:
    """
    Phần GDT (dùng chung cho `tick` và `xml`): ngày 5/15 quét lại tháng trước; đồng bộ danh sách
    30 ngày gần nhất; tải XML trong hàng đợi. Trả về ({MST: Counter}, ok).
    """
    today = today_vn()
    if today.day in job_policy.SWEEP_DAYS and not store.last_ok_run("sweep", today):
        log.info("Hôm nay ngày %d → quét lại tháng trước", today.day)
        run_sweep(store, companies, None)
    per_company, ok = {}, True
    for company in companies:
        mst = company["company_mst"]
        stats = per_company[mst] = Counter()
        store.begin_run_company(run_id, mst)
        tokens = TokenProvider(store, company, stats)
        breaker = job_policy.CircuitBreaker()
        try:
            sync_lists(store, company, tokens, _sub_types(),
                       lambda st: job_policy.sync_windows(store.get_cursor(mst, st), today),
                       breaker, stats)
            process_xml(store, company, tokens, breaker, run_id, xml_limit, stats, out_dir)
        except LoginError as e:
            stats["login_error"] = str(e)
            log.error("[%s] %s", mst, e)
        if breaker.open:
            stats["stopped_early"] = 1
            log.warning("[%s] GDT lỗi liên tiếp → dừng phần GDT, chờ lượt sau", mst)
        if stats["sync_errors"] or "login_error" in stats or breaker.open:
            ok = False
        store.finish_run_company(run_id, mst, _company_status(stats, breaker), dict(stats))
    return per_company, ok


def cmd_xml(store, args) -> int:
    """Chỉ phần GDT: đồng bộ danh sách + tải XML. Không đọc mail, không gọi site NCC."""
    if not store.try_lock():
        log.info("Đang có lượt chạy khác → thoát")
        return 0
    try:
        _prepare_queues(store)
        companies = _companies(store, args.company)
        run_id = store.start_run("xml")
        per_company, ok = {}, False
        try:
            per_company, ok = run_xml_step(store, companies, run_id, output_dir(), args.limit)
            for mst, stats in per_company.items():
                log.info("[%s] XML xong: %s", mst, dict(stats))
        except Exception:
            store.finish_run(run_id, "error", {"companies": {m: dict(s) for m, s in per_company.items()}})
            raise
        store.finish_run(run_id, "ok" if ok else "partial",
                         {"companies": {m: dict(s) for m, s in per_company.items()}})
        log_box(store)
        return 0 if ok else 1
    finally:
        store.unlock()


def cmd_tick(store, args) -> int:
    if not store.try_lock():
        log.info("Đang có lượt chạy khác → thoát")
        return 0
    try:
        _prepare_queues(store)
        companies = _companies(store, args.company)
        out_dir = output_dir()
        run_id = store.start_run("tick")
        all_stats, overall, mail_stats = {}, "ok", None
        per_company = {}
        try:
            # 1. GDT: danh sách + XML  (= lệnh `xml`)
            per_company, gdt_ok = run_xml_step(store, companies, run_id, out_dir, args.xml_limit)
            if not gdt_ok:
                overall = "partial"
            # 2. Mail: XML cho HĐ Thuế không có XML + PDF đính kèm (trước bước PDF → đỡ gọi site NCC)
            mail_stats = run_mail_step(store, run_id, out_dir)
            if mail_stats and (mail_stats["mail_error"] or "login_error" in mail_stats):
                overall = "partial"
            # 3. PDF từ site tra cứu của NCC  (= lệnh `pdf`)
            for company in companies:
                mst = company["company_mst"]
                stats = per_company[mst]
                store.begin_run_company(run_id, mst)
                process_pdf(store, company, run_id, args.pdf_limit, stats, out_dir)
                all_stats[mst] = dict(stats)
                store.finish_run_company(run_id, mst, _company_status(stats), dict(stats))
                log.info("[%s] xong: %s", mst, dict(stats))
        except Exception:
            overall = "error"
            raise
        finally:
            store.finish_run(run_id, overall, {"companies": all_stats,
                                               "mail": dict(mail_stats) if mail_stats else None})
        log_box(store)
        return 0
    finally:
        store.unlock()


# ---------------------------------------------------------------------------
# Mô hình hàng đợi: sync-lists (3 lần/ngày) sinh việc, worker chạy liên tục làm việc
# ---------------------------------------------------------------------------

def cmd_sync_lists(store, args) -> int:
    """
    Chỉ lấy danh sách GDT → HĐ mới vào hộp chờ XML. Không tải XML (việc của worker).
    Ngày 5/15 quét lại tháng trước như `tick`. Khóa riêng: không chạy chồng chính nó,
    nhưng không chặn worker hay `tick`.
    """
    from invoice_store import SYNC_LOCK_KEY
    if not store.try_lock(SYNC_LOCK_KEY):
        log.info("sync-lists khác đang chạy → thoát")
        return 0
    try:
        companies = _companies(store, args.company)
        today = today_vn()
        if today.day in job_policy.SWEEP_DAYS and not store.last_ok_run("sweep", today):
            log.info("Hôm nay ngày %d → quét lại tháng trước", today.day)
            run_sweep(store, companies, None)
        run_id = store.start_run("sync-lists")
        all_stats, ok = {}, True
        try:
            for company in companies:
                mst = company["company_mst"]
                stats = Counter()
                store.begin_run_company(run_id, mst)
                breaker = job_policy.CircuitBreaker()
                try:
                    sync_lists(store, company, TokenProvider(store, company, stats), _sub_types(),
                               lambda st, m=mst: job_policy.sync_windows(store.get_cursor(m, st), today),
                               breaker, stats)
                except LoginError as e:
                    stats["login_error"] = str(e)
                    log.error("[%s] %s", mst, e)
                if stats["sync_errors"] or "login_error" in stats or breaker.open:
                    ok = False
                all_stats[mst] = dict(stats)
                store.finish_run_company(run_id, mst, _company_status(stats, breaker), dict(stats))
                log.info("[%s] danh sách xong: GDT trả %d HĐ, mới %d vào hộp chờ",
                         mst, stats["listed"], stats["new_invoices"])
        finally:
            store.finish_run(run_id, "ok" if ok else "partial", {"companies": all_stats})
        log_box(store)
        return 0 if ok else 1
    finally:
        store.unlock(SYNC_LOCK_KEY)


class _WorkerRun:
    """
    1 dòng job_runs cho 1 "đợt" việc của worker: mở khi nhận được việc, đóng khi hộp chờ
    rỗng hoặc đã quá ROLL_SECONDS. Worker ngồi chờ không sinh dòng nào. Token và bộ ngắt
    mạch GDT sống theo đợt, nên giới hạn số lần đăng nhập của TokenProvider cũng reset
    theo đợt chứ không khóa chết worker chạy nhiều ngày.
    """
    ROLL_SECONDS = 3600

    def __init__(self, store, command: str):
        self.store, self.command = store, command
        self.run_id = None

    def ensure(self) -> int:
        if self.run_id is None:
            self.run_id = self.store.start_run(self.command)
            self.started = time.monotonic()
            self.stats: dict = {}
            self.tokens: dict = {}
            self.gdt_breakers: dict = {}
            self.pdf_breakers: dict = {}
        return self.run_id

    def due(self) -> bool:
        return self.run_id is not None and time.monotonic() - self.started > self.ROLL_SECONDS

    def stats_for(self, mst: str) -> Counter:
        if mst not in self.stats:
            self.stats[mst] = Counter()
            self.store.begin_run_company(self.run_id, mst)
        return self.stats[mst]

    def save(self, mst: str) -> None:
        stats = self.stats[mst]
        self.store.finish_run_company(self.run_id, mst,
                                      _company_status(stats, self.gdt_breakers.get(mst)), dict(stats))

    def close(self, status: str | None = None) -> None:
        if self.run_id is None:
            return
        if status is None:
            status = ("partial" if any(_company_status(s, self.gdt_breakers.get(m)) != "ok"
                                       for m, s in self.stats.items()) else "ok")
        self.store.finish_run(self.run_id, status,
                              {"companies": {m: dict(s) for m, s in self.stats.items()}})
        self.run_id = None


WORKER_PER_COMPANY = 20     # mỗi công ty tối đa bấy nhiêu HĐ mỗi mẻ → xoay vòng
WORKER_BATCH = 200          # tổng HĐ mỗi mẻ
WORKER_IDLE_SECONDS = 60    # hộp chờ rỗng → ngủ chừng này rồi xem lại
COOLDOWN_MINUTES = 15       # công ty không đăng nhập được / GDT lỗi liên tiếp → nghỉ
DEFER_MINUTES = 10          # site NCC đang ngắt mạch → hẹn task lại sau


def run_worker(store, task_type: str, out_dir: Path, *, per_company: int = WORKER_PER_COMPANY,
               batch: int = WORKER_BATCH, idle: float = WORKER_IDLE_SECONDS, once: bool = False,
               stop=lambda: False) -> int:
    """
    Rút hộp chờ `task_type` ('xml' | 'pdf') liên tục, xoay vòng qua các công ty.
    `once`: hộp chờ rỗng thì thoát (để chạy thử / test) thay vì ngủ chờ việc mới.
    Không dùng khóa toàn cục: nhiều worker chạy song song, SKIP LOCKED lo phần chia việc.
    """
    run = _WorkerRun(store, f"worker-{task_type}")
    cooldown: dict = {}         # MST → time.monotonic() hết nghỉ
    claimed: list = []
    log_month = f"{today_vn():%Y-%m}"
    reclaimed = store.reclaim_stale()
    if reclaimed:
        log.info("Thu hồi %d task kẹt", reclaimed)
    try:
        while not stop():
            if run.due():
                run.close()
            if f"{today_vn():%Y-%m}" != log_month:     # chạy qua đầu tháng → sang file log mới
                log_month = f"{today_vn():%Y-%m}"
                if logging.getLogger().handlers:
                    _setup_logging()
            now = time.monotonic()
            for m in [m for m, until in cooldown.items() if until <= now]:
                del cooldown[m]
            claimed = store.claim_round_robin(task_type, per_company, batch, exclude_msts=list(cooldown))
            if not claimed:
                run.close()
                if once:
                    break
                time.sleep(idle)
                continue

            run_id = run.ensure()
            if task_type == "pdf" and not run.stats:
                _prepare_queues(store)          # NCC vừa có adapter → HĐ no_source quay lại
            companies = {c["company_mst"]: c for c in store.active_companies()}
            groups: dict = {}
            for task in claimed:
                groups.setdefault(task["company_mst"], []).append(task)

            for mst, tasks in groups.items():
                company = companies.get(mst)
                if company is None:                 # công ty đã tắt (active=false)
                    store.release_tasks([t["task_id"] for t in tasks], delay_minutes=COOLDOWN_MINUTES)
                    continue
                stats = run.stats_for(mst)
                if task_type == "xml":
                    tokens = run.tokens.setdefault(mst, TokenProvider(store, company, stats))
                    breaker = run.gdt_breakers.setdefault(mst, job_policy.CircuitBreaker())
                    try:
                        xml_batch(store, company, tokens, breaker, run_id, tasks, stats, out_dir,
                                  release_delay=COOLDOWN_MINUTES)
                    except LoginError as e:
                        stats["login_error"] = str(e)
                        cooldown[mst] = time.monotonic() + COOLDOWN_MINUTES * 60
                        log.error("[%s] %s → nghỉ %d phút", mst, e, COOLDOWN_MINUTES)
                    if breaker.open:
                        stats["stopped_early"] = 1
                        cooldown[mst] = time.monotonic() + COOLDOWN_MINUTES * 60
                        run.gdt_breakers[mst] = job_policy.CircuitBreaker()
                        log.warning("[%s] GDT lỗi liên tiếp → nghỉ %d phút", mst, COOLDOWN_MINUTES)
                else:
                    deferred: list = []
                    try:
                        pdf_batch(store, mst, run_id, tasks, stats, out_dir, run.pdf_breakers, deferred)
                    finally:
                        store.release_tasks(deferred, delay_minutes=DEFER_MINUTES)
                run.save(mst)
            claimed = []
        return 0
    except BaseException:
        # Ctrl+C / lỗi bất ngờ: trả mọi task mẻ này còn 'running' (task đã xong không bị đụng)
        store.release_tasks([t["task_id"] for t in claimed])
        run.close("error")
        raise
    finally:
        run.close()


def cmd_worker(store, args) -> int:
    log.info("Worker %s bắt đầu (mỗi công ty %d HĐ/mẻ, tối đa %d HĐ/mẻ)%s",
             args.type, args.per_company, args.batch, ", chạy tới khi hộp chờ rỗng" if args.once else "")
    try:
        return run_worker(store, args.type, output_dir(), per_company=args.per_company,
                          batch=args.batch, idle=args.idle, once=args.once)
    except KeyboardInterrupt:
        log.info("Worker %s dừng (Ctrl+C)", args.type)
        return 0


def cmd_sweep(store, args) -> int:
    if not store.try_lock():
        log.info("Đang có lượt chạy khác → thoát")
        return 0
    try:
        return 0 if run_sweep(store, _companies(store, args.company), args.month) else 1
    finally:
        store.unlock()


def cmd_status(store, args) -> int:
    rep = store.status_report()
    print("\nHộp chờ (task_type / status / số lượng):")
    for r in rep["counts"]:
        print(f"  {r['task_type']:4} {r['status']:10} {r['n']:>6}")
    if rep["errors"]:
        print("\nLỗi đang chờ thử lại / đã bỏ:")
        for r in rep["errors"]:
            print(f"  {r['task_type']:4} {r['status']:8} {r['error_kind']:12} {r['n']:>6}")
    if rep["providers"]:
        print("\nĐơn vị cung cấp HĐĐT chưa có adapter PDF:")
        for r in rep["providers"]:
            print(f"  MSTTCGP {r['mst_tcgp'] or '?':14} {r['n']:>5} HĐ  {r['name'] or ''}"
                  f"  {r['lookup_url'] or ''}")
    if rep["summary_only"]:
        total = sum(r["n"] for r in rep["summary_only"])
        print(f"\nHĐ chỉ có dữ liệu tổng hợp trên Thuế (hthdon=2 / HĐ không mã GDT trả 500, "
              f"không có XML) — {total} HĐ, "
              f"đã lưu phần đầu HĐ, chờ nguồn khác (email/file):")
        for r in rep["summary_only"]:
            print(f"  người bán {r['nbmst']:16} {r['n']:>5} HĐ  {r['nbten'] or ''}")
    if rep["no_portal"]:
        print("\nNgười bán chưa biết cổng tra cứu (bổ sung vào SELLER_PORTALS, xem 'Website tra cứu'"
              " in trên hóa đơn, rồi chạy: requeue --status no_source --mst-tcgp <MSTTCGP>):")
        for r in rep["no_portal"]:
            print(f"  MSTTCGP {r['mst_tcgp']:12} người bán {r['nbmst']:14} {r['n']:>5} HĐ  {r['nbten'] or ''}")
    if rep["mails"]:
        print("\nMail hóa đơn (theo trạng thái):",
              ", ".join(f"{r['status']} {r['n']}" for r in rep["mails"]))
        print("  File từ mail:", ", ".join(f"{r['kind']}/{r['status']} {r['n']}" for r in rep["mail_files"]))
    if rep["mail_links"]:
        print("\nLink tra cứu trong mail chưa xử lý (theo cổng):")
        for r in rep["mail_links"]:
            print(f"  {r['host']:40} {r['n']:>4} link, {r['with_code']:>4} có mã")
    print("\nLượt chạy gần nhất:")
    for r in rep["runs"]:
        started = r["started_at"].astimezone(VN_TZ).strftime("%d/%m %H:%M")
        print(f"  #{r['id']:<5} {r['command']:6} {started}  {r['status'] or 'đang chạy'}")
    return 0


def cmd_requeue(store, args) -> int:
    n = store.requeue(args.status, task_type=args.type, mst_tcgp=args.mst_tcgp)
    print(f"Đã đưa {n} task về hàng đợi")
    return 0


def cmd_set_provider(store, args) -> int:
    n = store.set_provider_source(args.mst_tcgp, args.source_id, args.name)
    if args.source_id and args.source_id not in pdf_providers.available():
        print(f"⚠ source_id {args.source_id!r} chưa có adapter trong pdf_providers "
              f"(hiện có: {pdf_providers.available() or 'chưa có'})")
    print(f"Đã gắn {args.mst_tcgp} → {args.source_id}; {n} HĐ quay lại hàng đợi PDF")
    return 0


def cmd_add_company(store, args) -> int:
    if not job_policy.is_valid_mst(args.mst):
        print(f"MST không hợp lệ: {args.mst!r} (dạng 0123456789 hoặc 0123456789-001)")
        return 1
    store.upsert_company(args.mst, args.username or args.mst, args.name)
    print(f"Đã thêm công ty {args.mst} (đăng nhập GDT bằng {args.username or args.mst})")
    return 0


def cmd_init_db(store, args) -> int:
    store.init_schema()
    print("Đã tạo/cập nhật schema")
    return 0


def _setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    log_dir = output_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_dir / f"invoice_job_{today_vn():%Y-%m}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    handlers = [fh]
    if sys.stdout is not None:          # pythonw (Task Scheduler) không có console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        handlers.append(sh)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = handlers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Job lấy hóa đơn GDT (XML) + PDF bản gốc")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="tạo/cập nhật schema")

    p = sub.add_parser("add-company", help="thêm/sửa công ty")
    p.add_argument("--mst", required=True)
    p.add_argument("--username", help="tài khoản GDT (mặc định = MST), vd 1201496252-U002")
    p.add_argument("--name")

    p = sub.add_parser("tick", help="1 lượt chạy (Task Scheduler gọi)")
    p.add_argument("--company", help="chỉ chạy cho 1 MST")
    p.add_argument("--xml-limit", type=int, default=DEFAULT_XML_LIMIT)
    p.add_argument("--pdf-limit", type=int, default=DEFAULT_PDF_LIMIT)

    p = sub.add_parser("xml", help="chỉ phần GDT: đồng bộ danh sách + tải XML (không mail, không PDF)")
    p.add_argument("--company", help="chỉ chạy cho 1 MST")
    p.add_argument("--limit", type=int, default=DEFAULT_XML_LIMIT, help="số XML tối đa mỗi công ty")

    p = sub.add_parser("pdf", help="chỉ tải PDF đang chờ (không đăng nhập GDT)")
    p.add_argument("--company", help="chỉ chạy cho 1 MST")
    p.add_argument("--limit", type=int, default=DEFAULT_PDF_LIMIT, help="số HĐ tối đa mỗi công ty")

    p = sub.add_parser("mail", help="đọc mail hóa đơn (XML/PDF đính kèm, link tra cứu) — chỉ đọc")
    p.add_argument("--days", type=int, help="số ngày gần nhất (mặc định INVOICE_MAIL_DAYS hoặc 7)")
    p.add_argument("--limit", type=int, help="xử lý tối đa N mail mới")

    p = sub.add_parser("sweep", help="quét lại danh sách cả tháng (mặc định tháng trước)")
    p.add_argument("--month", help="yyyy-mm")
    p.add_argument("--company")

    sub.add_parser("status", help="tình trạng hộp chờ")

    p = sub.add_parser("sync-lists", help="chỉ lấy danh sách GDT → HĐ mới vào hộp chờ (3 lần/ngày)")
    p.add_argument("--company", help="chỉ chạy cho 1 MST")

    p = sub.add_parser("worker", help="rút hộp chờ liên tục, xoay vòng qua các công ty")
    p.add_argument("--type", required=True, choices=["xml", "pdf"])
    p.add_argument("--per-company", type=int, default=WORKER_PER_COMPANY,
                   help="số HĐ tối đa mỗi công ty mỗi mẻ")
    p.add_argument("--batch", type=int, default=WORKER_BATCH, help="tổng số HĐ tối đa mỗi mẻ")
    p.add_argument("--idle", type=float, default=WORKER_IDLE_SECONDS,
                   help="hộp chờ rỗng thì ngủ bao nhiêu giây")
    p.add_argument("--once", action="store_true", help="hộp chờ rỗng thì thoát (chạy thử)")

    p = sub.add_parser("requeue", help="đưa task dead/no_source về hàng đợi")
    p.add_argument("--status", required=True, choices=["dead", "no_source"])
    p.add_argument("--type", choices=["xml", "pdf"])
    p.add_argument("--mst-tcgp")

    p = sub.add_parser("set-provider", help="gắn adapter PDF cho 1 đơn vị cung cấp HĐĐT")
    p.add_argument("--mst-tcgp", required=True)
    p.add_argument("--source-id", required=True)
    p.add_argument("--name")

    args = ap.parse_args(argv)
    _setup_logging()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        log.error("Thiếu DATABASE_URL (đặt trong biến môi trường hoặc file .env ở thư mục gốc)")
        return 2

    store = InvoiceStore(dsn)
    try:
        return {
            "init-db": cmd_init_db, "add-company": cmd_add_company, "tick": cmd_tick,
            "xml": cmd_xml, "pdf": cmd_pdf, "mail": cmd_mail, "sweep": cmd_sweep, "status": cmd_status, "requeue": cmd_requeue,
            "set-provider": cmd_set_provider, "sync-lists": cmd_sync_lists, "worker": cmd_worker,
        }[args.cmd](store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
