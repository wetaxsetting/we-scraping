"""Worker WeInvoice (web/weinvoice_worker.py) — phần không cần DB: tháng, loại HĐ, mã nhập tay,
gom kết quả ZIP, vòng đời yêu cầu (DONE / CANCELLED / FAILED), chặn đường dẫn ngoài thư mục."""

import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import weinvoice_worker as ww  # noqa: E402


# --- tháng / loại HĐ ---------------------------------------------------------

def test_months_clip_future_and_current_month():
    out = ww.months_between("2026-10", "2026-12", date(2026, 11, 9))
    assert out == [(date(2026, 10, 1), date(2026, 10, 31), "2026-10"),
                   (date(2026, 11, 1), date(2026, 11, 9), "2026-11")]


def test_months_cross_year_and_february():
    out = ww.months_between("2023-12", "2024-02", date(2026, 11, 9))
    assert [o[2] for o in out] == ["2023-12", "2024-01", "2024-02"]
    assert out[2][1] == date(2024, 2, 29)


@pytest.mark.parametrize("a,b", [("2026-11", "2026-10"), ("2026-13", "2026-12"), ("202611", "2026-12"), (None, "2026-12")])
def test_months_reject_bad_input(a, b):
    with pytest.raises(ww.RequestFailed):
        ww.months_between(a, b, date(2026, 11, 9))


def test_months_limit():
    with pytest.raises(ww.RequestFailed):
        ww.months_between("2020-01", "2026-11", date(2026, 11, 9))


@pytest.mark.parametrize("direction,khhdon,expected", [
    ("mua", "C26TAA", "mua"), ("mua", "K26TAB", "mua-khma"), ("ban", "C26MAA", "ban-mtt"),
    ("ban", "K26MAA", "ban-mtt"), ("mua", "", "mua")])
def test_sub_type_of(direction, khhdon, expected):
    assert ww.sub_type_of(direction, khhdon) == expected


def test_tthai_from_related_invoice():
    assert ww._tthai_from_xml({}) == 1
    assert ww._tthai_from_xml({"thong_tin_chung": {"hoa_don_lien_quan": {"tinh_chat": 1}}}) == 2
    assert ww._tthai_from_xml({"thong_tin_chung": {"hoa_don_lien_quan": {"tinh_chat": 2}}}) == 3


# --- mã tra cứu người dùng nhập ---------------------------------------------

def test_manual_code_overrides_lookup_fields_only():
    task = {"tt_khac": {"Mã tra cứu": "SAI", "MTCuu": "SAI2", "Ghi chú": "x"}, "ma_tra_cuu": "SAI"}
    ww.Worker.apply_manual_code(task, "ABC123", "CTY01", None)
    assert task["ma_tra_cuu"] == "ABC123"
    assert task["tt_khac"]["Mã tra cứu"] == "ABC123"
    assert task["tt_khac"]["MTCuu"] == "ABC123"
    assert task["tt_khac"]["Ghi chú"] == "x"
    assert task["tt_khac"]["Mã công ty"] == "CTY01"


def test_manual_link_routes_to_adapter(monkeypatch):
    class Fake:
        source_id = "fake"

        def invoice_from_link(self, url, code, mail_text):
            assert "MST người bán: 0101243150" in mail_text
            return {"ma_tra_cuu": "FROMLINK", "nbmst": "0101243150"}

    monkeypatch.setattr(ww.pdf_providers, "adapter_for_host", lambda host: Fake() if host == "x.vn" else None)
    task = {"nbmst": "0101243150", "tt_khac": None}
    ww.Worker.apply_manual_code(task, None, None, "https://x.vn/tra?c=1")
    assert task["source_id"] == "fake" and task["ma_tra_cuu"] == "FROMLINK"
    assert task["lookup_url"] == "https://x.vn/tra?c=1"


# --- importer (không DB) ----------------------------------------------------

def _importer(managed, prefer=None, only=None):
    imp = object.__new__(ww.Importer)
    imp.managed, imp.prefer, imp.only = managed, prefer, only
    imp.batch_invoices = []
    return imp


def test_company_for_buyer_seller_foreign_and_prefer():
    key = {"nbmst": "0100000001", "nmmst": "0200000002"}
    assert _importer({"0200000002": None})._company_for(key) == ("0200000002", "mua")
    assert _importer({"0100000001": None})._company_for(key) == ("0100000001", "ban")
    assert _importer({"0300000003": None})._company_for(key) is None
    both = {"0100000001": None, "0200000002": None}
    assert _importer(both, prefer="0100000001")._company_for(key) == ("0100000001", "ban")
    assert _importer(both, only="0200000002")._company_for(key) == ("0200000002", "mua")


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_imports_xml_before_pdf_and_summarises():
    imp = _importer({})
    order = []
    imp.import_xml = lambda d, n: order.append(n) or ww.Outcome("NEW" if n == "a.xml" else "DUPLICATE")
    imp.import_pdf = lambda d, n: order.append(n) or ww.Outcome("MERGED")
    out = imp.import_zip(_zip({"x/b.pdf": b"%PDF", "a.xml": b"<x/>", "c.xml": b"<x/>"}), "lo.zip")
    assert order == ["a.xml", "c.xml", "b.pdf"]
    assert out.code == "NEW" and "mới 1" in out.message and "trùng 1" in out.message


