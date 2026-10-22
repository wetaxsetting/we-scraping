"""Job lấy hóa đơn: các quyết định không cần DB/mạng.

Những thứ ở đây sai thì job vẫn chạy "êm" nhưng âm thầm làm sai: bỏ sót hóa đơn
(cửa sổ đồng bộ hở), gọi GDT dồn dập (ngắt sớm không mở), xếp HĐ sai tháng (múi giờ),
hoặc chờ sai thời gian (phân loại lỗi). Nên kiểm chứ không giả định.

Run::

    pytest tests/test_invoice_job.py -q
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_client   # noqa: E402
import job_policy   # noqa: E402
import xml_parser   # noqa: E402


def _load_invoice_job():
    spec = importlib.util.spec_from_file_location("invoice_job", ROOT / "web" / "invoice_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


invoice_job = _load_invoice_job()

TODAY = date(2026, 10, 13)


# ---------------------------------------------------------------------------
# Cửa sổ đồng bộ: không hở, không quá 30 ngày/lần
# ---------------------------------------------------------------------------

def _assert_contiguous(windows, start, end):
    assert windows[0][0] == start and windows[-1][1] == end
    for (s1, e1), (s2, _) in zip(windows, windows[1:]):
        assert s2 == e1 + timedelta(days=1)
    for s, e in windows:
        assert (e - s).days + 1 <= job_policy.MAX_SPAN_DAYS


def test_sync_windows_first_run_is_rolling_30_days():
    w = job_policy.sync_windows(None, TODAY)
    assert w == [(TODAY - timedelta(days=29), TODAY)]


def test_sync_windows_recent_cursor_still_rescans_30_days():
    # HĐ lên GDT muộn: dù hôm qua đã đồng bộ, vẫn quét lại 30 ngày
    w = job_policy.sync_windows(TODAY - timedelta(days=1), TODAY)
    assert w == [(TODAY - timedelta(days=29), TODAY)]


def test_sync_windows_fills_gap_after_long_pause():
    cursor = TODAY - timedelta(days=75)
    w = job_policy.sync_windows(cursor, TODAY)
    _assert_contiguous(w, cursor, TODAY)
    assert len(w) == 3


def test_month_bounds():
    assert job_policy.month_bounds(None, date(2026, 1, 5)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert job_policy.month_bounds("2026-02", TODAY) == (date(2026, 2, 1), date(2026, 2, 28))
    assert job_policy.month_bounds("2028-02", TODAY) == (date(2028, 2, 1), date(2028, 2, 29))
    first, last = job_policy.month_bounds("2026-09", TODAY)
    _assert_contiguous(job_policy.chunk_range(first, last), first, last)


# ---------------------------------------------------------------------------
# Thử lại + ngắt sớm
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 13, 6, 30, tzinfo=timezone.utc)


def test_next_retry_waits_by_kind():
    assert job_policy.next_retry("network", 1, NOW) == ("pending", NOW + timedelta(minutes=30))
    assert job_policy.next_retry("not_ready", 1, NOW) == ("pending", NOW + timedelta(days=1))
    assert job_policy.next_retry("unknown-kind", 1, NOW)[0] == "pending"


def test_next_retry_gives_up():
    assert job_policy.next_retry("parse", 1, NOW) == ("dead", None)
    _, max_attempts = job_policy.RETRY_POLICY["network"]
    assert job_policy.next_retry("network", max_attempts, NOW) == ("dead", None)


def test_circuit_breaker_opens_on_consecutive_transient_errors():
    br = job_policy.CircuitBreaker(threshold=5)
    for _ in range(4):
        br.record(False, "network")
    assert not br.open
    br.record(False, "not_ready")        # lỗi riêng của HĐ: không phải GDT quá tải
    assert not br.open
    br.record(False, "server")
    assert br.open


def test_circuit_breaker_resets_on_success():
    br = job_policy.CircuitBreaker(threshold=3)
    br.record(False, "network")
    br.record(False, "network")
    br.record(True)
    br.record(False, "network")
    assert not br.open


# ---------------------------------------------------------------------------
# JWT + chuẩn hóa dòng danh sách
# ---------------------------------------------------------------------------

def _fake_jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(payload)}.sig"


def test_jwt_expiry():
    exp = int(datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    assert api_client.jwt_expiry(_fake_jwt({"exp": exp})) == datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert api_client.jwt_expiry(_fake_jwt({"sub": "x"})) is None
    assert api_client.jwt_expiry("not-a-jwt") is None


def test_normalize_list_row_uses_vietnam_date():
    row = {"nbmst": "0100112233", "khhdon": "C26TAA", "shdon": 123, "khmshdon": 1,
           "tdlap": "2026-10-31T17:00:00Z", "tgtcthue": 1000000.0, "tgtthue": "80000",
           "tgtttbso": 1080000, "tthai": 1}
    n = job_policy.normalize_list_row(row, "1201496252", "mua-khma")
    assert n["ngay_lap"] == date(2026, 11, 1)          # UTC 17h ngày 31/10 = 0h 1/11 giờ VN
    assert n["direction"] == "mua" and n["loai_hd"] == "Không có mã"
    assert n["tien_chua_thue"] == Decimal("1000000.0") and n["tien_thue"] == Decimal("80000")
    assert job_policy.normalize_list_row({"khhdon": "X", "shdon": 1}, "1", "mua") is None


def test_summary_only_detection():
    base = {"nbmst": "0100686209", "khhdon": "K26TAA", "shdon": 1, "khmshdon": 1,
            "tdlap": "2026-10-11T17:00:00Z"}
    n = job_policy.normalize_list_row({**base, "hthdon": 2}, "1201496252", "mua-khma")
    assert n["hthdon"] == 2 and job_policy.summary_only(n)
    for h in (0, 1, 5, None):                                 # 0/1/5: Thuế có XML (54/54)
        assert not job_policy.summary_only(job_policy.normalize_list_row(
            {**base, "hthdon": h}, "1201496252", "mua"))


def test_expand_invoice_types():
    assert api_client.expand_invoice_types(["mua-all"]) == ["mua", "mua-khma", "mua-mtt"]
    assert api_client.expand_invoice_types(["ban", "ban-all"]) == ["ban", "ban-khma", "ban-mtt"]
    with pytest.raises(ValueError):
        api_client.expand_invoice_types(["khong-co"])


# ---------------------------------------------------------------------------
# Phân loại lỗi tải XML từ GDT
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, content=b"", body=None):
        self.status_code = status
        self.content = content
        self.ok = 200 <= status < 300
        self.text = content.decode(errors="ignore")
        self._body = body or {}

    def json(self):
        return self._body


def _zip_with(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("resp, kind", [
    (_Resp(401), "auth"),
    (_Resp(403), "auth"),
    (_Resp(429), "rate_limit"),
    (_Resp(500), "not_ready"),
    (_Resp(503), "server"),
    (_Resp(404), "other"),
    (_Resp(200, b'{"message":"loi"}', {"message": "loi"}), "bad_content"),
])
def test_download_zip_classifies_errors(monkeypatch, resp, kind):
    monkeypatch.setattr(api_client._gdt_session, "get", lambda *a, **k: resp)
    with pytest.raises(api_client.GDTError) as ei:
        api_client.download_invoice_zip("tok", {"nbmst": "1", "khhdon": "C", "shdon": 1})
    assert ei.value.kind == kind


def test_download_zip_timeout_is_network(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr(api_client._gdt_session, "get", boom)
    with pytest.raises(api_client.GDTError) as ei:
        api_client.download_invoice_zip("tok", {"nbmst": "1", "khhdon": "C", "shdon": 1})
    assert ei.value.kind == "network" and ei.value.retryable


def test_download_zip_ok_and_sco_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, **k):
        seen["url"] = url
        return _Resp(200, _zip_with("a.xml", b"<HDon/>"))
    monkeypatch.setattr(api_client._gdt_session, "get", fake_get)
    data = api_client.download_invoice_zip("tok", {"nbmst": "1", "khhdon": "C", "shdon": 1,
                                                   "_sub_type": "mua-mtt"})
    assert data.startswith(b"PK") and "/sco-query/" in seen["url"]


# ---------------------------------------------------------------------------
# XML: thông tin tra cứu cho bước PDF
# ---------------------------------------------------------------------------

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HDon><DLHDon>
  <TTChung><KHMSHDon>1</KHMSHDon><KHHDon>C26TBV</KHHDon><SHDon>238805</SHDon>
    <MSTTCGP>0102331762</MSTTCGP>
    <TTKhac><TTin><TTruong>Fkey</TTruong><DLieu>FK-1</DLieu></TTin>
            <TTin><TTruong>MTCuu</TTruong><DLieu>BGT-F9WGNAKDCVYI</DLieu></TTin></TTKhac>
  </TTChung>
  <NDHDon>
    <NBan><Ten>NCC A</Ten><MST>0100112233</MST></NBan>
    <NMua><Ten>Cty B</Ten><MST>1201496252</MST></NMua>
    <DSHHDVu><HHDVu><STT>1</STT><THHDVu>Ve</THHDVu><ThTien>100</ThTien>
      <TTKhac><TTin><TTruong>VATAmount</TTruong><DLieu>8</DLieu></TTin>
              <TTin><TTruong>MTCuu</TTruong><DLieu>DONG-HANG</DLieu></TTin></TTKhac>
    </HHDVu></DSHHDVu>
  </NDHDon>
</DLHDon>
<DSCKS><Signature xmlns="http://www.w3.org/2000/09/xmldsig#"/></DSCKS>
<!-- tra cuu tai https://tracuu.example-hddt.vn/search -->
</HDon>"""


