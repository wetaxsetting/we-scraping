"""
Worker WeInvoice: xử lý yêu cầu backend (we-scraping-api) đẩy vào `weinvoice.winv_engn_rqst`.

    python -X utf8 web/weinvoice_worker.py            # chạy mãi, cạnh worker xml/pdf của job
    python -X utf8 web/weinvoice_worker.py --once     # xử lý hết hàng đợi rồi thoát (chạy thử)

Hợp đồng (backend README §"Engine contract"):
  1. nhận yêu cầu QUEUED cũ nhất (FOR UPDATE SKIP LOCKED) → RUNNING + strt_dtm;
  2. ghi step_cd / prgs_data trong lúc chạy; cncl_yn = 'Y' → dừng ở mốc kiểm tra sau → CANCELLED;
  3. kết thúc DONE / PARTIAL / FAILED + rslt_data, err_msg, end_dtm, job_run_id.

| rqst_type | việc |
|-----------|------|
| SYNC      | danh sách GDT + XML theo tháng; ghi `winv_sync_cvrg` |
| FETCH_XML | tải XML ngay cho các HĐ được chọn |
| FETCH_PDF | gọi adapter NCC ngay cho 1 HĐ (mã/link tra cứu người dùng nhập được ưu tiên) |
| IMPORT    | nhập file người dùng tải lên (lô `winv_upld_*` hoặc 1 file XML của 1 HĐ) |
| TEST_DATA | nạp thư mục TestData (XML/, PDF/, ma_tra_cuu.csv) |

Ngày giờ của bảng WeInvoice là chuỗi `YYYYMMDDHH24MISS` giờ UTC (như `WeInvoiceTime` bên Java).
Worker không bao giờ trả về / ghi log mật khẩu; đăng nhập GDT đi qua `invoice_job.TokenProvider`.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import logging
import os
import re
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "web"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import invoice_job                                # noqa: E402  (nạp .env, sys.path)
import api_client                                 # noqa: E402
import job_policy                                 # noqa: E402
import mail_ingest                                # noqa: E402
import pdf_providers                              # noqa: E402
import weinvoice_bridge                           # noqa: E402
import xml_parser                                 # noqa: E402
from invoice_store import InvoiceStore            # noqa: E402
from psycopg2.extras import Json                  # noqa: E402

log = logging.getLogger("weinvoice_worker")

ENGINE_USER = "engine"
TYPES = ("SYNC", "FETCH_XML", "FETCH_PDF", "IMPORT", "TEST_DATA")
POLL_SECONDS = 5.0
STALE_MINUTES = 180             # RUNNING không cập nhật quá lâu = worker chết giữa chừng
XML_BATCH = 5                   # mỗi mẻ XML ngắn để kiểm tra hủy thường xuyên
MAX_MONTHS = 24
ZIP_MAX_MEMBERS = 2000
ZIP_MAX_MEMBER_BYTES = 50 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 500 * 1024 * 1024
UPLOAD_PLACEHOLDER = "#upload:"  # gdt_username của công ty chỉ có HĐ tải lên (chưa có tài khoản GDT)

_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------

def now_dtm() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _dtm_minutes_ago(minutes: int) -> str:
    return datetime.fromtimestamp(time.time() - minutes * 60, timezone.utc).strftime("%Y%m%d%H%M%S")


def storage_root() -> Path | None:
    """Thư mục file người dùng tải lên = WECMS_WEINVOICE_STORAGE_ROOT của backend."""
    raw = os.getenv("WEINVOICE_STORAGE_ROOT", "").strip()
    return Path(raw) if raw else None


def test_data_dir() -> Path:
    raw = os.getenv("WEINVOICE_TEST_DATA_DIR", "").strip()
    return Path(raw) if raw else ROOT / "docs" / "WeInvoice_UI_Spec_v1.0" / "TestData"


def stored_file(relative: str) -> bytes:
    """File backend đã lưu (`stor_path` tương đối) — không cho thoát khỏi thư mục gốc."""
    root = storage_root()
    if root is None:
        raise RequestFailed("Engine chưa cấu hình WEINVOICE_STORAGE_ROOT (thư mục file tải lên của backend)")
    base = root.resolve()
    path = (base / relative).resolve()
    if base != path and base not in path.parents:
        raise ValueError("đường dẫn file nằm ngoài thư mục lưu trữ")
    return path.read_bytes()


def parse_month(value, name: str) -> tuple[int, int]:
    m = _YM_RE.match(str(value or ""))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise RequestFailed(f"{name} phải có dạng YYYY-MM")
    return int(m.group(1)), int(m.group(2))


def months_between(from_month, to_month, today: date) -> list[tuple[date, date, str]]:
    """[(ngày đầu, ngày cuối, 'YYYY-MM')] — tháng tương lai bỏ, tháng hiện tại cắt tới hôm nay."""
    y, m = parse_month(from_month, "from_month")
    ty, tm = parse_month(to_month, "to_month")
    if (y, m) > (ty, tm):
        raise RequestFailed("from_month phải trước hoặc bằng to_month")
    out = []
    while (y, m) <= (ty, tm):
        start = date(y, m, 1)
        if start > today:
            break
        end = min(date(y, m, calendar.monthrange(y, m)[1]), today)
        out.append((start, end, f"{y:04d}-{m:02d}"))
        if len(out) > MAX_MONTHS:
            raise RequestFailed(f"Mỗi lần đồng bộ tối đa {MAX_MONTHS} tháng")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def sub_type_of(direction: str, khhdon: str) -> str:
    """Ký hiệu: ký tự đầu C = có mã, K = không mã; ký tự thứ 4 M = máy tính tiền."""
    k = (khhdon or "").upper()
    if len(k) >= 4 and k[3] == "M":
        return f"{direction}-mtt"
    if k.startswith("K"):
        return f"{direction}-khma"
    return direction


def _tthai_from_xml(parsed: dict) -> int:
    """TCHDon của HĐ liên quan (1 thay thế, 2 điều chỉnh, 3/4 nhiều HĐ) → tthai GDT (2 / 3); gốc = 1."""
    lq = (parsed.get("thong_tin_chung") or {}).get("hoa_don_lien_quan") or {}
    return {1: 2, 3: 2, 2: 3, 4: 3}.get(lq.get("tinh_chat"), 1)


def _date_or_none(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


class RequestFailed(Exception):
    """Lỗi nghiệp vụ: câu thông báo đi thẳng vào err_msg (người dùng đọc được)."""


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Bảng yêu cầu
# ---------------------------------------------------------------------------

class Requests:
    def __init__(self, store: InvoiceStore):
        self.store = store
        self.s = weinvoice_bridge.app_schema()

    def available(self) -> bool:
        with self.store._tx() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{self.s}.winv_engn_rqst",))
            return bool(cur.fetchone()["ok"])

    def claim(self, types=TYPES) -> dict | None:
        dtm = now_dtm()
        with self.store._tx() as cur:
            cur.execute(f"""
                UPDATE {self.s}.winv_engn_rqst r
                   SET rqst_stts = 'RUNNING', strt_dtm = %(dtm)s, mod_id = %(u)s, mod_dtm = %(dtm)s
                 WHERE r.rqst_seq = (
                       SELECT q.rqst_seq FROM {self.s}.winv_engn_rqst q
                        WHERE q.rqst_stts = 'QUEUED' AND q.cncl_yn = 'N' AND q.rqst_type = ANY(%(types)s)
                        ORDER BY q.rqst_seq
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED)
             RETURNING r.rqst_seq, r.owner_id, r.cmpn_mst, r.rqst_type, r.trgr_cd, r.rqst_prmt
            """, {"dtm": dtm, "u": ENGINE_USER, "types": list(types)})
            return cur.fetchone()

    def report(self, rqst_seq: int, step: str | None, progress: dict | None) -> bool:
        """Ghi bước / tiến độ. Trả True khi người dùng đã bấm hủy."""
        dtm = now_dtm()
        with self.store._tx() as cur:
            cur.execute(f"""
                UPDATE {self.s}.winv_engn_rqst
                   SET step_cd = COALESCE(%s, step_cd),
                       prgs_data = COALESCE(%s, prgs_data),
                       mod_id = %s, mod_dtm = %s
                 WHERE rqst_seq = %s
             RETURNING cncl_yn
            """, (step, Json(progress) if progress is not None else None, ENGINE_USER, dtm, rqst_seq))
            row = cur.fetchone()
        return bool(row) and row["cncl_yn"] == "Y"

    def finish(self, rqst_seq: int, status: str, result: dict | None = None, error: str | None = None,
               job_run_id: int | None = None) -> None:
        dtm = now_dtm()
        with self.store._tx() as cur:
            cur.execute(f"""
                UPDATE {self.s}.winv_engn_rqst
                   SET rqst_stts = %s, rslt_data = %s, err_msg = %s, job_run_id = %s,
                       end_dtm = %s, mod_id = %s, mod_dtm = %s
                 WHERE rqst_seq = %s
            """, (status, Json(result) if result is not None else None,
                  (error or None) and error[:1000], job_run_id, dtm, ENGINE_USER, dtm, rqst_seq))

    def fail_stale(self, minutes: int = STALE_MINUTES) -> int:
        """RUNNING không được cập nhật `minutes` phút (worker bị kill) → FAILED, để màn hình thôi chờ."""
        dtm = now_dtm()
        with self.store._tx() as cur:
            cur.execute(f"""
                UPDATE {self.s}.winv_engn_rqst
                   SET rqst_stts = CASE WHEN cncl_yn = 'Y' THEN 'CANCELLED' ELSE 'FAILED' END,
                       err_msg = 'Engine dừng giữa chừng, hãy gửi lại yêu cầu',
                       end_dtm = %s, mod_id = %s, mod_dtm = %s
                 WHERE rqst_stts = 'RUNNING' AND COALESCE(mod_dtm, strt_dtm) < %s
            """, (dtm, ENGINE_USER, dtm, _dtm_minutes_ago(minutes)))
            return cur.rowcount


@dataclass
class Job:
    reqs: Requests
    row: dict
    step: str | None = None
    progress: dict = field(default_factory=dict)
    run_id: int | None = None

    @property
    def id(self) -> int:
        return int(self.row["rqst_seq"])

    @property
    def mst(self) -> str:
        return self.row["cmpn_mst"]

    @property
    def owner(self) -> str:
        return self.row["owner_id"]

    @property
    def params(self) -> dict:
        p = self.row.get("rqst_prmt")
        return p if isinstance(p, dict) else {}

    def report(self, step: str | None = None, **progress) -> None:
        """Mốc kiểm tra: ghi tiến độ, người dùng đã hủy → Cancelled."""
        if step:
            self.step = step
        self.progress.update(progress)
        if self.reqs.report(self.id, step, dict(self.progress)):
            raise Cancelled()


# ---------------------------------------------------------------------------
# Truy vấn engine dùng riêng cho worker
# ---------------------------------------------------------------------------

def _claim_ids(store, task_type: str, company_mst: str, invoice_ids: list) -> list:
    """Nhận task (đang pending) của đúng các HĐ này, bỏ qua giờ hẹn thử lại (người dùng bấm tay)."""
    if not invoice_ids:
        return []
    with store._tx() as cur:
        cur.execute("""
            UPDATE tasks t SET status='running', locked_at=now(), updated_at=now()
             WHERE t.id IN (
                   SELECT t2.id FROM tasks t2 JOIN invoices i2 ON i2.id = t2.invoice_id
                    WHERE t2.task_type = %s AND t2.status = 'pending'
                      AND i2.company_mst = %s AND t2.invoice_id = ANY(%s)
                    ORDER BY t2.id
                    FOR UPDATE OF t2 SKIP LOCKED)
         RETURNING t.id AS task_id, t.invoice_id, t.attempts, t.next_run_at
        """, (task_type, company_mst, list(invoice_ids)))
        claimed = cur.fetchall()
        return InvoiceStore._claimed_rows(cur, claimed)


def _xml_pending_ids(store, company_mst: str, start: date, end: date) -> list:
    with store._tx() as cur:
        cur.execute("""
            SELECT t.invoice_id FROM tasks t JOIN invoices i ON i.id = t.invoice_id
             WHERE t.task_type = 'xml' AND t.status = 'pending'
               AND i.company_mst = %s AND i.ngay_lap BETWEEN %s AND %s
             ORDER BY t.id
        """, (company_mst, start, end))
        return [r["invoice_id"] for r in cur.fetchall()]


def _invoice(store, invoice_id: int) -> dict | None:
    with store._tx() as cur:
        cur.execute("""
            SELECT i.*, (x.invoice_id IS NOT NULL) AS has_xml, (p.invoice_id IS NOT NULL) AS has_pdf,
                   p.source_id AS pdf_source
              FROM invoices i
              LEFT JOIN invoice_xml x ON x.invoice_id = i.id
              LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
             WHERE i.id = %s
        """, (invoice_id,))
        return cur.fetchone()


def _task_state(store, invoice_id: int, task_type: str) -> dict | None:
    with store._tx() as cur:
        cur.execute("SELECT status, error_kind, last_error FROM tasks WHERE invoice_id=%s AND task_type=%s",
                    (invoice_id, task_type))
        return cur.fetchone()


def _write_coverage(store, schema: str, mst: str, sub_type: str, ym: str, start: date, end: date,
                    gdt_cnt: int | None) -> None:
    """Độ phủ danh sách GDT của 1 (MST, loại, tháng). gdt_cnt None = lấy danh sách lỗi."""
    with store._tx() as cur:
        cur.execute("""
            SELECT count(*) AS n FROM invoices
             WHERE company_mst = %s AND sub_type = %s AND ngay_lap BETWEEN %s AND %s
        """, (mst, sub_type, start, end))
        got = cur.fetchone()["n"]
        complete = gdt_cnt is not None and got >= gdt_cnt
        cur.execute(f"""
            INSERT INTO {schema}.winv_sync_cvrg (cmpn_mst, sub_type, prd_ym, gdt_cnt, got_cnt, cmpl_yn, last_sync_dtm)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cmpn_mst, sub_type, prd_ym) DO UPDATE SET
                gdt_cnt = COALESCE(EXCLUDED.gdt_cnt, {schema}.winv_sync_cvrg.gdt_cnt),
                got_cnt = EXCLUDED.got_cnt, cmpl_yn = EXCLUDED.cmpl_yn,
                last_sync_dtm = EXCLUDED.last_sync_dtm
        """, (mst, sub_type, ym.replace("-", ""), gdt_cnt, got, "Y" if complete else "N", now_dtm()))


def owner_msts(store, schema: str, owner: str) -> dict:
    """MST đang quản lý của 1 tài khoản WeInvoice → tên công ty (mọi trạng thái kết nối)."""
    with store._tx() as cur:
        cur.execute(f"""
            SELECT cmpn_mst, cmpn_nm FROM {schema}.winv_cmpn_infm WHERE owner_id = %s AND del_yn = 'N'
        """, (owner,))
        return {r["cmpn_mst"]: r["cmpn_nm"] for r in cur.fetchall()}


def ensure_company(store, mst: str, name: str | None) -> None:
    """HĐ phải thuộc 1 dòng `companies`. MST chỉ có HĐ tải lên (chưa có tài khoản GDT) → dòng tạm
    không hoạt động (job không đăng nhập); khi MST CONNECTED, `weinvoice_bridge` nâng thành thật."""
    with store._tx() as cur:
        cur.execute("""
            INSERT INTO companies (company_mst, gdt_username, name, active)
            VALUES (%s, %s, %s, false)
            ON CONFLICT DO NOTHING
        """, (mst, UPLOAD_PLACEHOLDER + mst, name))


# ---------------------------------------------------------------------------
# Nhập file (IMPORT, TEST_DATA)
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    code: str                       # NEW | MERGED | DUPLICATE | FOREIGN | ERROR | PDF_PENDING
    message: str | None = None
    invoice_id: int | None = None
    nbmst: str | None = None
    nmmst: str | None = None
    partner: str | None = None
    total: float | None = None


_PRIORITY = ("NEW", "MERGED", "DUPLICATE", "PDF_PENDING", "FOREIGN", "ERROR")


class Importer:
    """
    Quy tắc gộp (UI Spec 3.2): khóa = MST bán + mẫu + ký hiệu + số. HĐ chưa có → tạo mới (NEW);
    có mà chưa có XML → gắn XML (MERGED); đã có XML → DUPLICATE (XML của Thuế là bản chuẩn).
    MST bán/mua không thuộc tài khoản → FOREIGN. PDF: ghép theo XML cùng lô hoặc theo chữ trong
    PDF; chưa có HĐ → PDF_PENDING.
    """

    def __init__(self, store, out_dir: Path, owner: str, prefer_mst: str | None, run_id: int | None,
                 only_mst: str | None = None, label: str = "upload"):
        self.store, self.out_dir, self.run_id = store, Path(out_dir), run_id
        self.schema = weinvoice_bridge.app_schema()
        self.managed = owner_msts(store, self.schema, owner)
        self.prefer, self.only = prefer_mst, only_mst
        self.label = label
        self.batch_invoices: list[dict] = []        # HĐ vừa nhập XML trong lô (để ghép PDF)
        self.mi = mail_ingest.MailIngest(store, self.out_dir, run_id)
        self.mi.managed = {m.split("-")[0] for m in self.managed}

    # -- XML ---------------------------------------------------------------
    def _company_for(self, key: dict) -> tuple[str, str] | None:
        cands = []
        for mst, direction in ((key["nmmst"], "mua"), (key["nbmst"], "ban")):
            if mst and mst in self.managed and (self.only is None or mst == self.only):
                cands.append((mst, direction))
        if len(cands) > 1 and self.prefer:
            cands.sort(key=lambda c: c[0] != self.prefer)
        return cands[0] if cands else None

    def import_xml(self, data: bytes, filename: str) -> Outcome:
        try:
            parsed = xml_parser.parse_xml_bytes(data)
        except Exception as e:  # noqa: BLE001 — XML hỏng / không phải HĐ
            return Outcome("ERROR", f"Không đọc được XML hóa đơn: {e}"[:500])
        key = mail_ingest._xml_key(parsed)
        seller = parsed.get("nguoi_ban") or {}
        buyer = parsed.get("nguoi_mua") or {}
        total = (parsed.get("thanh_toan") or {}).get("tong_tien_tt")
        base = dict(nbmst=key["nbmst"] or None, nmmst=key["nmmst"] or None,
                    total=float(total) if total is not None else None)
        if not (key["nbmst"] and key["khmshdon"] and key["khhdon"] and key["shdon"] is not None):
            return Outcome("ERROR", "XML thiếu MST bán / mẫu số / ký hiệu / số hóa đơn", **base)
        target = self._company_for(key)
        if target is None:
            return Outcome("FOREIGN", "MST bán và MST mua đều không thuộc tài khoản",
                           partner=seller.get("ten") or buyer.get("ten"), **base)
        mst, direction = target
        partner = seller if direction == "mua" else buyer
        base["partner"] = partner.get("ten")
        ensure_company(self.store, mst, self.managed.get(mst))
        sha = hashlib.sha256(data).hexdigest()
        tc = parsed.get("thong_tin_chung") or {}
        tt = parsed.get("thanh_toan") or {}
        row = {
            "company_mst": mst, "direction": direction,
            "sub_type": sub_type_of(direction, key["khhdon"]),
            "nbmst": key["nbmst"], "nbten": seller.get("ten"),
            "nmmst": key["nmmst"] or None, "nmten": buyer.get("ten"),
            "khmshdon": key["khmshdon"], "khhdon": key["khhdon"], "shdon": key["shdon"],
            "ngay_lap": _date_or_none(tc.get("ngay_lap")),
            "tien_chua_thue": tt.get("tien_chua_thue"), "tien_thue": tt.get("tien_thue"),
            "tong_tien": tt.get("tong_tien_tt"), "tthai": _tthai_from_xml(parsed),
        }
        row["loai_hd"] = job_policy.SUB_TYPE_LABEL.get(row["sub_type"], row["sub_type"])
        path = invoice_job.invoice_path(self.out_dir, "xml", row, mst, "xml")
        rel = path.relative_to(self.out_dir).as_posix()
        with self.store._tx() as cur:
            cur.execute("""
                SELECT i.id, (x.invoice_id IS NOT NULL) AS has_xml FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                 WHERE i.company_mst=%s AND i.nbmst=%s AND i.khmshdon=%s AND i.khhdon=%s AND i.shdon=%s
                 FOR UPDATE OF i
            """, (mst, key["nbmst"], key["khmshdon"], key["khhdon"], key["shdon"]))
            found = cur.fetchone()
            if found is None:
                cur.execute("""
                        INSERT INTO invoices (company_mst, direction, sub_type, loai_hd, nbmst, nbten,
                            nmmst, nmten, khmshdon, khhdon, shdon, ngay_lap, tien_chua_thue, tien_thue,
                            tong_tien, tthai, hthdon, list_raw)
                        VALUES (%(company_mst)s, %(direction)s, %(sub_type)s, %(loai_hd)s, %(nbmst)s,
                            %(nbten)s, %(nmmst)s, %(nmten)s, %(khmshdon)s, %(khhdon)s, %(shdon)s,
                            %(ngay_lap)s, %(tien_chua_thue)s, %(tien_thue)s, %(tong_tien)s, %(tthai)s,
                            NULL, %(list_raw)s)
                        ON CONFLICT (company_mst, nbmst, khmshdon, khhdon, shdon) DO NOTHING
                        RETURNING id
                    """, {**row, "list_raw": Json({"nguon": self.label, "file": filename[:255]})})
                created = cur.fetchone()
                if created is None:         # tiến trình khác vừa tạo đúng HĐ này
                    cur.execute("""
                        SELECT i.id, (x.invoice_id IS NOT NULL) AS has_xml FROM invoices i
                          LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                         WHERE i.company_mst=%s AND i.nbmst=%s AND i.khmshdon=%s AND i.khhdon=%s AND i.shdon=%s
                    """, (mst, key["nbmst"], key["khmshdon"], key["khhdon"], key["shdon"]))
                    found = cur.fetchone()
                else:
                    invoice_id, code = created["id"], "NEW"
            if found is not None:
                invoice_id = found["id"]
                code = "DUPLICATE" if found["has_xml"] else "MERGED"
            if code != "DUPLICATE":
                invoice_job.atomic_write(path, data)
                InvoiceStore._save_xml(cur, invoice_id, rel, sha, parsed, partner, "xml_upload")
                InvoiceStore._finish_task(cur, invoice_id, "xml", self.run_id)
                cur.execute("""
                    INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'pdf')
                    ON CONFLICT (invoice_id, task_type) DO NOTHING
                """, (invoice_id,))
        inv = _invoice(self.store, invoice_id)
        if inv:
            self.batch_invoices.append(inv)
        message = {"NEW": "Tạo hóa đơn mới", "MERGED": "Bổ sung XML cho hóa đơn đã có",
                   "DUPLICATE": "Hóa đơn đã có XML"}[code]
        return Outcome(code, message, invoice_id, **base)

    # -- PDF ---------------------------------------------------------------
    def import_pdf(self, data: bytes, filename: str) -> Outcome:
        if not data.startswith(b"%PDF"):
            return Outcome("ERROR", "File không phải PDF")
        text = mail_ingest._pdf_text(data)
        inv = None
        hay = f"{filename} {text}"
        pair = [x for x in self.batch_invoices if mail_ingest._has_number(hay, x["shdon"])]
        if len(pair) == 1:
            inv = pair[0]
        if inv is None:
            inv = self.mi._match_pdf_text(text)
            if inv is not None and self.only and inv["company_mst"] != self.only:
                inv = None
        if inv is None:
            return Outcome("PDF_PENDING", "Chưa tìm thấy hóa đơn của PDF này (chờ XML)")
        inv = _invoice(self.store, inv["id"]) or inv
        base = dict(invoice_id=inv["id"], nbmst=inv["nbmst"], nmmst=inv.get("nmmst"),
                    total=float(inv["tong_tien"]) if inv.get("tong_tien") is not None else None)
        if inv.get("has_pdf"):
            return Outcome("DUPLICATE", "Hóa đơn đã có PDF", **base)
        path = invoice_job.invoice_path(self.out_dir, "pdf", inv, inv["company_mst"], "pdf")
        invoice_job.atomic_write(path, data)
        saved = self.store.apply_email_pdf(inv["id"], self.run_id, path.relative_to(self.out_dir).as_posix(),
                                           hashlib.sha256(data).hexdigest(), source_id=self.label)
        if not saved:
            return Outcome("DUPLICATE", "Hóa đơn đã có PDF", **base)
        return Outcome("MERGED", "Gắn PDF bản gốc", **base)

    # -- ZIP ---------------------------------------------------------------
    def import_zip(self, data: bytes, filename: str) -> Outcome:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return Outcome("ERROR", "File ZIP hỏng")
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > ZIP_MAX_MEMBERS:
            return Outcome("ERROR", f"ZIP có quá {ZIP_MAX_MEMBERS} file")
        if sum(i.file_size for i in members) > ZIP_MAX_TOTAL_BYTES or \
                any(i.file_size > ZIP_MAX_MEMBER_BYTES for i in members):
            return Outcome("ERROR", "ZIP quá lớn sau khi giải nén")
        xmls = [i for i in members if i.filename.lower().endswith(".xml")]
        pdfs = [i for i in members if i.filename.lower().endswith(".pdf")]
        results = []
        for info in xmls + pdfs:
            try:
                inner = zf.read(info)
            except Exception as e:  # noqa: BLE001 — có mật khẩu / hỏng: bỏ đúng file đó
                results.append(Outcome("ERROR", f"{info.filename}: {e}"[:300]))
                continue
            name = info.filename.rsplit("/", 1)[-1]
            results.append(self.import_xml(inner, name) if info in xmls else self.import_pdf(inner, name))
        if not results:
            return Outcome("ERROR", "ZIP không có file XML / PDF")
        if len(results) == 1:
            return results[0]
        counts = Counter(r.code for r in results)
        code = next(c for c in _PRIORITY if counts[c])
        summary = ", ".join(f"{_LABEL[c]} {counts[c]}" for c in _PRIORITY if counts[c])
        return Outcome(code, f"ZIP {len(results)} file: {summary}")

    def import_file(self, data: bytes, filename: str) -> Outcome:
        ext = Path(filename).suffix.lower()
        if ext == ".xml":
            return self.import_xml(data, filename)
        if ext == ".pdf":
            return self.import_pdf(data, filename)
        if ext == ".zip":
            return self.import_zip(data, filename)
        return Outcome("ERROR", f"Không nhận loại file {ext or '(không đuôi)'}")


_LABEL = {"NEW": "mới", "MERGED": "bổ sung", "DUPLICATE": "trùng", "PDF_PENDING": "PDF chờ XML",
          "FOREIGN": "MST lạ", "ERROR": "lỗi"}


def _order_files(rows: list, name_key: str) -> list:
    """XML trước, rồi ZIP, PDF cuối — PDF ghép được với XML cùng lô."""
    rank = {".xml": 0, ".zip": 1, ".pdf": 2}
    return sorted(rows, key=lambda r: (rank.get(Path(r[name_key]).suffix.lower(), 3), r.get("file_no", 0)))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, store: InvoiceStore, out_dir: Path | None = None):
        self.store = store
        self.reqs = Requests(store)
        self.out_dir = out_dir or invoice_job.output_dir()
        self.handlers = {"SYNC": self.do_sync, "FETCH_XML": self.do_fetch_xml,
                         "FETCH_PDF": self.do_fetch_pdf, "IMPORT": self.do_import,
                         "TEST_DATA": self.do_test_data}

    # -- vòng chạy ---------------------------------------------------------
    def run_one(self) -> bool:
        row = self.reqs.claim(tuple(self.handlers))
        if row is None:
            return False
        job = Job(self.reqs, row)
        log.info("WeInvoice #%s %s MST %s bắt đầu", job.id, row["rqst_type"], job.mst)
        try:
            status, result = self.handlers[row["rqst_type"]](job)
            self.reqs.finish(job.id, status, result, None, job.run_id)
            log.info("WeInvoice #%s %s → %s %s", job.id, row["rqst_type"], status, result)
        except Cancelled:
            self.reqs.finish(job.id, "CANCELLED", dict(job.progress) or None, "Người dùng đã hủy", job.run_id)
            log.info("WeInvoice #%s hủy theo yêu cầu", job.id)
        except invoice_job.LoginError as e:
            self.reqs.finish(job.id, "FAILED", dict(job.progress) or None, f"Không đăng nhập được Cục Thuế: {e}",
                             job.run_id)
            log.warning("WeInvoice #%s đăng nhập GDT lỗi: %s", job.id, e)
        except RequestFailed as e:
            self.reqs.finish(job.id, "FAILED", dict(job.progress) or None, str(e), job.run_id)
            log.warning("WeInvoice #%s thất bại: %s", job.id, e)
        except Exception as e:  # noqa: BLE001 — 1 yêu cầu lỗi không được làm chết worker
            log.exception("WeInvoice #%s lỗi", job.id)
            self.reqs.finish(job.id, "FAILED", dict(job.progress) or None, f"Lỗi engine: {type(e).__name__}: {e}",
                             job.run_id)
        return True

    def run(self, once: bool = False, poll: float = POLL_SECONDS) -> int:
        if not self.reqs.available():
            log.error("Không thấy bảng %s.winv_engn_rqst — WeInvoice chưa dựng trên DB này",
                      self.reqs.s)
            return 1
        n = self.reqs.fail_stale()
        if n:
            log.warning("WeInvoice: %d yêu cầu RUNNING bị bỏ dở → FAILED", n)
        while True:
            if self.run_one():
                continue
            if once:
                return 0
            time.sleep(poll)

    # -- dùng chung --------------------------------------------------------
    def _company(self, mst: str) -> dict:
        for c in invoice_job._active_companies(self.store):
            if c["company_mst"] == mst:
                return c
        raise RequestFailed(f"MST {mst} chưa kết nối Cục Thuế (thiếu tài khoản / mật khẩu)")

    def _start_run(self, job: Job, command: str) -> int:
        job.run_id = self.store.start_run(command)
        self.store.begin_run_company(job.run_id, job.mst)
        return job.run_id

    def _end_run(self, job: Job, status: str, stats: Counter, extra: dict | None = None) -> None:
        if job.run_id is None:
            return
        self.store.finish_run(job.run_id, status, {"company": job.mst, "rqst_seq": job.id,
                                                    "stats": dict(stats), **(extra or {})})
        self.store.finish_run_company(job.run_id, job.mst, status, dict(stats))

    def _ids(self, job: Job) -> list[int]:
        out = []
        for v in self.params_list(job, "invoice_ids"):
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def params_list(job: Job, name: str) -> list:
        v = job.params.get(name)
        return v if isinstance(v, list) else []

    # -- SYNC --------------------------------------------------------------
    def do_sync(self, job: Job):
        p = job.params
        direction = (p.get("direction") or "both").lower()
        types = {"purchase": ["mua-all"], "sales": ["ban-all"], "both": ["mua-all", "ban-all"]}.get(direction)
        if types is None:
            raise RequestFailed("direction phải là both / purchase / sales")
        months = months_between(p.get("from_month"), p.get("to_month"), invoice_job.today_vn())
        sub_types = api_client.expand_invoice_types(types)
        company = self._company(job.mst)
        stats = Counter()
        self._start_run(job, "weinvoice-sync")
        status = "error"
        breaker = job_policy.CircuitBreaker()
        done_months = []
        try:
            tokens = invoice_job.TokenProvider(self.store, company, stats)
            job.report("login", month=None, listed=0, xml_done=0, xml_total=0)
            tokens.get()                    # JWT còn hạn thì dùng lại; hết hạn → giải captcha + đăng nhập
            for start, end, ym in months:
                job.report("list", month=ym, xml_done=0, xml_total=0)
                for st in sub_types:
                    listed, errors = stats["listed"], stats["sync_errors"]
                    invoice_job.sync_lists(self.store, company, tokens, [st],
                                           lambda _st, s=start, e=end: [(s, e)],
                                           breaker, stats, move_cursor=False)
                    ok = stats["sync_errors"] == errors and not breaker.open
                    _write_coverage(self.store, self.reqs.s, job.mst, st, ym, start, end,
                                    stats["listed"] - listed if ok else None)
                    job.report(listed=stats["listed"])
                ids = _xml_pending_ids(self.store, job.mst, start, end)
                job.report("xml", xml_total=len(ids), xml_done=0)
                handled = 0
                while ids and not breaker.open:
                    batch = _claim_ids(self.store, "xml", job.mst, ids[:XML_BATCH])
                    ids = ids[XML_BATCH:]
                    if not batch:
                        continue
                    handled += invoice_job.xml_batch(self.store, company, tokens, breaker, job.run_id, batch,
                                                     stats, self.out_dir)
                    job.report(xml_done=handled)
                    done_months.append(ym)
                if breaker.open:
                    break
            job.report("done")
            status = "partial" if (stats["sync_errors"] or stats["xml_failed"] or breaker.open) else "ok"
        finally:
            self._end_run(job, status, stats, {"months": done_months})
        result = {"new": stats["new_invoices"], "updated": 0, "xml": stats["xml_ok"],
                  "listed": stats["listed"], "xml_failed": stats["xml_failed"],
                  "sync_errors": stats["sync_errors"], "months": done_months}
        return ("PARTIAL" if status == "partial" else "DONE"), result

    # -- FETCH_XML ---------------------------------------------------------
    def do_fetch_xml(self, job: Job):
        ids = self._ids(job)
        if not ids:
            raise RequestFailed("Không có hóa đơn nào để lấy XML")
        with self.store._tx() as cur:
            cur.execute("""
                SELECT i.id, (x.invoice_id IS NOT NULL) AS has_xml, t.status, t.error_kind
                  FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                  LEFT JOIN tasks t ON t.invoice_id = i.id AND t.task_type = 'xml'
                 WHERE i.company_mst = %s AND i.id = ANY(%s)
            """, (job.mst, ids))
            rows = cur.fetchall()
            wanted = [r["id"] for r in rows if not r["has_xml"] and r["error_kind"] != "summary_only"
                      and r["status"] != "running"]
            # HĐ chưa có task / task đang chờ giờ hẹn / đã bỏ cuộc → chạy ngay
            for iid in wanted:
                cur.execute("""
                    INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'xml')
                    ON CONFLICT (invoice_id, task_type) DO UPDATE SET
                        status='pending', next_run_at=now(), locked_at=NULL, updated_at=now()
                     WHERE tasks.status IN ('pending', 'dead', 'no_source')
                """, (iid,))
        skipped = {"already": sum(1 for r in rows if r["has_xml"]),
                   "summary_only": sum(1 for r in rows if not r["has_xml"] and r["error_kind"] == "summary_only"),
                   "not_found": len(set(ids) - {r["id"] for r in rows})}
        stats = Counter()
        job.report("xml", xml_total=len(wanted), xml_done=0)
        if not wanted:
            return "DONE", {"requested": len(ids), "xml": 0, "failed": 0, **skipped}
        company = self._company(job.mst)
        self._start_run(job, "weinvoice-fetch-xml")
        status = "error"
        try:
            tokens = invoice_job.TokenProvider(self.store, company, stats)
            breaker = job_policy.CircuitBreaker()
            job.report("login")
            tokens.get()
            job.report("xml")
            handled, rest = 0, list(wanted)
            while rest and not breaker.open:
                batch = _claim_ids(self.store, "xml", job.mst, rest[:XML_BATCH])
                rest = rest[XML_BATCH:]
                if batch:
                    handled += invoice_job.xml_batch(self.store, company, tokens, breaker, job.run_id, batch,
                                                     stats, self.out_dir)
                job.report(xml_done=handled)
            job.report("done")
            status = "ok" if stats["xml_ok"] == len(wanted) else "partial"
        finally:
            self._end_run(job, status, stats)
        result = {"requested": len(ids), "xml": stats["xml_ok"],
                  "failed": len(wanted) - stats["xml_ok"], **skipped}
        if stats["xml_ok"] == len(wanted):
            return "DONE", result
        return ("PARTIAL" if stats["xml_ok"] else "FAILED"), result

    # -- FETCH_PDF ---------------------------------------------------------
    def _saved_code(self, job: Job, invoice_id: int) -> dict:
        with self.store._tx() as cur:
            cur.execute(f"""
                SELECT lkup_cd, cmpn_cd, lkup_url FROM {self.reqs.s}.winv_lkup_cd
                 WHERE owner_id = %s AND invc_id = %s
            """, (job.owner, invoice_id))
            return cur.fetchone() or {}

    @staticmethod
    def apply_manual_code(task: dict, code: str | None, company_code: str | None, url: str | None) -> None:
        """Mã người dùng nhập thắng mã đoán từ XML: thay mọi trường TTKhac kiểu "mã tra cứu"."""
        from pdf_providers.lookup_codes import _HINTS, _norm
        tt = dict(task.get("tt_khac") or {}) if isinstance(task.get("tt_khac"), dict) else {}
        if code:
            for k in list(tt):
                if any(h in _norm(k) for h in _HINTS):
                    tt[k] = code
            tt.setdefault("Mã tra cứu", code)
            task["ma_tra_cuu"] = code
        if company_code:
            tt["Mã công ty"] = company_code
        task["tt_khac"] = tt
        if url:
            task["lookup_url"] = url
            adapter = pdf_providers.adapter_for_host(urlsplit(url).hostname)
            if adapter is not None:
                inv = None
                try:
                    inv = adapter.invoice_from_link(url, code, f"MST người bán: {task.get('nbmst') or ''}")
                except Exception as e:  # noqa: BLE001 — link lạ: vẫn thử bằng mã
                    log.info("invoice_from_link %s: %s", adapter.source_id, e)
                if inv:
                    for k, v in inv.items():
                        if v not in (None, "", {}, []):
                            task[k] = v
                    task["source_id"] = adapter.source_id

    def do_fetch_pdf(self, job: Job):
        ids = self._ids(job)
        if not ids:
            raise RequestFailed("Không có hóa đơn nào để lấy PDF")
        iid = ids[0]
        inv = _invoice(self.store, iid)
        if inv is None or inv["company_mst"] != job.mst:
            raise RequestFailed("Không tìm thấy hóa đơn")
        if inv["has_pdf"]:
            return "DONE", {"pdf": "exists", "source_id": inv["pdf_source"]}
        job.report("open")
        with self.store._tx() as cur:
            cur.execute("""
                INSERT INTO tasks (invoice_id, task_type, status, locked_at) VALUES (%s, 'pdf', 'running', now())
                ON CONFLICT (invoice_id, task_type) DO UPDATE SET
                    status='running', locked_at=now(), updated_at=now()
                 WHERE tasks.status <> 'running'
             RETURNING id AS task_id, invoice_id, attempts, next_run_at
            """, (iid,))
            claimed = cur.fetchall()
            tasks = InvoiceStore._claimed_rows(cur, claimed)
        if not tasks:
            raise RequestFailed("Hóa đơn đang được engine lấy PDF, thử lại sau ít phút")
        task = tasks[0]
        saved = self._saved_code(job, iid)
        p = job.params
        self.apply_manual_code(task, p.get("lookup_code") or saved.get("lkup_cd"),
                               p.get("company_code") or saved.get("cmpn_cd"),
                               p.get("lookup_url") or saved.get("lkup_url"))
        stats = Counter()
        self._start_run(job, "weinvoice-fetch-pdf")
        status = "error"
        deferred: list = []
        try:
            job.report("submit")
            invoice_job.pdf_batch(self.store, job.mst, job.run_id, [task], stats, self.out_dir, {}, deferred)
            job.report("download")
            status = "ok" if stats["pdf_ok"] else "partial"
        finally:
            self.store.release_tasks(deferred)
            self._end_run(job, status, stats)
        if stats["pdf_ok"]:
            job.report("done")
            after = _invoice(self.store, iid) or {}
            return "DONE", {"pdf": "saved", "source_id": after.get("pdf_source")}
        state = _task_state(self.store, iid, "pdf") or {}
        if stats["pdf_no_source"]:
            raise RequestFailed("Chưa có cách lấy PDF tự động cho nhà cung cấp của hóa đơn này")
        raise RequestFailed(state.get("last_error") or "Không lấy được PDF từ cổng tra cứu")

    # -- IMPORT ------------------------------------------------------------
    def do_import(self, job: Job):
        p = job.params
        if p.get("upload_id") is not None:
            return self._import_upload(job, int(p["upload_id"]), self.params_list(job, "file_nos"),
                                       p.get("company_mst"))
        if p.get("invoice_id") is not None and p.get("file_id") is not None:
            return self._import_invoice_file(job, int(p["invoice_id"]), int(p["file_id"]))
        raise RequestFailed("IMPORT thiếu upload_id hoặc invoice_id + file_id")

    def _import_upload(self, job: Job, upload_id: int, file_nos: list, adopt_mst: str | None):
        s = self.reqs.s
        with self.store._tx() as cur:
            cur.execute(f"SELECT * FROM {s}.winv_upld_infm WHERE upld_seq=%s AND owner_id=%s AND del_yn='N'",
                        (upload_id, job.owner))
            if cur.fetchone() is None:
                raise RequestFailed("Không tìm thấy lô tải lên")
            nos = [int(n) for n in file_nos if str(n).isdigit()]
            if nos:
                cur.execute(f"SELECT * FROM {s}.winv_upld_file WHERE upld_seq=%s AND file_no = ANY(%s)",
                            (upload_id, nos))
            else:
                cur.execute(f"SELECT * FROM {s}.winv_upld_file WHERE upld_seq=%s AND rslt_cd IS NULL",
                            (upload_id,))
            files = _order_files(cur.fetchall(), "file_nm")
        stats = Counter()
        self._start_run(job, "weinvoice-import")
        importer = Importer(self.store, self.out_dir, job.owner, job.mst, job.run_id, only_mst=adopt_mst)
        status = "error"
        try:
            job.report("import", done=0, total=len(files))
            for i, f in enumerate(files, 1):
                try:
                    data = stored_file(f["stor_path"])
                    out = importer.import_file(data, f["file_nm"])
                except RequestFailed:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("IMPORT lô %s file %s lỗi: %s", upload_id, f["file_no"], e)
                    out = Outcome("ERROR", f"{type(e).__name__}: {e}"[:500])
                stats[out.code.lower()] += 1
                self._write_upload_result(upload_id, f["file_no"], out)
                job.report(done=i)
            job.report("done")
            status = "partial" if stats["error"] else "ok"
        finally:
            self._end_run(job, status, stats, {"upload_id": upload_id})
        result = {k: stats[k] for k in ("new", "merged", "duplicate", "foreign", "error", "pdf_pending")}
        result["files"] = len(files)
        if stats["error"] and stats["error"] == len(files):
            return "FAILED", result
        return ("PARTIAL" if stats["error"] else "DONE"), result

    def _write_upload_result(self, upload_id: int, file_no: int, out: Outcome) -> None:
        with self.store._tx() as cur:
            cur.execute(f"""
                UPDATE {self.reqs.s}.winv_upld_file
                   SET rslt_cd=%s, rslt_msg=%s, invc_id=%s, nbmst=%s, nmmst=%s, ptnr_nm=%s, totl_amt=%s,
                       prcs_dtm=%s
                 WHERE upld_seq=%s AND file_no=%s
            """, (out.code, (out.message or None) and out.message[:1000], out.invoice_id,
                  (out.nbmst or None) and out.nbmst[:14], (out.nmmst or None) and out.nmmst[:14],
                  (out.partner or None) and out.partner[:500], out.total, now_dtm(), upload_id, file_no))

    def _import_invoice_file(self, job: Job, invoice_id: int, file_seq: int):
        with self.store._tx() as cur:
            cur.execute(f"""
                SELECT * FROM {self.reqs.s}.winv_invc_file
                 WHERE file_seq=%s AND owner_id=%s AND invc_id=%s AND del_yn='N'
            """, (file_seq, job.owner, invoice_id))
            f = cur.fetchone()
        if f is None or f["file_kind"] != "XML":
            raise RequestFailed("Không tìm thấy file XML đã tải lên")
        inv = _invoice(self.store, invoice_id)
        if inv is None or inv["company_mst"] != job.mst:
            raise RequestFailed("Không tìm thấy hóa đơn")
        if inv["has_xml"]:
            return "DONE", {"xml": "duplicate", "invoice_id": invoice_id}
        job.report("import")
        data = stored_file(f["stor_path"])
        try:
            parsed = xml_parser.parse_xml_bytes(data)
        except Exception as e:  # noqa: BLE001
            raise RequestFailed(f"Không đọc được XML hóa đơn: {e}"[:500]) from e
        key = mail_ingest._xml_key(parsed)
        if (key["nbmst"], key["khmshdon"], key["khhdon"], key["shdon"]) != \
                (inv["nbmst"], int(inv["khmshdon"]), inv["khhdon"], int(inv["shdon"])):
            raise RequestFailed("XML không phải của hóa đơn này (MST bán / mẫu / ký hiệu / số khác)")
        path = invoice_job.invoice_path(self.out_dir, "xml", inv, inv["company_mst"], "xml")
        invoice_job.atomic_write(path, data)
        partner = parsed.get("nguoi_ban") if inv["direction"] == "mua" else parsed.get("nguoi_mua")
        with self.store._tx() as cur:
            cur.execute("SELECT 1 FROM invoice_xml WHERE invoice_id=%s FOR UPDATE", (invoice_id,))
            if cur.fetchone():
                return "DONE", {"xml": "duplicate", "invoice_id": invoice_id}
            InvoiceStore._save_xml(cur, invoice_id, path.relative_to(self.out_dir).as_posix(),
                                   hashlib.sha256(data).hexdigest(), parsed, partner, "xml_upload")
            InvoiceStore._finish_task(cur, invoice_id, "xml", None)
            cur.execute("""
                INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'pdf')
                ON CONFLICT (invoice_id, task_type) DO NOTHING
            """, (invoice_id,))
        job.report("done")
        return "DONE", {"xml": "saved", "invoice_id": invoice_id}

    # -- TEST_DATA ---------------------------------------------------------
    def do_test_data(self, job: Job):
        folder = test_data_dir()
        if not folder.is_dir():
            raise RequestFailed("no_folder")
        xml_files = sorted(p for p in (folder / "XML").rglob("*") if p.suffix.lower() in (".xml", ".zip")) \
            if (folder / "XML").is_dir() else []
        pdf_files = sorted((folder / "PDF").rglob("*.pdf")) if (folder / "PDF").is_dir() else []
        stats = Counter()
        self._start_run(job, "weinvoice-test-data")
        importer = Importer(self.store, self.out_dir, job.owner, job.mst, job.run_id, label="test_data")
        report = {"folder": folder.name, "xml_total": len(xml_files), "created": 0, "merged": 0,
                  "duplicate": 0, "errors": [], "new_tax_ids": [], "codes_applied": 0,
                  "codes_unmatched": [], "pdf_attached": 0, "pdf_unmatched": []}
        status = "error"
        try:
            total = len(xml_files) + len(pdf_files)
            job.report("import", done=0, total=total)
            done = 0
            foreign = set()
            for path in xml_files:
                out = self._safe_import(importer, path)
                stats[out.code.lower()] += 1
                if out.code == "NEW":
                    report["created"] += 1
                elif out.code == "MERGED":
                    report["merged"] += 1
                elif out.code == "DUPLICATE":
                    report["duplicate"] += 1
                elif out.code == "FOREIGN":
                    foreign.update(m for m in (out.nbmst, out.nmmst) if m)
                elif out.code == "ERROR":
                    report["errors"].append({"file": path.name, "error": out.message})
                done += 1
                job.report(done=done)
            report["new_tax_ids"] = sorted(foreign - set(importer.managed))
            codes = folder / "ma_tra_cuu.csv"
            if codes.is_file():
                applied, unmatched = self._apply_codes(job, importer, codes)
                report["codes_applied"], report["codes_unmatched"] = applied, unmatched
            for path in pdf_files:
                out = self._safe_import(importer, path)
                if out.code == "MERGED":
                    report["pdf_attached"] += 1
                elif out.code != "DUPLICATE":
                    report["pdf_unmatched"].append(path.name)
                done += 1
                job.report(done=done)
            job.report("done")
            status = "partial" if report["errors"] else "ok"
        finally:
            self._end_run(job, status, stats)
        return ("PARTIAL" if report["errors"] else "DONE"), report

    @staticmethod
    def _safe_import(importer: Importer, path: Path) -> Outcome:
        try:
            return importer.import_file(path.read_bytes(), path.name)
        except Exception as e:  # noqa: BLE001
            log.warning("TestData %s lỗi: %s", path.name, e)
            return Outcome("ERROR", f"{type(e).__name__}: {e}"[:500])

    def _apply_codes(self, job: Job, importer: Importer, path: Path) -> tuple[int, list]:
        """ma_tra_cuu.csv: `số, ký hiệu, MST bán, mã` (dấu , hoặc ;) → mã tra cứu nhập tay của HĐ."""
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        applied, unmatched = 0, []
        msts = list(importer.managed)
        for line in csv.reader(io.StringIO(text.replace(";", ","))):
            cells = [c.strip().strip('"') for c in line]
            if len(cells) < 4 or not cells[0].isdigit():
                continue
            number, symbol, seller, code = cells[:4]
            with self.store._tx() as cur:
                cur.execute("""
                    SELECT id, company_mst FROM invoices
                     WHERE company_mst = ANY(%s) AND nbmst=%s AND khhdon=%s AND shdon=%s
                """, (msts, seller, symbol, int(number)))
                rows = cur.fetchall()
                if len(rows) != 1 or not code:
                    unmatched.append(f"{symbol}-{number}")
                    continue
                dtm = now_dtm()
                cur.execute(f"""
                    INSERT INTO {self.reqs.s}.winv_lkup_cd (owner_id, invc_id, cmpn_mst, lkup_cd, reg_id, reg_dtm)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (owner_id, invc_id) DO UPDATE SET
                        lkup_cd = EXCLUDED.lkup_cd, mod_id = EXCLUDED.reg_id, mod_dtm = EXCLUDED.reg_dtm
                """, (job.owner, rows[0]["id"], rows[0]["company_mst"], code[:200], ENGINE_USER, dtm))
                applied += 1
        return applied, unmatched


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Worker yêu cầu WeInvoice (weinvoice.winv_engn_rqst)")
    ap.add_argument("--once", action="store_true", help="xử lý hết hàng đợi rồi thoát")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS, help="hàng đợi rỗng thì ngủ bao nhiêu giây")
    args = ap.parse_args(argv)
    invoice_job._setup_logging()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        log.error("Thiếu DATABASE_URL")
        return 1
    return Worker(InvoiceStore(dsn)).run(once=args.once, poll=args.poll)


if __name__ == "__main__":
    sys.exit(main())
