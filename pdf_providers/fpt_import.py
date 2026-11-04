# pdf_providers/fpt_import.py
"""
FPT eInvoice (tracuuhoadon.fpt.com.vn) — MSTTCGP 0104128565.

Cổng có reCAPTCHA v3 (server gọi siteverify), nên job KHÔNG tự tra. Người chạy script trình duyệt
`pdf_providers/fpt.py` (mở `?mst=<MST bán>&sec=<mã>`, mã `sec` chỉ có trong mail) và lưu JSON
trả về của `GET /api/search` vào thư mục chờ:

    <INVOICE_OUTPUT_DIR>/manual/fpt/*.json

JSON đó có sẵn mọi thứ: `doc.stax` (MST bán), `doc.form` (mẫu), `doc.serial` (ký hiệu),
`doc.seq` ("00000943"), `doc.idt`, `pdf` ("data:application/pdf;base64, JVBERi0...") và `xml`.
Adapter khớp HĐ theo (MST bán, mẫu, ký hiệu, số), kiểm số HĐ trong PDF rồi trả PDF; file JSON đã
dùng chuyển sang `used/`. Chưa có JSON của HĐ → `no_portal` (task nằm chờ, không tốn lượt);
`_prepare_queues` thấy còn JSON chưa dùng trong thư mục chờ thì đưa các task đó vào lại hộp chờ.

Thật 03/11/2026: C25TAB 943/1128/1129/1156 (Samsung SDS 2300680991), JSON ~1 MB/HĐ.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import ROOT, check_pdf_is_invoice, is_pdf

log = logging.getLogger(__name__)


def drop_dir() -> Path:
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "manual" / "fpt"


def _key(stax, form, serial, seq) -> tuple:
    try:
        seq = int(str(seq).strip())
    except ValueError:
        return ()
    return (str(stax or "").strip(), str(form or "").strip(), str(serial or "").strip().upper(), seq)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        doc = data["doc"]
        return {"key": _key(doc.get("stax"), doc.get("form"), doc.get("serial"), doc.get("seq")),
                "sec": doc.get("sec"), "data": data}
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("JSON FPT không đọc được %s: %s", path, e)
        return None


class FptAdapter(PdfAdapter):
    source_id = "fpt"
    name = "FPT eInvoice"
    mst_tcgp = ("0104128565",)
    # Không khai `hosts`: link mail FPT giữ `new` (khai mà invoice_from_link trả None thì thành `ignored`).

    def has_pending_input(self) -> bool:
        d = drop_dir()
        return d.is_dir() and any(d.glob("*.json"))

    def fetch_pdf(self, invoice: dict) -> bytes:
        want = _key(invoice.get("nbmst"), invoice.get("khmshdon"), invoice.get("khhdon"),
                    invoice.get("shdon"))
        d = drop_dir()
        for path in sorted(d.glob("*.json")) if d.is_dir() else ():
            hit = _read(path)
            if not hit or hit["key"] != want:
                continue
            b64 = str(hit["data"].get("pdf") or "")
            pdf = base64.b64decode(b64.split(",", 1)[-1].strip() or b"")
            if not is_pdf(pdf):
                raise PdfFetchError(f"JSON FPT {path.name} không có PDF hợp lệ", "bad_content")
            check_pdf_is_invoice(pdf, invoice)
            used = d / "used"
            used.mkdir(exist_ok=True)
            path.replace(used / path.name)
            return pdf
        raise PdfFetchError(
            f"FPT: chưa có JSON /api/search của HĐ {want[1:] if want else '?'} trong {d} — chạy script "
            "trình duyệt pdf_providers/fpt.py với MST bán + mã sec (trong mail)", "no_portal")


register(FptAdapter())