def test_parser_extracts_invoice_level_lookup_info():
    d = xml_parser.parse_xml_bytes(SAMPLE_XML.encode())
    assert d["thong_tin_chung"]["mst_tcgp"] == "0102331762"
    assert d["tt_khac"] == {"Fkey": "FK-1", "MTCuu": "BGT-F9WGNAKDCVYI"}   # bỏ TTKhac của dòng hàng
    assert d["ma_tra_cuu"] == "BGT-F9WGNAKDCVYI"                           # MTCuu ưu tiên hơn Fkey
    assert d["lookup_url"] == "https://tracuu.example-hddt.vn/search"     # bỏ qua w3.org


def test_extract_xml_rejects_bad_zip():
    with pytest.raises(ValueError):
        xml_parser.extract_xml(b"not a zip")
    with pytest.raises(ValueError):
        xml_parser.extract_xml(_zip_with("readme.txt", b"x"))
    assert xml_parser.extract_xml(_zip_with("hd.xml", b"<HDon/>")) == b"<HDon/>"


# ---------------------------------------------------------------------------
# Ghi file + đường dẫn
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_tmp(tmp_path):
    target = tmp_path / "a" / "b" / "x.xml"
    invoice_job.atomic_write(target, b"one")
    invoice_job.atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["x.xml"]


