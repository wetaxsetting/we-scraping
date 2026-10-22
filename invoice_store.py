# invoice_store.py
"""
Lớp truy cập PostgreSQL cho job lấy hóa đơn (schema: db/schema.sql).

Hộp chờ = bảng tasks. Lấy việc bằng FOR UPDATE SKIP LOCKED, nên 2 tiến trình
không bao giờ nhận cùng 1 task. Mỗi bước "xong" (ghi XML/PDF + đánh dấu task done
+ tạo task kế tiếp) nằm trong 1 transaction: trạng thái luôn khớp dữ liệu.

Dùng:
    store = InvoiceStore(os.environ["DATABASE_URL"])
    if store.try_lock(): ...
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, RealDictCursor

import job_policy

# Khóa advisory toàn cục: chỉ 1 lượt chạy job tại 1 thời điểm.
_JOB_LOCK_KEY = 7311002

_SCHEMA_SQL = Path(__file__).resolve().parent / "db" / "schema.sql"


class InvoiceStore:
    lock_key = _JOB_LOCK_KEY

    def __init__(self, dsn: str, schema: str | None = None):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False
        if schema:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
                raise ValueError(f"Tên schema không hợp lệ: {schema!r}")
            with self._tx() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                cur.execute(f"SET search_path TO {schema}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self):
        """1 transaction: commit khi xong, rollback khi lỗi."""
        with self.conn:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur

    # ------------------------------------------------------------------
    # Schema + khóa chạy
    # ------------------------------------------------------------------
    def init_schema(self, sql_path: Path = _SCHEMA_SQL) -> None:
        with self._tx() as cur:
            cur.execute(Path(sql_path).read_text(encoding="utf-8"))

    def try_lock(self) -> bool:
        """False nếu đang có lượt chạy khác (khóa giữ đến khi unlock/đóng kết nối)."""
        with self._tx() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (self.lock_key,))
            return cur.fetchone()["ok"]

    def unlock(self) -> None:
        with self._tx() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))

    # ------------------------------------------------------------------
    # Công ty
    # ------------------------------------------------------------------
    def upsert_company(self, mst: str, username: str, name: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO companies (company_mst, gdt_username, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (company_mst) DO UPDATE
                   SET gdt_username = EXCLUDED.gdt_username,
                       name = COALESCE(EXCLUDED.name, companies.name),
                       active = true
            """, (mst, username, name))

    def active_companies(self) -> list:
        with self._tx() as cur:
            cur.execute("SELECT * FROM companies WHERE active ORDER BY company_mst")
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Cache JWT
    # ------------------------------------------------------------------
    def get_token(self, mst: str, min_ttl_seconds: int = 600) -> str | None:
        """Token còn hạn ít nhất min_ttl_seconds và chưa bị đánh dấu hỏng."""
        with self._tx() as cur:
            cur.execute("""
                SELECT token FROM gdt_sessions
                 WHERE company_mst = %s AND invalidated_at IS NULL
                   AND expires_at > now() + %s * interval '1 second'
            """, (mst, min_ttl_seconds))
            row = cur.fetchone()
            return row["token"] if row else None

    def save_token(self, mst: str, token: str, expires_at: datetime) -> None:
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO gdt_sessions (company_mst, token, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (company_mst) DO UPDATE
                   SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at,
                       obtained_at = now(), invalidated_at = NULL
            """, (mst, token, expires_at))

    def invalidate_token(self, mst: str) -> None:
        with self._tx() as cur:
            cur.execute("UPDATE gdt_sessions SET invalidated_at = now() WHERE company_mst = %s",
                        (mst,))

    # ------------------------------------------------------------------
    # Mốc đồng bộ
    # ------------------------------------------------------------------
    def get_cursor(self, mst: str, sub_type: str) -> date | None:
        with self._tx() as cur:
            cur.execute("SELECT synced_to FROM sync_cursors WHERE company_mst=%s AND sub_type=%s",
                        (mst, sub_type))
            row = cur.fetchone()
            return row["synced_to"] if row else None

    def set_cursor(self, mst: str, sub_type: str, synced_to: date) -> None:
        """Chỉ tiến lên, không bao giờ lùi."""
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO sync_cursors (company_mst, sub_type, synced_to)
                VALUES (%s, %s, %s)
                ON CONFLICT (company_mst, sub_type) DO UPDATE
                   SET synced_to = GREATEST(sync_cursors.synced_to, EXCLUDED.synced_to),
                       updated_at = now()
            """, (mst, sub_type, synced_to))

    # ------------------------------------------------------------------
    # Hóa đơn
    # ------------------------------------------------------------------
    def upsert_invoices(self, rows: list) -> int:
        """
        Thêm HĐ mới (đã normalize). HĐ đã có → bỏ qua (không lấy lại).
        Mỗi HĐ mới được tạo 1 task XML. Trả về số HĐ mới.
        """
        new = 0
        with self._tx() as cur:
            for v in rows:
                cur.execute("""
                    INSERT INTO invoices (company_mst, direction, sub_type, loai_hd,
                        nbmst, nbten, nmmst, nmten, khmshdon, khhdon, shdon, ngay_lap,
                        tien_chua_thue, tien_thue, tong_tien, tthai, hthdon, list_raw)
                    VALUES (%(company_mst)s, %(direction)s, %(sub_type)s, %(loai_hd)s,
                        %(nbmst)s, %(nbten)s, %(nmmst)s, %(nmten)s, %(khmshdon)s, %(khhdon)s,
                        %(shdon)s, %(ngay_lap)s, %(tien_chua_thue)s, %(tien_thue)s,
                        %(tong_tien)s, %(tthai)s, %(hthdon)s, %(list_raw)s)
                    ON CONFLICT (company_mst, nbmst, khmshdon, khhdon, shdon) DO NOTHING
                    RETURNING id
                """, {**v, "hthdon": v.get("hthdon"), "list_raw": Json(v["list_raw"])})
                row = cur.fetchone()
                if row:
                    if job_policy.summary_only(v):
                        # Thuế chỉ có dữ liệu tổng hợp → không tải XML (luôn 500). Nằm chờ nguồn
                        # khác (email / file người dùng); phần đầu HĐ đã có trong invoices.
                        cur.execute("""
                            INSERT INTO tasks (invoice_id, task_type, status, error_kind, last_error)
                            VALUES (%s, 'xml', 'no_source', 'summary_only',
                                    'HĐ gửi Thuế theo bảng tổng hợp (hthdon=2): Thuế không có XML')
                            ON CONFLICT (invoice_id, task_type) DO NOTHING
                        """, (row["id"],))
                    else:
                        cur.execute("""
                            INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'xml')
                            ON CONFLICT (invoice_id, task_type) DO NOTHING
                        """, (row["id"],))
                    new += 1
        return new

    # ------------------------------------------------------------------
    # Hộp chờ
    # ------------------------------------------------------------------
    def reclaim_stale(self, minutes: int = 60) -> int:
        """Task 'running' quá lâu (lượt trước bị kill) → trả về 'pending'."""
        with self._tx() as cur:
            cur.execute("""
                UPDATE tasks SET status='pending', locked_at=NULL, updated_at=now()
                 WHERE status='running' AND locked_at < now() - %s * interval '1 minute'
            """, (minutes,))
            return cur.rowcount

    def claim_tasks(self, task_type: str, company_mst: str, limit: int) -> list:
        """
        Nhận tối đa `limit` task đã đến hạn (cũ trước) → status 'running'.
        Trả về list dict: thông tin HĐ + task_id/attempts + (PDF) dữ liệu tra cứu từ XML.
        """
        with self._tx() as cur:
            cur.execute("""
                UPDATE tasks t SET status='running', locked_at=now(), updated_at=now()
                 WHERE t.id IN (
                       SELECT t2.id FROM tasks t2
                         JOIN invoices i2 ON i2.id = t2.invoice_id
                        WHERE t2.task_type = %s AND t2.status = 'pending'
                          AND t2.next_run_at <= now() AND i2.company_mst = %s
                        ORDER BY t2.next_run_at, t2.id
                        LIMIT %s
                        FOR UPDATE OF t2 SKIP LOCKED)
             RETURNING t.id AS task_id, t.invoice_id, t.attempts, t.next_run_at
            """, (task_type, company_mst, limit))
            claimed = cur.fetchall()
            if not claimed:
                return []
            cur.execute("""
                SELECT i.*, x.mst_tcgp, x.ma_tra_cuu, x.lookup_url,
                       x.parsed -> 'tt_khac' AS tt_khac, x.parsed ->> 'ma_cqt' AS ma_cqt,
                       x.parsed ->> 'dlhdon_id' AS dlhdon_id, x.xml_path,
                       p.source_id
                  FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                  LEFT JOIN einvoice_providers p ON p.mst_tcgp = x.mst_tcgp
                 WHERE i.id = ANY(%s)
            """, ([c["invoice_id"] for c in claimed],))
            info = {r["id"]: r for r in cur.fetchall()}
        out = []
        for c in sorted(claimed, key=lambda c: (c["next_run_at"], c["task_id"])):
            row = dict(info[c["invoice_id"]])
            row.update(task_id=c["task_id"], attempts=c["attempts"])
            out.append(row)
        return out

    def release_tasks(self, task_ids: list) -> None:
        """Trả task về 'pending' KHÔNG tính lần thử (dừng sớm, lỗi đăng nhập...)."""
        if not task_ids:
            return
        with self._tx() as cur:
            cur.execute("""
                UPDATE tasks SET status='pending', locked_at=NULL, updated_at=now()
                 WHERE id = ANY(%s) AND status='running'
            """, (list(task_ids),))

    def fail_task(self, task: dict, kind: str, error: str, run_id: int | None) -> str:
        """Ghi 1 lần thất bại, tính lần thử lại theo job_policy. Trả về status mới."""
        attempts = task["attempts"] + 1
        status, next_at = job_policy.next_retry(kind, attempts, datetime.now(timezone.utc))
        with self._tx() as cur:
            cur.execute("""
                UPDATE tasks SET status=%s, attempts=%s,
                       next_run_at=COALESCE(%s, next_run_at),
                       error_kind=%s, last_error=%s, locked_at=NULL, updated_at=now()
                 WHERE id=%s
            """, (status, attempts, next_at, kind, error[:2000], task["task_id"]))
            self._log_attempt(cur, task["task_id"], run_id, False, kind, error)
        return status

    def mark_no_source(self, task: dict, reason: str, run_id: int | None,
                       kind: str = "no_source") -> None:
        """
        Task chưa thể làm → nằm chờ, không tốn lượt thử. kind:
          no_source     PDF: chưa có adapter cho đơn vị cung cấp HĐĐT (tự chạy lại khi có adapter)
          no_portal     PDF: có adapter nhưng chưa biết cổng tra cứu của người bán (chạy lại thủ công)
          summary_only  XML: Thuế không có XML (HĐ gửi theo bảng tổng hợp) → chờ XML từ mail
        """
        with self._tx() as cur:
            cur.execute("""
                UPDATE tasks SET status='no_source', error_kind=%s, last_error=%s,
                       locked_at=NULL, updated_at=now()
                 WHERE id=%s
            """, (kind, reason, task["task_id"]))
            self._log_attempt(cur, task["task_id"], run_id, False, kind, reason)

    def complete_xml(self, task: dict, run_id: int | None, xml_path: str, sha256: str,
                     parsed: dict, partner: dict | None) -> None:
        """
        1 transaction: lưu XML đã parse + upsert NCC/đối tác + đơn vị cung cấp HĐĐT
        + task XML done + tạo task PDF.
        """
        with self._tx() as cur:
            self._save_xml(cur, task["id"], xml_path, sha256, parsed, partner, "xml")
            self._mark_done(cur, task, run_id)
            cur.execute("""
                INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'pdf')
                ON CONFLICT (invoice_id, task_type) DO NOTHING
            """, (task["id"],))

    @classmethod
    def _save_xml(cls, cur, invoice_id: int, xml_path: str, sha256: str, parsed: dict,
                  partner: dict | None, nguon: str) -> None:
        """invoice_xml + NCC/đối tác + đơn vị cung cấp HĐĐT + dòng hàng, trong transaction đang mở."""
        tc = parsed.get("thong_tin_chung") or {}
        mst_tcgp = (tc.get("mst_tcgp") or "").strip() or None
        if mst_tcgp and not job_policy.is_valid_mst(mst_tcgp):
            mst_tcgp = None
        if mst_tcgp:
            cur.execute("""
                INSERT INTO einvoice_providers (mst_tcgp) VALUES (%s)
                ON CONFLICT (mst_tcgp) DO NOTHING
            """, (mst_tcgp,))
        if partner and job_policy.is_valid_mst(partner.get("mst")):
            cur.execute("""
                INSERT INTO partners (mst, name, address, phone, email, bank_account, bank_name)
                VALUES (%(mst)s, %(ten)s, %(dia_chi)s, %(dien_thoai)s, %(email)s,
                        %(so_tk)s, %(ngan_hang)s)
                ON CONFLICT (mst) DO UPDATE SET
                    name         = COALESCE(NULLIF(EXCLUDED.name, ''), partners.name),
                    address      = COALESCE(NULLIF(EXCLUDED.address, ''), partners.address),
                    phone        = COALESCE(NULLIF(EXCLUDED.phone, ''), partners.phone),
                    email        = COALESCE(NULLIF(EXCLUDED.email, ''), partners.email),
                    bank_account = COALESCE(NULLIF(EXCLUDED.bank_account, ''), partners.bank_account),
                    bank_name    = COALESCE(NULLIF(EXCLUDED.bank_name, ''), partners.bank_name),
                    updated_at   = now()
            """, {k: (partner.get(k) or None) for k in
                  ("ten", "dia_chi", "dien_thoai", "email", "so_tk", "ngan_hang")}
                 | {"mst": partner["mst"].strip()})
        cur.execute("""
            INSERT INTO invoice_xml (invoice_id, xml_path, xml_sha256, mst_tcgp,
                                     ma_tra_cuu, lookup_url, parsed)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (invoice_id) DO UPDATE SET
                xml_path = EXCLUDED.xml_path, xml_sha256 = EXCLUDED.xml_sha256,
                mst_tcgp = EXCLUDED.mst_tcgp, ma_tra_cuu = EXCLUDED.ma_tra_cuu,
                lookup_url = EXCLUDED.lookup_url, parsed = EXCLUDED.parsed,
                fetched_at = now()
        """, (invoice_id, xml_path, sha256, mst_tcgp,
              parsed.get("ma_tra_cuu") or None, parsed.get("lookup_url") or None,
              Json(parsed)))
        cls.replace_details(cur, invoice_id, parsed.get("hang_hoa") or [], nguon)

    def complete_pdf(self, task: dict, run_id: int | None, pdf_path: str, sha256: str,
                     source_id: str | None) -> None:
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO invoice_pdf (invoice_id, pdf_path, pdf_sha256, source_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (invoice_id) DO UPDATE SET
                    pdf_path = EXCLUDED.pdf_path, pdf_sha256 = EXCLUDED.pdf_sha256,
                    source_id = EXCLUDED.source_id, fetched_at = now()
            """, (task["id"], pdf_path, sha256, source_id))
            self._mark_done(cur, task, run_id)

    # ------------------------------------------------------------------
    # Mail hóa đơn (lệnh `mail`)
    # ------------------------------------------------------------------
    def known_emails(self, mailbox: str, message_ids: list) -> set:
        if not message_ids:
            return set()
        with self._tx() as cur:
            # Cùng tài khoản (mailbox = "user/thư mục"): mail bị chuyển thư mục vẫn là đã xem.
            # Mail lỗi khi xử lý ('error') không tính là đã xem → lượt sau thử lại.
            cur.execute("""SELECT message_id FROM email_messages
                            WHERE split_part(mailbox, '/', 1) = split_part(%s, '/', 1)
                              AND message_id = ANY(%s) AND status <> 'error'""",
                        (mailbox, list(message_ids)))
            return {r["message_id"] for r in cur.fetchall()}

    def record_email(self, mailbox: str, message_id: str, is_invoice: bool, status: str,
                     stats: dict | None = None, subject: str | None = None,
                     from_addr: str | None = None, received_at=None) -> int:
        """Ghi/cập nhật 1 mail. Mail không phải HĐ: chỉ Message-ID (không lưu tiêu đề/người gửi)."""
        if not is_invoice:
            subject = from_addr = None
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO email_messages (mailbox, message_id, received_at, subject, from_addr,
                                            is_invoice, status, stats)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mailbox, message_id) DO UPDATE SET
                    status = EXCLUDED.status, stats = EXCLUDED.stats, processed_at = now()
                RETURNING id
            """, (mailbox, message_id, received_at, subject, from_addr, is_invoice, status,
                  Json(stats or {}, dumps=_json_dumps)))
            return cur.fetchone()["id"]

    def managed_msts(self) -> set:
        with self._tx() as cur:
            cur.execute("SELECT company_mst FROM companies")
            return {r["company_mst"] for r in cur.fetchall()}

    def invoices_between(self, company_mst: str, start: date, end: date) -> list:
        """HĐ của 1 công ty có ngày lập trong [start, end], kèm đã có XML/PDF chưa."""
        with self._tx() as cur:
            cur.execute("""
                SELECT i.*, (x.invoice_id IS NOT NULL) AS has_xml, (p.invoice_id IS NOT NULL) AS has_pdf
                  FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                  LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
                 WHERE i.company_mst = %s AND i.ngay_lap BETWEEN %s AND %s
                 ORDER BY i.ngay_lap, i.nbmst, i.khhdon, i.shdon
            """, (company_mst, start, end))
            return cur.fetchall()

    def find_invoices(self, nbmst: str, khmshdon, khhdon: str, shdon) -> list:
        """HĐ theo khóa (mọi công ty), kèm trạng thái XML/PDF đã có chưa."""
        with self._tx() as cur:
            cur.execute("""
                SELECT i.*, (x.invoice_id IS NOT NULL) AS has_xml, (p.invoice_id IS NOT NULL) AS has_pdf
                  FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                  LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
                 WHERE i.nbmst=%s AND i.khmshdon=%s AND i.khhdon=%s AND i.shdon=%s
            """, (nbmst, int(khmshdon), khhdon, int(shdon)))
            return cur.fetchall()

    def find_invoices_in_text(self, msts: list, text: str) -> list:
        """HĐ của các MST người bán `msts` mà ký hiệu + số HĐ đều xuất hiện trong `text` (PDF lẻ)."""
        if not msts:
            return []
        with self._tx() as cur:
            cur.execute("""
                SELECT i.*, (p.invoice_id IS NOT NULL) AS has_pdf
                  FROM invoices i LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
                 WHERE i.nbmst = ANY(%s)
                   AND strpos(%s, i.khhdon) > 0 AND strpos(%s, i.shdon::text) > 0
            """, (list(msts), text, text))
            return cur.fetchall()

    def add_email_file(self, email_id: int, f: dict) -> int | None:
        """Ghi 1 file lấy từ mail; trùng (cùng mail + sha256) → None."""
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO email_files (email_id, filename, kind, sha256, saved_path, nbmst, khmshdon,
                                         khhdon, shdon, nmmst, invoice_id, status, note)
                VALUES (%(email_id)s, %(filename)s, %(kind)s, %(sha256)s, %(saved_path)s, %(nbmst)s,
                        %(khmshdon)s, %(khhdon)s, %(shdon)s, %(nmmst)s, %(invoice_id)s, %(status)s,
                        %(note)s)
                ON CONFLICT (email_id, sha256) DO NOTHING
                RETURNING id
            """, {"nbmst": None, "khmshdon": None, "khhdon": None, "shdon": None, "nmmst": None,
                  "invoice_id": None, "note": None, **f, "email_id": email_id})
            row = cur.fetchone()
            return row["id"] if row else None

    def set_email_file(self, file_id: int, status: str, invoice_id: int | None = None,
                       note: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute("""
                UPDATE email_files SET status=%s, invoice_id=COALESCE(%s, invoice_id),
                       note=COALESCE(%s, note) WHERE id=%s
            """, (status, invoice_id, note, file_id))

    def unmatched_email_files(self) -> list:
        with self._tx() as cur:
            cur.execute("SELECT * FROM email_files WHERE status='unmatched' ORDER BY id")
            return cur.fetchall()

    def add_email_links(self, email_id: int, links: list, invoice_id: int | None = None) -> int:
        n = 0
        with self._tx() as cur:
            for link in links:
                cur.execute("""
                    INSERT INTO email_links (email_id, url, host, code, invoice_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (email_id, url) DO NOTHING
                """, (email_id, link["url"], link["host"], link.get("code"), invoice_id))
                n += cur.rowcount
        return n

    def apply_email_xml(self, invoice_id: int, run_id: int | None, xml_path: str, sha256: str,
                        parsed: dict, partner: dict | None) -> bool:
        """
        XML lấy từ mail cho HĐ chưa có XML (vd hthdon=2: Thuế không có XML) → lưu như XML từ
        GDT với nguon='xml_email', task XML → done, tạo task PDF. HĐ đã có XML → False (XML
        của Thuế là bản chuẩn, không ghi đè).
        """
        with self._tx() as cur:
            cur.execute("SELECT 1 FROM invoice_xml WHERE invoice_id=%s FOR UPDATE", (invoice_id,))
            if cur.fetchone():
                return False
            self._save_xml(cur, invoice_id, xml_path, sha256, parsed, partner, "xml_email")
            self._finish_task(cur, invoice_id, "xml", run_id)
            cur.execute("""
                INSERT INTO tasks (invoice_id, task_type) VALUES (%s, 'pdf')
                ON CONFLICT (invoice_id, task_type) DO NOTHING
            """, (invoice_id,))
            return True

    def pending_email_links(self, max_attempts: int = 3) -> list:
        """Link 'new' (chưa xử lý / lỗi chưa quá max_attempts lần), kèm tiêu đề + mã trong mail."""
        with self._tx() as cur:
            cur.execute("""
                SELECT l.*, m.subject, m.stats -> 'codes' AS mail_codes
                  FROM email_links l JOIN email_messages m ON m.id = l.email_id
                 WHERE l.status = 'new' AND l.attempts < %s
                 ORDER BY l.email_id, l.id
            """, (max_attempts,))
            return cur.fetchall()

    def set_email_link(self, link_id: int, status: str, invoice_id: int | None = None,
                       error: str | None = None, failed: bool = False) -> None:
        with self._tx() as cur:
            cur.execute("""
                UPDATE email_links SET status=%s, invoice_id=COALESCE(%s, invoice_id),
                       last_error=%s, attempts = attempts + %s, updated_at=now()
                 WHERE id=%s
            """, (status, invoice_id, (error or "")[:1000] or None, 1 if failed else 0, link_id))

    def invoice_by_id(self, invoice_id: int) -> dict | None:
        with self._tx() as cur:
            cur.execute("""
                SELECT i.*, (x.invoice_id IS NOT NULL) AS has_xml, (p.invoice_id IS NOT NULL) AS has_pdf
                  FROM invoices i
                  LEFT JOIN invoice_xml x ON x.invoice_id = i.id
                  LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
                 WHERE i.id = %s
            """, (invoice_id,))
            return cur.fetchone()

    def apply_email_pdf(self, invoice_id: int, run_id: int | None, pdf_path: str, sha256: str,
                        source_id: str = "email") -> bool:
        """PDF bản gốc lấy từ mail (đính kèm, hoặc tải bằng mã trong mail). HĐ đã có PDF → False."""
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO invoice_pdf (invoice_id, pdf_path, pdf_sha256, source_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (invoice_id) DO NOTHING
                RETURNING invoice_id
            """, (invoice_id, pdf_path, sha256, source_id))
            if not cur.fetchone():
                return False
            self._finish_task(cur, invoice_id, "pdf", run_id)
            return True

    @classmethod
    def _finish_task(cls, cur, invoice_id: int, task_type: str, run_id: int | None) -> None:
        """Task (đang ở bất kỳ trạng thái nào, hoặc chưa có) → done, ghi 1 lần thử OK."""
        cur.execute("""
            INSERT INTO tasks (invoice_id, task_type, status) VALUES (%s, %s, 'done')
            ON CONFLICT (invoice_id, task_type) DO UPDATE SET
                status='done', error_kind=NULL, last_error=NULL, locked_at=NULL, updated_at=now()
            RETURNING id
        """, (invoice_id, task_type))
        cls._log_attempt(cur, cur.fetchone()["id"], run_id, True, "email", None)

    _DETAIL_EXTRA_KEYS = ("tt_khac", "hang_dac_trung", "ten_hang", "don_vi_tt", "ty_gia",
                          "tien_thue_duoc_hoan", "tinh_chat_ten")

    @classmethod
    def replace_details(cls, cur, invoice_id: int, items: list, nguon: str) -> None:
        """Ghi lại toàn bộ dòng hàng của 1 HĐ (xóa cũ → ghi mới) trong transaction đang mở."""
        cur.execute("DELETE FROM invoice_details WHERE invoice_id=%s", (invoice_id,))
        for line_no, it in enumerate(items, 1):
            extra = {k: it[k] for k in cls._DETAIL_EXTRA_KEYS if it.get(k) not in (None, "", [], {})}
            cur.execute("""
                INSERT INTO invoice_details (invoice_id, line_no, stt, tinh_chat, ma_hhdv, ten_hhdv,
                    don_vi_tinh, so_luong, don_gia, tl_ck, st_ck, thanh_tien, thue_suat, tien_thue,
                    tong_tien, ma_nhom_nganh, extra, nguon)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (invoice_id, line_no, it.get("stt"), it.get("tinh_chat"),
                  it.get("ma_hhdv") or None, it.get("ten_hhdv") or None,
                  it.get("don_vi_tinh") or None, it.get("so_luong"), it.get("don_gia"),
                  it.get("tl_ck"), it.get("st_ck"), it.get("thanh_tien"),
                  it.get("thue_suat") or None, it.get("tien_thue"), it.get("tong_tien"),
                  it.get("ma_nhom_nganh"), Json(extra) if extra else None, nguon))

    @staticmethod
    def _mark_done(cur, task: dict, run_id: int | None) -> None:
        cur.execute("""
            UPDATE tasks SET status='done', error_kind=NULL, last_error=NULL,
                   locked_at=NULL, updated_at=now()
             WHERE id=%s
        """, (task["task_id"],))
        InvoiceStore._log_attempt(cur, task["task_id"], run_id, True, None, None)

    @staticmethod
    def _log_attempt(cur, task_id, run_id, ok, kind, error) -> None:
        cur.execute("""
            INSERT INTO task_attempts (task_id, run_id, ok, error_kind, error)
            VALUES (%s, %s, %s, %s, %s)
        """, (task_id, run_id, ok, kind, (error or "")[:2000] or None))

    # ------------------------------------------------------------------
    # Quản trị
    # ------------------------------------------------------------------
    def requeue(self, status: str, task_type: str | None = None,
                mst_tcgp: str | None = None, error_kind: str | None = None) -> int:
        """Đưa task 'dead' / 'no_source' về hàng đợi (reset số lần thử)."""
        sql = """UPDATE tasks SET status='pending', attempts=0, next_run_at=now(),
                        locked_at=NULL, updated_at=now()
                  WHERE status=%s"""
        params: list = [status]
        if task_type:
            sql += " AND task_type=%s"
            params.append(task_type)
        if error_kind:
            sql += " AND error_kind=%s"
            params.append(error_kind)
        if mst_tcgp:
            sql += " AND invoice_id IN (SELECT invoice_id FROM invoice_xml WHERE mst_tcgp=%s)"
            params.append(mst_tcgp)
        with self._tx() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def set_provider_source(self, mst_tcgp: str, source_id: str | None,
                            name: str | None = None) -> int:
        """Gắn adapter cho 1 đơn vị cung cấp HĐĐT, rồi đưa các task PDF đang chờ về hàng đợi."""
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO einvoice_providers (mst_tcgp, name, source_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (mst_tcgp) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    name = COALESCE(EXCLUDED.name, einvoice_providers.name)
            """, (mst_tcgp, name, source_id))
        if not source_id:
            return 0
        return self.requeue("no_source", task_type="pdf", mst_tcgp=mst_tcgp)

    def last_ok_run(self, command: str, on_day: date) -> bool:
        """Đã có lượt `command` chạy thành công trong ngày `on_day` chưa."""
        with self._tx() as cur:
            cur.execute("""
                SELECT 1 FROM job_runs
                 WHERE command=%s AND status='ok'
                   AND (started_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = %s
                 LIMIT 1
            """, (command, on_day))
            return cur.fetchone() is not None

    def start_run(self, command: str) -> int:
        with self._tx() as cur:
            cur.execute("INSERT INTO job_runs (command) VALUES (%s) RETURNING id", (command,))
            return cur.fetchone()["id"]

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        with self._tx() as cur:
            cur.execute("""
                UPDATE job_runs SET finished_at=now(), status=%s, stats=%s WHERE id=%s
            """, (status, Json(stats, dumps=_json_dumps), run_id))

    def begin_run_company(self, run_id: int, company_mst: str) -> None:
        """Mốc bắt đầu của 1 công ty trong lượt chạy. Gọi lại không sao (giữ mốc đầu)."""
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO job_run_companies (run_id, company_mst) VALUES (%s, %s)
                ON CONFLICT (run_id, company_mst) DO NOTHING
            """, (run_id, company_mst))

    def finish_run_company(self, run_id: int, company_mst: str, status: str,
                           stats: dict) -> None:
        """Kết quả của 1 công ty trong lượt chạy.

        `tick` gọi hai lần cho cùng một công ty — một lần sau phần GDT, một lần sau
        phần PDF — nên stats được **trộn** chứ không ghi đè: khóa của lượt sau thắng,
        khóa chỉ có ở lượt trước vẫn còn.
        """
        with self._tx() as cur:
            cur.execute("""
                INSERT INTO job_run_companies (run_id, company_mst, finished_at, status, stats)
                     VALUES (%s, %s, now(), %s, %s)
                ON CONFLICT (run_id, company_mst) DO UPDATE
                    SET finished_at = now(),
                        status      = EXCLUDED.status,
                        stats       = job_run_companies.stats || EXCLUDED.stats
            """, (run_id, company_mst, status, Json(stats, dumps=_json_dumps)))

    def status_report(self) -> dict:
        with self._tx() as cur:
            cur.execute("""
                SELECT task_type, status, count(*) AS n FROM tasks
                 GROUP BY task_type, status ORDER BY task_type, status
            """)
            counts = cur.fetchall()
            cur.execute("""
                SELECT task_type, status, error_kind, count(*) AS n FROM tasks
                 WHERE status IN ('pending','dead') AND error_kind IS NOT NULL
                 GROUP BY task_type, status, error_kind ORDER BY n DESC
            """)
            errors = cur.fetchall()
            cur.execute("""
                SELECT x.mst_tcgp, p.name, count(*) AS n, min(x.lookup_url) AS lookup_url
                  FROM tasks t
                  JOIN invoice_xml x ON x.invoice_id = t.invoice_id
                  LEFT JOIN einvoice_providers p ON p.mst_tcgp = x.mst_tcgp
                 WHERE t.task_type='pdf' AND t.status='no_source' AND t.error_kind='no_source'
                 GROUP BY x.mst_tcgp, p.name ORDER BY n DESC
            """)
            providers = cur.fetchall()
            cur.execute("""
                SELECT x.mst_tcgp, i.nbmst, min(i.nbten) AS nbten, count(*) AS n
                  FROM tasks t
                  JOIN invoices i ON i.id = t.invoice_id
                  JOIN invoice_xml x ON x.invoice_id = t.invoice_id
                 WHERE t.task_type='pdf' AND t.status='no_source' AND t.error_kind='no_portal'
                 GROUP BY x.mst_tcgp, i.nbmst ORDER BY n DESC
            """)
            no_portal = cur.fetchall()
            cur.execute("""
                SELECT i.nbmst, min(i.nbten) AS nbten, count(*) AS n
                  FROM tasks t JOIN invoices i ON i.id = t.invoice_id
                 WHERE t.task_type='xml' AND t.error_kind='summary_only' AND t.status='no_source'
                 GROUP BY i.nbmst ORDER BY n DESC
            """)
            summary_only = cur.fetchall()
            cur.execute("""
                SELECT id, command, started_at, finished_at, status, stats
                  FROM job_runs ORDER BY id DESC LIMIT 5
            """)
            runs = cur.fetchall()
            cur.execute("""
                SELECT status, count(*) AS n FROM email_messages WHERE is_invoice
                 GROUP BY status ORDER BY n DESC
            """)
            mails = cur.fetchall()
            cur.execute("""
                SELECT kind, status, count(*) AS n FROM email_files GROUP BY kind, status ORDER BY kind, n DESC
            """)
            mail_files = cur.fetchall()
            cur.execute("""
                SELECT host, count(*) AS n, count(code) AS with_code FROM email_links
                 WHERE status='new' GROUP BY host ORDER BY n DESC LIMIT 20
            """)
            mail_links = cur.fetchall()
        return {"counts": counts, "errors": errors, "providers": providers,
                "no_portal": no_portal, "summary_only": summary_only, "runs": runs,
                "mails": mails, "mail_files": mail_files, "mail_links": mail_links}


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
