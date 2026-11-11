"""Nạp lô (IMPORT): HĐ đã có XML/PDF trong DB nhưng máy này không có file (engine máy khác tải về)
→ ghi file người dùng nạp vào đúng đường dẫn đã lưu, báo MERGED; còn file thì vẫn DUPLICATE."""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import weinvoice_worker as ww  # noqa: E402

XML_REL = "xml/1201496252/2026/11/1201496252/1_C26TWE_3.xml"
PDF_REL = "pdf/1201496252/2026/11/1201496252/1_C26TWE_4.pdf"


class _Cur:
    def __init__(self, found):
        self.found, self.sql = found, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return self.found


class _Store:
    def __init__(self, found):
        self.cur = _Cur(found)

    @contextlib.contextmanager
    def _tx(self):
        yield self.cur


def _importer(tmp_path, store):
    imp = object.__new__(ww.Importer)
    imp.store, imp.out_dir, imp.run_id = store, tmp_path, None
    imp.managed, imp.prefer, imp.only, imp.label = {"1201496252": None}, None, None, "upload"
    imp.batch_invoices = []
    return imp


def _xml_import(tmp_path, monkeypatch, found):
    parsed = {"nguoi_ban": {"ten": "Webcash"}, "nguoi_mua": {"ten": "HANSUNGBOLT"},
              "thanh_toan": {"tong_tien_tt": 5500000}, "thong_tin_chung": {"ngay_lap": "2026-11-02"}}
    key = {"nbmst": "1201496252", "nmmst": "3603379922", "khmshdon": 1, "khhdon": "C26TWE", "shdon": 3}
    monkeypatch.setattr(ww.xml_parser, "parse_xml_bytes", lambda d: parsed)
    monkeypatch.setattr(ww.mail_ingest, "_xml_key", lambda p: key)
    monkeypatch.setattr(ww, "ensure_company", lambda *a: None)
    monkeypatch.setattr(ww, "_invoice", lambda store, i: None)
    saved = []
    monkeypatch.setattr(ww.InvoiceStore, "_save_xml", staticmethod(lambda *a: saved.append(a)))
    store = _Store(found)
    out = _importer(tmp_path, store).import_xml(b"<HDon>new</HDon>", "1C26TWE_00000003.xml")
    return out, saved


def test_xml_row_without_file_here_is_restored_not_reparsed(tmp_path, monkeypatch):
    out, saved = _xml_import(tmp_path, monkeypatch, {"id": 10434, "has_xml": True, "xml_path": XML_REL})
    assert (out.code, out.message) == ("MERGED", "Bổ sung file XML (máy chủ chưa có file)")
    assert (tmp_path / XML_REL).read_bytes() == b"<HDon>new</HDon>"
    assert saved == []                       # dữ liệu HĐ giữ nguyên, không parse lại


def test_xml_row_with_file_here_stays_duplicate(tmp_path, monkeypatch):
    (tmp_path / XML_REL).parent.mkdir(parents=True)
    (tmp_path / XML_REL).write_bytes(b"<HDon>gdt</HDon>")
    out, saved = _xml_import(tmp_path, monkeypatch, {"id": 10434, "has_xml": True, "xml_path": XML_REL})
    assert out.code == "DUPLICATE"
    assert (tmp_path / XML_REL).read_bytes() == b"<HDon>gdt</HDon>"
    assert saved == []


def _pdf_import(tmp_path, monkeypatch, inv):
    imp = _importer(tmp_path, _Store(None))

    class _Mi:
        def _match_pdf_text(self, text):
            return inv
    imp.mi = _Mi()
    monkeypatch.setattr(ww.mail_ingest, "_pdf_text", lambda d: "C26TWE 4")
    monkeypatch.setattr(ww, "_invoice", lambda store, i: inv)
    return imp.import_pdf(b"%PDF-1.4 user", "1C26TWE_00000004.pdf")


INV = {"id": 10435, "company_mst": "1201496252", "nbmst": "1201496252", "nmmst": None, "tong_tien": 1,
       "has_pdf": True, "pdf_path": PDF_REL}


def test_pdf_row_without_file_here_is_restored(tmp_path, monkeypatch):
    out = _pdf_import(tmp_path, monkeypatch, INV)
    assert (out.code, out.message) == ("MERGED", "Bổ sung file PDF (máy chủ chưa có file)")
    assert (tmp_path / PDF_REL).read_bytes() == b"%PDF-1.4 user"


def test_pdf_row_with_file_here_stays_duplicate(tmp_path, monkeypatch):
    (tmp_path / PDF_REL).parent.mkdir(parents=True)
    (tmp_path / PDF_REL).write_bytes(b"%PDF-1.4 ncc")
    out = _pdf_import(tmp_path, monkeypatch, INV)
    assert out.code == "DUPLICATE"
    assert (tmp_path / PDF_REL).read_bytes() == b"%PDF-1.4 ncc"