def test_invoice_path_layout(tmp_path):
    task = {"ngay_lap": date(2026, 11, 1), "nbmst": "0100112233",
            "khmshdon": 1, "khhdon": "C26/TAA", "shdon": 123}
    p = invoice_job.invoice_path(tmp_path, "pdf", task, "1201496252", "pdf")
    assert p.relative_to(tmp_path).as_posix() == "pdf/1201496252/2026/11/0100112233/1_C26_TAA_123.pdf"


class _FakeStore:
    def __init__(self):
        self.completed = []

    def complete_xml(self, task, run_id, xml_path, sha, parsed, partner):
        self.completed.append(xml_path)


class _FakeTokens:
    mst = "1201496252"

    def get(self):
        return "tok"

    def refresh(self):
        return "tok"


_TASK = {"nbmst": "0100112233", "khhdon": "C26TAA", "shdon": 123, "khmshdon": 1,
         "sub_type": "mua", "direction": "mua", "ngay_lap": date(2026, 11, 1)}
_COMPANY = {"company_mst": "1201496252"}


def test_xml_task_saves_original_zip_next_to_xml(tmp_path, monkeypatch):
    zip_bytes = _zip_with("hd.xml", SAMPLE_XML.encode())
    monkeypatch.setattr(invoice_job.api_client, "download_invoice_zip", lambda tok, row: zip_bytes)
    monkeypatch.delenv("INVOICE_SAVE_ZIP", raising=False)
    store = _FakeStore()
    invoice_job._do_xml_task(store, _COMPANY, _FakeTokens(), _TASK, None, tmp_path)

    rel = "1201496252/2026/11/0100112233/1_C26TAA_123"
    assert (tmp_path / "temp" / f"{rel}.zip").read_bytes() == zip_bytes      # ZIP gốc, nguyên byte
    assert (tmp_path / "xml" / f"{rel}.xml").read_bytes() == SAMPLE_XML.encode()
    assert store.completed == [f"xml/{rel}.xml"]
    assert not list(tmp_path.rglob("*.json"))          # dữ liệu parse chỉ vào DB, không ra file JSON