def test_zip_single_member_keeps_its_outcome():
    imp = _importer({})
    imp.import_xml = lambda d, n: ww.Outcome("FOREIGN", "lạ", nbmst="0100000001")
    out = imp.import_zip(_zip({"a.xml": b"<x/>"}), "lo.zip")
    assert out.code == "FOREIGN" and out.nbmst == "0100000001"


def test_zip_broken_or_empty():
    imp = _importer({})
    assert imp.import_zip(b"not a zip", "x.zip").code == "ERROR"
    assert imp.import_zip(_zip({"readme.txt": b"x"}), "x.zip").code == "ERROR"


def test_import_file_rejects_unknown_extension():
    assert _importer({}).import_file(b"x", "a.docx").code == "ERROR"


def test_import_pdf_rejects_non_pdf():
    assert _importer({}).import_pdf(b"<html>", "a.pdf").code == "ERROR"


# --- lưu trữ ----------------------------------------------------------------

def test_stored_file_blocks_traversal(tmp_path, monkeypatch):
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner" / "a.xml").write_bytes(b"ok")
    monkeypatch.setenv("WEINVOICE_STORAGE_ROOT", str(tmp_path))
    assert ww.stored_file("owner/a.xml") == b"ok"
    with pytest.raises(ValueError):
        ww.stored_file("../outside.txt")


def test_stored_file_needs_root(monkeypatch):
    monkeypatch.delenv("WEINVOICE_STORAGE_ROOT", raising=False)
    with pytest.raises(ww.RequestFailed):
        ww.stored_file("a.xml")


# --- vòng đời yêu cầu -------------------------------------------------------

class FakeRequests:
    s = "weinvoice"

    def __init__(self, row, cancel_after=None):
        self.row, self.cancel_after, self.reports, self.finished = row, cancel_after, [], None

    def claim(self, types):
        row, self.row = self.row, None
        return row

    def report(self, rqst_seq, step, progress):
        self.reports.append((step, progress))
        return self.cancel_after is not None and len(self.reports) >= self.cancel_after

    def finish(self, rqst_seq, status, result=None, error=None, job_run_id=None):
        self.finished = (status, result, error, job_run_id)


def _worker(row, handler, cancel_after=None):
    w = object.__new__(ww.Worker)
    w.reqs = FakeRequests(row, cancel_after)
    w.handlers = {row["rqst_type"]: handler} if row else {}
    return w


ROW = {"rqst_seq": 7, "owner_id": "u@x", "cmpn_mst": "1201496252", "rqst_type": "SYNC", "rqst_prmt": {}}


def test_run_one_done():
    w = _worker(dict(ROW), lambda job: ("DONE", {"new": 1}))
    assert w.run_one() is True
    assert w.reqs.finished[0] == "DONE" and w.reqs.finished[1] == {"new": 1}


def test_run_one_empty_queue():
    w = _worker(None, None)
    assert w.run_one() is False


def test_run_one_cancelled_at_checkpoint():
    def handler(job):
        job.report("list", month="2026-10")
        job.report("xml", xml_done=1)
        return "DONE", {}
    w = _worker(dict(ROW), handler, cancel_after=2)
    w.run_one()
    status, result, error, _ = w.reqs.finished
    assert status == "CANCELLED" and result["month"] == "2026-10"


@pytest.mark.parametrize("exc,expected", [
    (ww.RequestFailed("MST chưa kết nối"), "MST chưa kết nối"),
    (ww.invoice_job.LoginError("sai mật khẩu"), "Không đăng nhập được Cục Thuế: sai mật khẩu"),
    (KeyError("x"), "Lỗi engine: KeyError"),
])
def test_run_one_failures_become_failed(exc, expected):
    def handler(job):
        raise exc
    w = _worker(dict(ROW), handler)
    w.run_one()
    status, _, error, _ = w.reqs.finished
    assert status == "FAILED" and error.startswith(expected)


def test_job_params_tolerate_non_dict():
    job = ww.Job(FakeRequests(None), {**ROW, "rqst_prmt": None})
    assert job.params == {}
    assert ww.Worker.params_list(ww.Job(FakeRequests(None), {**ROW, "rqst_prmt": {"invoice_ids": "1"}}),
                                 "invoice_ids") == []
