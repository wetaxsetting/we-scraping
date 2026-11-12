"""web/fetch_missing_xml.py: tải XML cho danh sách HĐ thiếu XML (offline, GDT giả).

Sai ở đây thì: tra GDT bằng MST mất số 0 đầu (không ra HĐ nào), gọi nhầm endpoint thường cho
HĐ máy tính tiền, hoặc chạy lại thì tải lại cả HĐ đã có file.

Run::

    pytest tests/test_fetch_missing_xml.py -q
"""

from __future__ import annotations

import importlib
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "web"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

fmx = importlib.import_module("fetch_missing_xml")

CSV = ("tei_einvoice_m_pk,company_tax_code,year,month,form_no,serial_no,invoice_no,invoice_date,note\n"
       "1,318793907,2026,8,1,C26MSC,48135,8/10/2026,no_xml_content\n"
       "2,318793907,2026,9,1,C26TSE,512,9/2/2026,no_xml_content\n")


def _zip(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("invoice.xml", xml)
    return buf.getvalue()


def test_normalize_mst_restores_leading_zero():
    assert fmx.normalize_mst("318793907") == "0318793907"
    assert fmx.normalize_mst("0108515678-001") == "0108515678-001"
    assert fmx.normalize_mst("108515678-001") == "0108515678-001"


def test_mtt_by_fourth_char():
    assert fmx.is_mtt("C26MSC") and fmx.is_mtt("C26MHD")
    assert not fmx.is_mtt("C26TSE") and not fmx.is_mtt("C26THG")


def test_read_rows_cp1258(tmp_path):
    p = tmp_path / "m.csv"
    p.write_bytes((CSV.replace("no_xml_content", "hóa đơn")).encode("cp1258"))
    rows = fmx.read_rows(p)
    assert [fmx.to_invoice(r) for r in rows] == [
        {"nbmst": "0318793907", "khmshdon": 1, "khhdon": "C26MSC", "shdon": 48135, "year": 2026, "month": 8},
        {"nbmst": "0318793907", "khmshdon": 1, "khhdon": "C26TSE", "shdon": 512, "year": 2026, "month": 9},
    ]


def test_month_from_invoice_date_when_columns_missing():
    inv = fmx.to_invoice({"company_tax_code": "318793907", "form_no": "1", "serial_no": "C26MSC",
                          "invoice_no": "7", "invoice_date": "10/3/2026"})
    assert (inv["year"], inv["month"]) == (2026, 10)


def test_select_by_mst_year_month():
    invs = [fmx.to_invoice(r) for r in fmx.csv.DictReader(CSV.splitlines())]
    other = dict(invs[0], nbmst="0101243150")
    invs.append(other)
    assert fmx.select(invs, None, None, "8") == [invs[0], other]
    assert fmx.select(invs, "318793907", None, "2026-08") == [invs[0]]
    assert fmx.select(invs, None, 2025, None) == []
    assert fmx.select(invs, "0318793907", 2026, None) == invs[:2]


def test_file_path_by_mst_year_month(tmp_path):
    inv = {"nbmst": "0318793907", "year": 2026, "month": 8, "khmshdon": 1, "khhdon": "C26MSC", "shdon": 5}
    assert fmx.file_path(tmp_path, inv) == tmp_path / "0318793907" / "2026" / "08" / "1_C26MSC_5.xml"


def test_main_downloads_missing_only(tmp_path, monkeypatch):
    csv_path = tmp_path / "m.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    out = tmp_path / "xml"
    old = out / "0318793907" / "2026" / "09" / "1_C26TSE_512.xml"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"<old/>")                              # đã có → không tải lại

    calls = []

    class _Store:
        def __init__(self, dsn):
            pass

        def close(self):
            pass

    def fake_zip(tok, nbmst, khhdon, shdon, khmshdon, use_sco=False):
        calls.append((nbmst, khhdon, shdon, use_sco))
        return _zip(b"<HDon/>")

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(fmx, "InvoiceStore", _Store)
    monkeypatch.setattr(fmx.invoice_job, "_setup_logging", lambda: None)
    monkeypatch.setattr(fmx.invoice_job, "_active_companies",
                        lambda store: [{"company_mst": "0318793907", "gdt_username": "0318793907"}])
    monkeypatch.setattr(fmx.invoice_job.TokenProvider, "get", lambda self: "tok")
    monkeypatch.setattr(fmx.api_client, "fetch_invoice_zip", fake_zip)
    monkeypatch.setattr(fmx, "PAUSE_SECONDS", 0)

    assert fmx.main(["--csv", str(csv_path), "--out", str(out)]) == 0
    assert calls == [("0318793907", "C26MSC", 48135, True)]
    assert (out / "0318793907" / "2026" / "08" / "1_C26MSC_48135.xml").read_bytes() == b"<HDon/>"
    assert old.read_bytes() == b"<old/>"
    assert "ok" in (out / fmx.RESULT_NAME).read_text(encoding="utf-8-sig")