def test_xml_log_line_and_total_mismatch_warning(caplog):
    caplog.set_level("INFO", logger="invoice_job")
    parsed = {"thong_tin_chung": {"ngay_lap": "2026-11-01", "mst_tcgp": "0102331762"},
              "nguoi_ban": {"mst": "0100112233", "ten": "NCC A"}, "thanh_toan": {"tong_tien_tt": 1080000},
              "hang_hoa": [{}, {}], "ma_tra_cuu": "BGT-1"}
    invoice_job._log_xml("1201496252", dict(_TASK, tong_tien=1000000), parsed, "xml/a.xml")
    assert ("XML OK 1_C26TAA_123 | bán 0100112233 NCC A | ngày 2026-11-01 | tổng 1,080,000 | 2 dòng hàng"
            " | MSTTCGP 0102331762 | mã tra cứu BGT-1 | xml/a.xml") in caplog.text
    assert "lệch tổng tiền: danh sách GDT 1,000,000, XML 1,080,000" in caplog.text


def test_xml_task_keeps_zip_when_xml_is_broken(tmp_path, monkeypatch):
    zip_bytes = _zip_with("hd.xml", b"<HDon><chua-dong-the>")
    monkeypatch.setattr(invoice_job.api_client, "download_invoice_zip", lambda tok, row: zip_bytes)
    monkeypatch.delenv("INVOICE_SAVE_ZIP", raising=False)
    with pytest.raises(invoice_job.TaskError) as ei:
        invoice_job._do_xml_task(_FakeStore(), _COMPANY, _FakeTokens(), _TASK, None, tmp_path)
    assert ei.value.kind == "parse"
    assert len(list((tmp_path / "temp").rglob("*.zip"))) == 1                # còn bản gốc để xem
    assert not (tmp_path / "xml").exists()


def test_xml_task_zip_saving_can_be_disabled(tmp_path, monkeypatch):
    zip_bytes = _zip_with("hd.xml", SAMPLE_XML.encode())
    monkeypatch.setattr(invoice_job.api_client, "download_invoice_zip", lambda tok, row: zip_bytes)
    monkeypatch.setenv("INVOICE_SAVE_ZIP", "0")
    invoice_job._do_xml_task(_FakeStore(), _COMPANY, _FakeTokens(), _TASK, None, tmp_path)
    assert not (tmp_path / "temp").exists()
    assert len(list((tmp_path / "xml").rglob("*.xml"))) == 1


