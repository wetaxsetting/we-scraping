# mail_ingest.py
"""
Lấy XML/PDF hóa đơn từ hộp mail (lệnh `invoice_job.py mail`, và 1 bước trong `tick`).

Mỗi mail (trong N ngày gần nhất, chưa xem):
  1. Không phải mail HĐ → chỉ ghi Message-ID (lần sau bỏ qua), không lưu gì khác.
  2. XML đính kèm (cả trong .zip): parse → khớp HĐ trong DB theo (MST bán, mẫu số, ký hiệu, số)
     mà bên mua/bán là công ty mình quản lý.
       - HĐ chưa có XML (vd hthdon=2: Thuế không có XML) → lưu như XML từ GDT, nguon='xml_email'.
       - HĐ đã có XML từ Thuế → bỏ qua (XML của Thuế là bản chuẩn).
       - Chưa có HĐ trong DB (mail đến trước khi GDT liệt kê) → lưu tạm output/mail/unmatched/,
         lượt sau khớp lại.
  3. PDF đính kèm → ghép với XML cùng mail (theo số HĐ), hoặc dò trong chữ của PDF
     (MST người bán + ký hiệu + số HĐ). Khớp → làm PDF bản gốc (source 'email').
  4. Link tra cứu + mã trong nội dung → bảng email_links (KHÔNG tự mở link). Chỉ gọi cổng tra cứu
     khi mail KHÔNG kèm PDF hóa đơn — có file trong mail thì ưu tiên file, đỡ tốn thời gian.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import api_client
import mail_reader
import xml_parser

log = logging.getLogger("mail_ingest")

UNMATCHED_DIR = Path("mail") / "unmatched"
_MST_RE = re.compile(r"(?<!\d)(\d{10}(?:-\d{3})?)(?!\d)")
# HĐ thay thế / điều chỉnh in kèm số HĐ gốc: "Thay thế cho hóa đơn Mẫu số 2, ký hiệu K26TAF, số 00000148"
_CITED_RE = re.compile(r"(?:thay the|dieu chinh)(?:\s+\w+){0,3}?\s+hoa don(.{0,160})")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _invoice_file(out_dir: Path, kind: str, inv: dict) -> Path:
    """Cùng cấu trúc với job: <out>/<xml|pdf>/<MST công ty>/<năm>/<tháng>/<MST bán>/<tên>."""
    row = {"tdlap": inv["ngay_lap"].isoformat() if inv.get("ngay_lap") else "",
           "nbmst": inv["nbmst"], "khmshdon": inv["khmshdon"], "khhdon": inv["khhdon"],
           "shdon": inv["shdon"]}
    return (out_dir / kind / api_client.invoice_rel_dir(row, inv["company_mst"])
            / api_client.invoice_filename(row, kind))


def _unmatched_file(out_dir: Path, sha: str, filename: str) -> Path:
    safe = re.sub(r"[^\w.\-]+", "_", filename)[-80:] or "file"
    return out_dir / UNMATCHED_DIR / f"{sha[:12]}_{safe}"


def _rel(out_dir: Path, p: Path) -> str:
    return p.relative_to(out_dir).as_posix()


def _pdf_text(data: bytes) -> str:
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "".join(page.get_text() for page in doc)
    except Exception:  # noqa: BLE001
        return ""


def _has_number(text: str, shdon) -> bool:
    return bool(re.search(rf"(?<!\d)0*{int(shdon)}(?!\d)", text))


def _cited_numbers(text: str) -> set:
    """Số HĐ gốc mà HĐ thay thế / điều chỉnh nhắc tới — để PDF không bị coi là trùng 2 HĐ."""
    folded = mail_reader.fold(" ".join(text.split()))
    return {int(n) for m in _CITED_RE.finditer(folded)
            for n in re.findall(r"\bso\s*:?\s*0*(\d{1,10})\b", m.group(1))}


def _xml_key(parsed: dict) -> dict:
    tc = parsed.get("thong_tin_chung") or {}
    return {"nbmst": ((parsed.get("nguoi_ban") or {}).get("mst") or "").strip(),
            "khmshdon": int(tc["ky_hieu_mau"]) if str(tc.get("ky_hieu_mau") or "").isdigit() else None,
            "khhdon": (tc.get("ky_hieu") or "").strip(),
            "shdon": int(tc["so_hd"]) if str(tc.get("so_hd") or "").isdigit() else None,
            "nmmst": ((parsed.get("nguoi_mua") or {}).get("mst") or "").strip()}


class MailIngest:
    def __init__(self, store, out_dir: Path, run_id=None):
        self.store, self.out_dir, self.run_id = store, Path(out_dir), run_id
        self.managed = {m.split("-")[0] for m in store.managed_msts()}

    # ------------------------------------------------------------------
    def _ours(self, key: dict) -> bool:
        return any((key.get(k) or "").split("-")[0] in self.managed for k in ("nmmst", "nbmst"))

    def _match(self, key: dict) -> dict | None:
        if not (key["nbmst"] and key["khmshdon"] and key["khhdon"] and key["shdon"] is not None):
            return None
        rows = [r for r in self.store.find_invoices(key["nbmst"], key["khmshdon"], key["khhdon"],
                                                    key["shdon"])
                if r["company_mst"].split("-")[0] in self.managed]
        return rows[0] if len(rows) == 1 else None

    def _match_pdf_text(self, text: str) -> dict | None:
        if not text:
            return None
        compact = "".join(text.split())
        msts = sorted(set(_MST_RE.findall(compact)) | set(_MST_RE.findall(text)))
        rows = [r for r in self.store.find_invoices_in_text(msts, compact)
                if r["company_mst"].split("-")[0] in self.managed and _has_number(text, r["shdon"])]
        if len(rows) > 1:                       # PDF HĐ thay thế/điều chỉnh có cả số HĐ gốc → bỏ HĐ gốc
            cited = _cited_numbers(text)
            rows = [r for r in rows if int(r["shdon"]) not in cited] or rows
        return rows[0] if len(rows) == 1 else None

    # ------------------------------------------------------------------
    def _save_xml(self, email_id: int, f, stats) -> dict | None:
        """Trả về HĐ đã khớp (để ghép PDF cùng mail), hoặc None."""
        sha = hashlib.sha256(f.data).hexdigest()
        try:
            parsed = xml_parser.parse_xml_bytes(f.data)
        except (ValueError, SyntaxError) as e:
            stats["xml_not_invoice"] += 1
            self.store.add_email_file(email_id, {"filename": f.filename, "kind": "xml", "sha256": sha,
                                                 "saved_path": "", "status": "not_invoice",
                                                 "note": str(e)[:300]})
            return None
        key = _xml_key(parsed)
        base = {"filename": f.filename, "kind": "xml", "sha256": sha, **key}
        if not self._ours(key):
            stats["xml_not_ours"] += 1
            self.store.add_email_file(email_id, {**base, "saved_path": "", "status": "not_ours"})
            return None
        inv = self._match(key)
        if inv is None:
            path = _unmatched_file(self.out_dir, sha, f.filename)
            _atomic_write(path, f.data)
            self.store.add_email_file(email_id, {**base, "saved_path": _rel(self.out_dir, path),
                                                 "status": "unmatched"})
            stats["xml_unmatched"] += 1
            return {"_key": key}
        return self._apply_xml(email_id, inv, f.data, sha, parsed, base, stats)

    def _apply_xml(self, email_id, inv, data, sha, parsed, base, stats, file_id=None) -> dict:
        if inv["has_xml"]:
            status = "duplicate"
            stats["xml_duplicate"] += 1
            path_rel = ""
        else:
            path = _invoice_file(self.out_dir, "xml", inv)
            _atomic_write(path, data)
            path_rel = _rel(self.out_dir, path)
            saved = self.store.apply_email_xml(inv["id"], self.run_id, path_rel, sha, parsed,
                                               parsed.get("nguoi_ban"))
            status = "matched" if saved else "duplicate"
            stats["xml_saved" if saved else "xml_duplicate"] += 1
            if saved:
                log.info("Mail → XML %s (HĐ %s %s-%s, nguon=xml_email)", path_rel, inv["nbmst"],
                         inv["khhdon"], inv["shdon"])
        if file_id is None:
            self.store.add_email_file(email_id, {**base, "saved_path": path_rel, "invoice_id": inv["id"],
                                                 "status": status})
        else:
            self.store.set_email_file(file_id, status, inv["id"])
        return inv

    def _save_pdf(self, email_id: int, f, xml_invoices: list, stats, source_id: str = "email") -> dict | None:
        sha = hashlib.sha256(f.data).hexdigest()
        if not f.data.startswith(b"%PDF"):
            stats["pdf_bad"] += 1
            return None
        text = _pdf_text(f.data)
        inv = None
        key = None
        # 1) ghép với XML cùng mail: số HĐ nằm trong tên file hoặc trong chữ của PDF
        cands = [x for x in xml_invoices if x]
        if len(cands) == 1 and (not text or _has_number(text, (cands[0].get("_key") or cands[0])["shdon"])):
            pick = cands[0]
        else:
            pick = next((x for x in cands
                         if _has_number(f.filename + " " + text, (x.get("_key") or x)["shdon"])), None)
        if pick is not None:
            if "_key" in pick:
                key = pick["_key"]          # XML cùng mail chưa khớp → PDF chờ cùng
            else:
                inv = pick
        # 2) PDF lẻ: dò trong chữ của PDF
        if inv is None and key is None:
            inv = self._match_pdf_text(text)
        base = {"filename": f.filename, "kind": "pdf", "sha256": sha}
        if inv is None:
            path = _unmatched_file(self.out_dir, sha, f.filename)
            _atomic_write(path, f.data)
            self.store.add_email_file(email_id, {**base, **(key or {}), "saved_path": _rel(self.out_dir, path),
                                                 "status": "unmatched"})
            stats["pdf_unmatched"] += 1
            return None
        self._apply_pdf(email_id, inv, f.data, sha, base, stats, source_id=source_id)
        return inv

    def _apply_pdf(self, email_id, inv, data, sha, base, stats, file_id=None, source_id="email") -> None:
        if inv.get("has_pdf"):
            status, path_rel = "duplicate", ""
            stats["pdf_duplicate"] += 1
        else:
            path = _invoice_file(self.out_dir, "pdf", inv)
            _atomic_write(path, data)
            path_rel = _rel(self.out_dir, path)
            saved = self.store.apply_email_pdf(inv["id"], self.run_id, path_rel, sha, source_id=source_id)
            status = "matched" if saved else "duplicate"
            stats["pdf_saved" if saved else "pdf_duplicate"] += 1
            if saved:
                log.info("Mail → PDF %s", path_rel)
        key = {k: inv[k] for k in ("nbmst", "khmshdon", "khhdon", "shdon")}
        if file_id is None:
            self.store.add_email_file(email_id, {**base, **key, "saved_path": path_rel,
                                                 "invoice_id": inv["id"], "status": status})
        else:
            self.store.set_email_file(file_id, status, inv["id"])

    # ------------------------------------------------------------------
    def process(self, mailbox: str, pm, stats) -> str:
        """Xử lý 1 mail đã parse. Trả về status của mail."""
        mid = pm.message_id
        if not pm.is_invoice:
            self.store.record_email(mailbox, mid, False, "ignored")
            stats["mail_ignored"] += 1
            return "ignored"
        stats["mail_invoice"] += 1
        email_id = self.store.record_email(mailbox, mid, True, "processing", subject=pm.subject[:500],
                                           from_addr=pm.from_addr, received_at=pm.received_at)
        local = {"xml": 0, "pdf": 0, "matched": 0, "unmatched": 0}
        before = stats.copy()
        xml_invoices = [self._save_xml(email_id, f, stats) for f in pm.files if f.kind == "xml"]
        pdf_invoices = [self._save_pdf(email_id, f, xml_invoices, stats) for f in pm.files if f.kind == "pdf"]
        matched = {x["id"] for x in xml_invoices + pdf_invoices if x and "id" in x}
        local.update(xml=sum(f.kind == "xml" for f in pm.files), pdf=sum(f.kind == "pdf" for f in pm.files),
                     matched=len(matched),
                     unmatched=(stats["xml_unmatched"] - before["xml_unmatched"])
                     + (stats["pdf_unmatched"] - before["pdf_unmatched"]))
        links = [{"url": l.url, "host": l.host,
                  "code": l.code or (pm.codes[0] if len(pm.codes) == 1 else None)} for l in pm.links]
        local["links"] = self.store.add_email_links(email_id, links,
                                                    next(iter(matched)) if len(matched) == 1 else None)
        local["codes"] = pm.codes
        if pm.seller_msts:
            local["seller_msts"] = pm.seller_msts
        stats["links_new"] += local["links"]
        if pm.files:
            status = ("done" if not local["unmatched"] else "partial" if local["matched"] else "unmatched")
        else:
            status = "links_only" if links else "no_file"
        self.store.record_email(mailbox, mid, True, status, stats=local)
        return status

    def process_links(self, stats, limit: int | None = None) -> None:
        """
        Mail chỉ có link/mã tra cứu (vd HĐ hthdon=2: Thuế không có XML) → adapter của cổng (theo host)
        dựng HĐ từ tham số link/mã (KHÔNG mở link), rồi tải PDF (+ XML nếu adapter có fetch_xml) qua
        endpoint tra cứu của chính nó. Kết quả khớp vào HĐ như file đính kèm.
        Mail đã kèm PDF hóa đơn thì dùng file đó, không gọi cổng (link → 'mail_file').
        """
        import job_policy
        import pdf_providers
        breakers, done_mail, handled = {}, set(), 0
        for link in self.store.pending_email_links():
            if link.get("mail_has_pdf"):
                self.store.set_email_link(link["id"], "mail_file")
                stats["link_mail_has_file"] += 1
                continue
            adapter = pdf_providers.adapter_for_host(link["host"])
            if adapter is None:
                continue                      # chưa có adapter cho cổng này: để 'new', có adapter thì làm
            mail_text = "\n".join([link.get("subject") or ""] + list(link.get("mail_codes") or [])
                                  + [f"MST người bán: {m}" for m in link.get("mail_seller_msts") or []])
            inv = adapter.invoice_from_link(link["url"], link.get("code"), mail_text)
            if inv is None:                   # link không dùng được (trang chủ, hủy nhận mail...)
                self.store.set_email_link(link["id"], "ignored")
                stats["link_ignored"] += 1
                continue
            key = (link["email_id"], adapter.source_id)
            if key in done_mail:              # mail có nhiều link cùng cổng (xem/tải): đã lấy rồi
                self.store.set_email_link(link["id"], "done")
                continue
            target = self.store.invoice_by_id(link["invoice_id"]) if link.get("invoice_id") else None
            if target and target["has_pdf"] and (target["has_xml"] or not hasattr(adapter, "fetch_xml")):
                self.store.set_email_link(link["id"], "done", target["id"])
                done_mail.add(key)
                stats["link_already_have"] += 1
                continue
            br = breakers.setdefault(adapter.source_id, job_policy.CircuitBreaker())
            if br.open or (limit is not None and handled >= limit):
                continue
            handled += 1
            name = f"{adapter.source_id}_link{link['id']}"
            try:
                xml_invoices = []
                if hasattr(adapter, "fetch_xml"):
                    try:
                        xml = adapter.fetch_xml(dict(inv))
                        xml_invoices.append(self._save_xml(link["email_id"],
                                                           mail_reader.MailFile(f"{name}.xml", "xml", xml), stats))
                    except pdf_providers.PdfFetchError as e:     # XML là phần thêm: lỗi thì vẫn lấy PDF
                        log.info("Link %s: không lấy được XML (%s)", link["id"], e)
                pdf = adapter.fetch_pdf(dict(inv))
                cands = [x for x in xml_invoices if x] or ([target] if target else [])
                matched = self._save_pdf(link["email_id"], mail_reader.MailFile(f"{name}.pdf", "pdf", pdf),
                                         cands, stats, source_id=f"email:{adapter.source_id}")
                br.record(True)
                self.store.set_email_link(link["id"], "done", (matched or {}).get("id"))
                done_mail.add(key)
                stats["link_fetched"] += 1
            except Exception as e:  # noqa: BLE001  1 link lỗi không dừng cả lượt
                kind = getattr(e, "kind", "other")
                br.record(False, kind)
                # không có mã / không có cổng: thử lại vô ích; còn lại thử lại lượt sau (tối đa 3 lần)
                status = "failed" if kind in ("no_code", "no_portal") else "new"
                self.store.set_email_link(link["id"], status, error=f"{kind}: {e}", failed=True)
                stats["link_failed"] += 1
                log.warning("Link %s (%s) lỗi %s: %s", link["id"], link["host"], kind, e)

    def rematch(self, stats) -> None:
        """File lượt trước chưa khớp (HĐ chưa lên danh sách GDT) → khớp lại."""
        for row in self.store.unmatched_email_files():
            path = self.out_dir / row["saved_path"]
            try:
                data = path.read_bytes()
            except OSError:
                continue
            key = {k: row[k] for k in ("nbmst", "khmshdon", "khhdon", "shdon")}
            if row["kind"] == "xml":
                inv = self._match({**key, "nmmst": row["nmmst"] or ""})
                if inv:
                    parsed = xml_parser.parse_xml_bytes(data)
                    self._apply_xml(row["email_id"], inv, data, row["sha256"], parsed, {}, stats,
                                    file_id=row["id"])
            else:
                inv = (self._match({**key, "nmmst": ""}) if key["nbmst"] and key["shdon"] is not None
                       else self._match_pdf_text(_pdf_text(data)))
                if inv:
                    self._apply_pdf(row["email_id"], inv, data, row["sha256"], {}, stats, file_id=row["id"])
            if inv:
                stats["rematched"] += 1
                path.unlink(missing_ok=True)          # đã chép sang đúng chỗ (hoặc trùng) → bỏ bản tạm


class MailConfigError(Exception):
    """Đã bật mail (có INVOICE_MAIL_USER) nhưng cấu hình chưa đủ / sai."""


# Công tắc INVOICE_MAIL_AUTH → biến bắt buộc + máy chủ IMAP mặc định
AUTH_MODES = {
    "password":         {"required": ("INVOICE_MAIL_HOST", "INVOICE_MAIL_PASSWORD"), "host": None},
    "oauth2_microsoft": {"required": ("INVOICE_MAIL_MS_TENANT_ID", "INVOICE_MAIL_MS_CLIENT_ID",
                                      "INVOICE_MAIL_MS_CLIENT_SECRET"),
                         "host": "outlook.office365.com"},
    "oauth2_google":    {"required": ("INVOICE_MAIL_GOOGLE_CLIENT_ID", "INVOICE_MAIL_GOOGLE_CLIENT_SECRET",
                                      "INVOICE_MAIL_GOOGLE_REFRESH_TOKEN"),
                         "host": "imap.gmail.com"},
}


def mail_config() -> dict | None:
    """
    Cấu hình hộp mail từ env. Chưa có INVOICE_MAIL_USER → None (bước mail bị bỏ qua).
    INVOICE_MAIL_AUTH = password (mặc định) | oauth2_microsoft | oauth2_google.
    Thiếu biến bắt buộc của chế độ đã chọn → MailConfigError (nêu rõ thiếu gì, không in giá trị).
    """
    user = (os.getenv("INVOICE_MAIL_USER") or "").strip()
    if not user:
        return None
    auth = (os.getenv("INVOICE_MAIL_AUTH") or "password").strip().lower()
    mode = AUTH_MODES.get(auth)
    if mode is None:
        raise MailConfigError(f"INVOICE_MAIL_AUTH={auth!r} không hợp lệ — chọn: {', '.join(AUTH_MODES)}")
    env = {k: (os.getenv(k) or "").strip() for k in mode["required"]}
    if mode["host"]:
        env.pop("INVOICE_MAIL_HOST", None)
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise MailConfigError(f"INVOICE_MAIL_AUTH={auth}: thiếu {', '.join(missing)} trong .env")
    try:
        port = int(os.getenv("INVOICE_MAIL_PORT") or 993)
        days = int(os.getenv("INVOICE_MAIL_DAYS") or 7)
    except ValueError as e:
        raise MailConfigError(f"INVOICE_MAIL_PORT / INVOICE_MAIL_DAYS phải là số: {e}") from e

    if auth == "oauth2_microsoft":
        token_provider = lambda: mail_reader.microsoft_token(            # noqa: E731
            env["INVOICE_MAIL_MS_TENANT_ID"], env["INVOICE_MAIL_MS_CLIENT_ID"],
            env["INVOICE_MAIL_MS_CLIENT_SECRET"])
    elif auth == "oauth2_google":
        token_provider = lambda: mail_reader.google_token(               # noqa: E731
            env["INVOICE_MAIL_GOOGLE_CLIENT_ID"], env["INVOICE_MAIL_GOOGLE_CLIENT_SECRET"],
            env["INVOICE_MAIL_GOOGLE_REFRESH_TOKEN"])
    else:
        token_provider = None
    # Nhiều thư mục cách nhau dấu phẩy (vd "INBOX,HDDT"): mail bị bộ lọc chuyển thư mục vẫn không bị sót
    folders = [f.strip() for f in (os.getenv("INVOICE_MAIL_FOLDER") or "INBOX").split(",") if f.strip()]
    return {"auth": auth, "host": (os.getenv("INVOICE_MAIL_HOST") or "").strip() or mode["host"],
            "port": port, "user": user, "password": env.get("INVOICE_MAIL_PASSWORD"),
            "token_provider": token_provider, "folders": folders or ["INBOX"], "days": days}


def open_mailbox(cfg: dict, folder: str) -> mail_reader.ImapMailbox:
    return mail_reader.ImapMailbox(cfg["host"], cfg["port"], cfg["user"], cfg["password"], folder,
                                   auth=cfg["auth"], token_provider=cfg["token_provider"])


def process_links(store, out_dir: Path, run_id, stats, limit: int | None = None) -> None:
    """Gọi 1 lần mỗi lượt, sau khi đã đọc xong mọi thư mục mail."""
    MailIngest(store, out_dir, run_id).process_links(stats, limit)


def run(store, mailbox, out_dir: Path, run_id, days: int, stats, limit: int | None = None) -> None:
    """Lấy mail mới trong `days` ngày từ `mailbox` (ImapMailbox đã mở) và xử lý."""
    ingest = MailIngest(store, out_dir, run_id)
    ingest.rematch(stats)
    items = mailbox.message_ids_since(days)
    ids = [mid or f"uid:{uid}" for uid, mid in items]
    known = store.known_emails(mailbox.name, ids)
    todo = [(uid, mid) for (uid, _), mid in zip(items, ids) if mid not in known]
    stats["mail_seen"] += len(items)
    stats["mail_new"] += len(todo)
    for uid, mid in todo[:limit] if limit else todo:
        try:
            pm = mail_reader.parse_message(mailbox.fetch(uid))
            pm.message_id = pm.message_id or mid
            ingest.process(mailbox.name, pm, stats)
        except mail_reader.MailError as e:
            stats["mail_error"] += 1
            log.warning("Mail UID %s: %s", uid, e)
        except Exception as e:  # noqa: BLE001  1 mail lỗi không được dừng cả lượt
            stats["mail_error"] += 1
            log.exception("Mail UID %s lỗi khi xử lý: %s", uid, e)
            store.record_email(mailbox.name, mid, True, "error", stats={"error": str(e)[:500]})