def test_batch_download_saves_zip_next_to_xml(tmp_path, monkeypatch):
    """Đường main_auto.py --xml: ZIP gốc vào temp/, XML vào xml/, cùng cấu trúc thư mục."""
    good = _zip_with("hd.xml", SAMPLE_XML.encode())
    broken = _zip_with("readme.txt", b"khong co xml")
    zips = {123: good, 124: broken}
    monkeypatch.setattr(api_client, "_download_zip", lambda tok, nbmst, khhdon, shdon, khms, use_sco: zips[shdon])
    monkeypatch.setattr(api_client.time, "sleep", lambda s: None)
    rows = [{"nbmst": "0100112233", "khhdon": "C26TAA", "shdon": n, "khmshdon": 1,
             "tdlap": "2026-10-31T17:00:00Z"} for n in (123, 124)]
    res = api_client.download_invoices_xml_batch(
        "tok", rows, output_dir=str(tmp_path / "xml"), company_mst="1201496252",
        zip_dir=str(tmp_path / "temp"))

    rel = "1201496252/2026/11/0100112233"
    assert (tmp_path / "temp" / rel / "1_C26TAA_123.zip").read_bytes() == good
    assert (tmp_path / "xml" / rel / "1_C26TAA_123.xml").read_bytes() == SAMPLE_XML.encode()
    # ZIP không có XML: vẫn giữ ZIP gốc để xem, XML báo lỗi
    assert (tmp_path / "temp" / rel / "1_C26TAA_124.zip").read_bytes() == broken
    assert res[1]["filepath"] is None and res[1]["zip_path"] and "XML" in res[1]["error"]
    assert not list(tmp_path.rglob("*.tmp"))


def test_batch_download_without_zip_dir_saves_no_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(api_client, "_download_zip",
                        lambda *a, **k: _zip_with("hd.xml", SAMPLE_XML.encode()))
    rows = [{"nbmst": "0100112233", "khhdon": "C26TAA", "shdon": 1, "khmshdon": 1,
             "tdlap": "2026-10-11T17:00:00Z"}]
    res = api_client.download_invoices_xml_batch("tok", rows, output_dir=str(tmp_path / "xml"),
                                                 company_mst="1201496252")
    assert res[0]["filepath"] and res[0]["zip_path"] is None
    assert not list(tmp_path.rglob("*.zip"))


# ---------------------------------------------------------------------------
# Lệnh `pdf`: chỉ hàng đợi PDF, không được đăng nhập GDT
# ---------------------------------------------------------------------------

class _PdfStore:
    def __init__(self, locked=False):
        self.locked, self.calls, self.runs = locked, [], []
        self.company_runs = []

    def try_lock(self):
        self.calls.append("lock")
        return not self.locked

    def unlock(self):
        self.calls.append("unlock")

    def reclaim_stale(self):
        self.calls.append("reclaim")
        return 0

    def requeue(self, status, task_type=None, mst_tcgp=None, error_kind=None):
        self.calls.append(("requeue", status, task_type, mst_tcgp, error_kind))
        return 0

    def active_companies(self):
        return [{"company_mst": "1201496252"}, {"company_mst": "0100112233"}]

    def start_run(self, command):
        self.runs.append([command, None, None])
        return 7

    def finish_run(self, run_id, status, stats):
        self.runs[-1][1:] = [status, stats]

    def begin_run_company(self, run_id, company_mst):
        self.company_runs.append(("begin", run_id, company_mst))

    def finish_run_company(self, run_id, company_mst, status, stats):
        self.company_runs.append(("finish", run_id, company_mst, status, stats))

    def status_report(self):
        return {"counts": [{"task_type": "pdf", "status": "pending", "n": 3}]}


def _no_gdt(*a, **k):
    raise AssertionError("lệnh pdf không được gọi GDT")


@pytest.fixture
def pdf_cmd(monkeypatch, tmp_path):
    seen = []

    def fake_process_pdf(store, company, run_id, limit, stats, out_dir):
        seen.append((company["company_mst"], run_id, limit, out_dir))
        stats["pdf_ok"] += 1

    monkeypatch.setattr(invoice_job, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(invoice_job, "TokenProvider", _no_gdt)
    monkeypatch.setattr(invoice_job, "sync_lists", _no_gdt)
    monkeypatch.setattr(invoice_job, "process_xml", _no_gdt)
    monkeypatch.setattr(invoice_job.api_client, "authenticate_auto", _no_gdt)
    monkeypatch.setattr(invoice_job, "output_dir", lambda: tmp_path)
    return seen


def test_pdf_command_runs_only_pdf_queue_for_each_company(pdf_cmd, tmp_path):
    store = _PdfStore()
    args = invoice_job.argparse.Namespace(company=None, limit=20)
    assert invoice_job.cmd_pdf(store, args) == 0

    assert [s[0] for s in pdf_cmd] == ["1201496252", "0100112233"]
    assert all(s[1:] == (7, 20, tmp_path) for s in pdf_cmd)
    assert store.runs == [["pdf", "ok", {"companies": {"1201496252": {"pdf_ok": 1},
                                                       "0100112233": {"pdf_ok": 1}}}]]
    assert store.calls[0] == "lock" and store.calls[-1] == "unlock"
    assert "reclaim" in store.calls                          # thu hồi task kẹt như tick


def test_pdf_command_logs_one_row_per_company(pdf_cmd):
    """job_run_companies: mỗi MST 1 dòng, có mốc bắt đầu trước khi gọi site NCC."""
    store = _PdfStore()
    invoice_job.cmd_pdf(store, invoice_job.argparse.Namespace(company=None, limit=20))

    assert store.company_runs == [
        ("begin", 7, "1201496252"),
        ("finish", 7, "1201496252", "ok", {"pdf_ok": 1}),
        ("begin", 7, "0100112233"),
        ("finish", 7, "0100112233", "ok", {"pdf_ok": 1}),
    ]


def test_company_status_separates_no_login_from_some_failures():
    """'error' = công ty đó không làm được gì; HĐ lẻ lỗi chỉ là 'partial'."""
    assert invoice_job._company_status(invoice_job.Counter()) == "ok"
    assert invoice_job._company_status(invoice_job.Counter({"pdf_ok": 3})) == "ok"
    assert invoice_job._company_status(invoice_job.Counter({"xml_ok": 2, "pdf_failed": 1})) == "partial"
    assert invoice_job._company_status(invoice_job.Counter({"sync_errors": 1})) == "partial"
    assert invoice_job._company_status(invoice_job.Counter({"login_error": "sai mật khẩu"})) == "error"

    breaker = job_policy.CircuitBreaker(threshold=2)
    for _ in range(2):
        breaker.record(False, "network")
    assert breaker.open
    assert invoice_job._company_status(invoice_job.Counter({"xml_ok": 5}), breaker) == "partial"


def test_pdf_command_filters_company(pdf_cmd):
    args = invoice_job.argparse.Namespace(company="0100112233", limit=5)
    invoice_job.cmd_pdf(_PdfStore(), args)
    assert [s[0] for s in pdf_cmd] == ["0100112233"]


def test_pdf_command_respects_running_tick(pdf_cmd):
    store = _PdfStore(locked=True)
    assert invoice_job.cmd_pdf(store, invoice_job.argparse.Namespace(company=None, limit=5)) == 0
    assert pdf_cmd == [] and store.runs == [] and "unlock" not in store.calls


def test_pdf_command_requeues_providers_that_got_an_adapter(pdf_cmd, monkeypatch):
    monkeypatch.setattr(invoice_job.pdf_providers, "supported_mst_tcgp", lambda: ["0101243150"])
    store = _PdfStore()
    invoice_job.cmd_pdf(store, invoice_job.argparse.Namespace(company=None, limit=5))
    assert ("requeue", "no_source", "pdf", "0101243150", "no_source") in store.calls


def test_pdf_command_cli_defaults(monkeypatch):
    seen = {}

    class _Store:                      # main() không được mở DB thật trong test
        def __init__(self, dsn):
            pass

        def close(self):
            pass

    def fake_cmd(store, args):
        seen.update(vars(args))
        return 0

    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(invoice_job, "_setup_logging", lambda: None)
    monkeypatch.setattr(invoice_job, "InvoiceStore", _Store)
    monkeypatch.setattr(invoice_job, "cmd_pdf", fake_cmd)
    assert invoice_job.main(["pdf"]) == 0
    assert seen["company"] is None and seen["limit"] == invoice_job.DEFAULT_PDF_LIMIT
    invoice_job.main(["pdf", "--company", "1201496252", "--limit", "3"])
    assert seen["company"] == "1201496252" and seen["limit"] == 3


class _XmlStore(_PdfStore):
    def last_ok_run(self, command, on_day):
        return True                                   # không kích hoạt sweep ngày 5/15 trong test

    def get_cursor(self, mst, st):
        return None


def test_xml_command_runs_only_gdt_part(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())
    monkeypatch.setattr(invoice_job, "sync_lists",
                        lambda store, company, *a, **k: seen.append(("sync", company["company_mst"])))
    monkeypatch.setattr(invoice_job, "process_xml",
                        lambda store, company, tokens, breaker, run_id, limit, stats, out_dir:
                        seen.append(("xml", company["company_mst"], limit)))
    monkeypatch.setattr(invoice_job, "process_pdf", _no_gdt)          # bị gọi = sai
    monkeypatch.setattr(invoice_job, "run_mail_step", _no_gdt)
    monkeypatch.setattr(invoice_job, "output_dir", lambda: tmp_path)
    store = _XmlStore()
    assert invoice_job.cmd_xml(store, invoice_job.argparse.Namespace(company=None, limit=7)) == 0
    assert seen == [("sync", "1201496252"), ("xml", "1201496252", 7),
                    ("sync", "0100112233"), ("xml", "0100112233", 7)]
    assert store.runs[0][0] == "xml" and store.runs[0][1] == "ok"
    assert store.calls[0] == "lock" and store.calls[-1] == "unlock"


def test_xml_command_respects_running_job(monkeypatch):
    monkeypatch.setattr(invoice_job, "sync_lists", _no_gdt)
    store = _XmlStore(locked=True)
    assert invoice_job.cmd_xml(store, invoice_job.argparse.Namespace(company=None, limit=7)) == 0
    assert store.runs == []


class _QueueStore:
    """Hàng đợi PDF giả: claim_tasks lấy dần từ danh sách pending."""

    def __init__(self, tasks):
        self.pending = list(tasks)
        self.no_source, self.done, self.released = [], [], []

    def claim_tasks(self, task_type, mst, n):
        batch, self.pending = self.pending[:n], self.pending[n:]
        return batch

    def mark_no_source(self, task, reason, run_id, kind="no_source"):
        self.no_source.append(task["task_id"])

    def complete_pdf(self, task, run_id, path, sha, source_id):
        self.done.append(task["task_id"])

    def release_tasks(self, ids):
        self.released.extend(ids)


def test_pdf_limit_counts_only_real_fetches(tmp_path, monkeypatch):
    """HĐ của NCC chưa có adapter không gọi mạng → không được ăn mất --limit."""
    import pdf_providers

    class _Ad(pdf_providers.PdfAdapter):
        source_id, mst_tcgp = "fake_ncc", ("0109999999",)

        def fetch_pdf(self, invoice):
            return b"%PDF-1.4\n" + b"x" * 2000

    monkeypatch.setitem(pdf_providers._REGISTRY, "fake_ncc", _Ad())
    base = dict(_TASK, attempts=0, source_id=None)
    tasks = ([dict(base, id=i, task_id=i, shdon=i, mst_tcgp="0109888888") for i in range(1, 6)]
             + [dict(base, id=i, task_id=i, shdon=i, mst_tcgp="0109999999") for i in range(6, 9)])
    store, stats = _QueueStore(tasks), invoice_job.Counter()
    invoice_job.process_pdf(store, _COMPANY, 1, 2, stats, tmp_path)

    assert store.no_source == [1, 2, 3, 4, 5]
    assert store.done == [6, 7]
    assert 8 in store.released or 8 in [t["task_id"] for t in store.pending]   # để lượt sau
    assert stats["pdf_ok"] == 2 and stats["pdf_no_source"] == 5


_GUID = "70611f78-501c-4fce-a2dc-b31dd272f873"


def test_parser_reads_vnpt_fkey_outside_dlhdon():
    """VNPT đặt <Fkey> ngay dưới <HDon>; TTKhac không có mã → ma_tra_cuu lấy Fkey đó."""
    no_code = SAMPLE_XML.replace("<TTin><TTruong>Fkey</TTruong><DLieu>FK-1</DLieu></TTin>", "") \
                        .replace("<TTin><TTruong>MTCuu</TTruong><DLieu>BGT-F9WGNAKDCVYI</DLieu></TTin>", "")
    vnpt = no_code.replace("</DLHDon>", "</DLHDon><Fkey>HCM10013369202609</Fkey>")
    assert xml_parser.parse_xml_bytes(vnpt.encode())["ma_tra_cuu"] == "HCM10013369202609"
    assert xml_parser.parse_xml_bytes(SAMPLE_XML.encode())["ma_tra_cuu"] == "BGT-F9WGNAKDCVYI"  # TTKhac ưu tiên


def test_parser_exposes_dlhdon_id():
    """BKAV tra cứu bằng DLHDon/@Id — parser phải đưa ra."""
    with_id = SAMPLE_XML.replace("<DLHDon>", f'<DLHDon Id="{_GUID}">')
    assert xml_parser.parse_xml_bytes(with_id.encode())["dlhdon_id"] == _GUID
    assert xml_parser.parse_xml_bytes(SAMPLE_XML.encode())["dlhdon_id"] == ""


def test_backfill_reads_dlhdon_id_from_saved_xml(tmp_path):
    """Bản ghi DB parse trước khi có dlhdon_id (SQL trả NULL) → đọc lại file XML."""
    xml = tmp_path / "xml" / "a.xml"
    xml.parent.mkdir()
    xml.write_text(SAMPLE_XML.replace("<DLHDon>", f'<DLHDon Id="{_GUID}">'), encoding="utf-8")
    task = {"dlhdon_id": None, "xml_path": "xml/a.xml"}
    invoice_job._backfill_from_xml(task, tmp_path)
    assert task["dlhdon_id"] == _GUID

    task = {"dlhdon_id": "", "xml_path": "xml/khong-co.xml"}    # đã parse mới, không có Id → để yên
    invoice_job._backfill_from_xml(task, tmp_path)
    assert task["dlhdon_id"] == ""
    task = {"dlhdon_id": None, "xml_path": "xml/khong-co.xml"}  # file mất → không làm hỏng lượt chạy
    invoice_job._backfill_from_xml(task, tmp_path)
    assert task["dlhdon_id"] is None


def test_classify():
    assert invoice_job.classify(api_client.GDTError("x", "not_ready")) == "not_ready"
    assert invoice_job.classify(requests.exceptions.ConnectionError()) == "network"
    assert invoice_job.classify(RuntimeError("Rate limit 429 sau 4 lần thử")) == "rate_limit"
    assert invoice_job.classify(ValueError("x")) == "other"
