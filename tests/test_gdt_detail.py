"""
Chi tiết HĐ từ API "Xem hóa đơn" của GDT khi Thuế không có XML (gdt_detail.py, 27/11/2026).
Offline: không gọi GDT, không cần DB.
"""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_client   # noqa: E402
import gdt_detail   # noqa: E402
from invoice_store import InvoiceStore   # noqa: E402


def _load_invoice_job():
    spec = importlib.util.spec_from_file_location("invoice_job_gdt_detail", ROOT / "web" / "invoice_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


invoice_job = _load_invoice_job()

# Rút gọn từ JSON GDT trả thật 27/11/2026
EVN = {  # bảng tổng hợp: 1 dòng / thuế suất, có đơn vị + số lượng
    "nbmst": "0100100417-024", "khhdon": "K26TPA", "shdon": 910007, "khmshdon": 1, "hthdon": 2,
    "mkhang": "PA22010217476", "tgtcthue": 1250612.0, "tgtthue": 100049.0, "tgtttbso": 1350661.0,
    "dvtte": "VND", "nbdchi": None, "tthai": 1,
    "hdhhdvu": [{"stt": 295, "sxep": 1, "ten": "Khác", "dvtinh": "kWh", "ltsuat": "8%", "tsuat": 0.08,
                 "sluong": 454.0, "thtien": 1250612.0, "tthue": 100049}],
}
VNA = {  # bảng tổng hợp nhiều thuế suất, không tên hàng
    "khhdon": "K26TKE", "hthdon": 2, "tgtttbso": 6500362.0,
    "hdhhdvu": [
        {"stt": 4967, "sxep": 3, "ltsuat": "8%", "tsuat": 0.08, "thtien": 833333.0, "tthue": 66667},
        {"stt": 4965, "sxep": 1, "ltsuat": "KKKNT", "tsuat": 0.0, "thtien": 234362.0, "tthue": 0},
        {"stt": 4966, "sxep": 2, "ltsuat": "8%", "tsuat": 0.08, "thtien": 4968000.0, "tthue": 398000},
    ],
}
WINMART = {  # HĐ thường (hthdon=0) mà export-xml trả 500: đủ dòng hàng
    "khhdon": "K26TTM", "hthdon": 0, "msttcgp": "0104128565", "nbdchi": "Tầng 2, TTTM Vincom PLAZA Kon Tum",
    "tgtttbso": 162486.0,
    "hdhhdvu": [{"stt": 1, "sxep": 1, "tchat": 1, "ten": "OPPA Mì ăn liền bò hầm 65g", "dvtinh": "GÓI",
                 "sluong": 59.0, "dgia": 2550.0, "thtien": 150450.0, "ltsuat": "8%", "tsuat": 0.08,
                 "mnnnghe": None}],
}


# ---------------------------------------------------------------- mapping

def test_summary_table_line_keeps_unit_quantity_and_tax():
    [it] = gdt_detail.items(EVN)
    assert it["stt"] == 1                                  # không phải 295 (số dòng trong cả bảng tổng hợp)
    assert (it["ten_hhdv"], it["don_vi_tinh"], it["so_luong"]) == ("Khác", "kWh", 454.0)
    assert (it["thanh_tien"], it["thue_suat"], it["tien_thue"], it["tong_tien"]) == (1250612.0, "8%", 100049.0,
                                                                                     1350661.0)
    assert gdt_detail.summary_table(EVN) and gdt_detail.hthdon(EVN) == 2


def test_summary_table_lines_follow_sxep_order():
    items = gdt_detail.items(VNA)
    assert [i["thue_suat"] for i in items] == ["KKKNT", "8%", "8%"]
    assert [i["stt"] for i in items] == [1, 2, 3]
    assert items[0]["ten_hhdv"] is None and items[0]["tong_tien"] == 234362.0


def test_full_invoice_keeps_line_items():
    [it] = gdt_detail.items(WINMART)
    assert (it["stt"], it["tinh_chat"], it["ten_hhdv"], it["so_luong"], it["don_gia"]) == \
           (1, 1, "OPPA Mì ăn liền bò hầm 65g", 59.0, 2550.0)
    assert it["tien_thue"] is None and it["tong_tien"] is None    # GDT không trả tthue cho dòng HĐ thường
    assert not gdt_detail.summary_table(WINMART)


@pytest.mark.parametrize("line, rate", [
    ({"ltsuat": "KCT"}, "KCT"),
    ({"tsuat": 0.1}, "10%"),
    ({"tsuat": 0.05}, "5%"),
    ({"tsuat": 0.0}, "0%"),
    ({}, None),
])
def test_rate_label(line, rate):
    assert gdt_detail._rate(line) == rate


def test_header_keeps_known_non_empty_fields_and_marks_time():
    h = gdt_detail.header(EVN)
    assert h["hthdon"] == 2 and h["mkhang"] == "PA22010217476" and h["tgtttbso"] == 1350661.0
    assert "nbdchi" not in h and "hdhhdvu" not in h and "tthai" not in h and "nbmst" not in h
    assert h["_gdt_detail_at"]


# ---------------------------------------------------------------- api_client

class _Resp:
    def __init__(self, status, body=None, text="x"):
        self.status_code, self._body, self.text = status, body, text
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("resp, kind", [
    (_Resp(401), "auth"), (_Resp(403), "auth"), (_Resp(429), "rate_limit"), (_Resp(503), "server"),
    (_Resp(500), "other"), (_Resp(200, None), "bad_content"), (_Resp(200, {"message": "x"}), "bad_content"),
])
def test_detail_api_classifies_errors(monkeypatch, resp, kind):
    monkeypatch.setattr(api_client._gdt_session, "get", lambda *a, **k: resp)
    with pytest.raises(api_client.GDTError) as ei:
        api_client.fetch_invoice_detail_json("tok", "0100100417-024", "K26TPA", 910007, 1)
    assert ei.value.kind == kind


def test_detail_api_ok(monkeypatch):
    seen = {}

    def fake_get(url, **k):
        seen.update(url=url, params=k["params"], auth=k["headers"]["Authorization"])
        return _Resp(200, EVN)
    monkeypatch.setattr(api_client._gdt_session, "get", fake_get)
    assert api_client.fetch_invoice_detail_json("tok", "0100100417-024", "K26TPA", 910007, 1) is EVN
    assert seen["url"].endswith("/query/invoices/detail") and seen["auth"] == "Bearer tok"
    assert seen["params"] == {"nbmst": "0100100417-024", "khhdon": "K26TPA", "shdon": 910007, "khmshdon": 1}


# ---------------------------------------------------------------- xml_batch

class _Tokens:
    mst = "3700860461"

    def get(self):
        return "tok"

    def refresh(self):
        return "tok"


class _Store:
    def __init__(self, save_result=True):
        self.saved, self.no_source, self.failed = [], [], []
        self.save_result = save_result

    def save_gdt_detail(self, invoice_id, header, items, hthdon):
        self.saved.append((invoice_id, header, items, hthdon))
        return self.save_result

    def mark_no_source(self, task, reason, run_id, kind="no_source"):
        self.no_source.append((kind, reason))

    def fail_task(self, task, kind, msg, run_id):
        self.failed.append((kind, msg))
        return "pending"


def _k_task(**kw):
    return {"id": 7, "task_id": 70, "attempts": 0, "company_mst": "3700860461", "nbmst": "0100100417-024",
            "khmshdon": 1, "khhdon": "K26TPA", "shdon": 910007, "sub_type": "mua-khma", "hthdon": None, **kw}


@pytest.fixture
def batch(monkeypatch, tmp_path):
    monkeypatch.setattr(invoice_job.time, "sleep", lambda s: None)

    def no_xml(*a, **k):
        raise api_client.GDTError("XML không có sẵn trên GDT (500)", kind="not_ready")
    monkeypatch.setattr(invoice_job, "_do_xml_task", no_xml)

    def run(store, task, detail):
        calls = []

        def fake_detail(tok, nbmst, khhdon, shdon, khmshdon):
            calls.append((nbmst, khhdon, shdon, khmshdon))
            if isinstance(detail, Exception):
                raise detail
            return detail
        monkeypatch.setattr(invoice_job.api_client, "fetch_invoice_detail_json", fake_detail)
        stats = Counter()
        invoice_job.xml_batch(store, {"company_mst": "3700860461"}, _Tokens(),
                              invoice_job.job_policy.CircuitBreaker(), 1, [task], stats, tmp_path)
        return calls, stats
    return run


def test_summary_invoice_saves_gdt_lines_and_stays_summary_only(batch):
    store = _Store()
    calls, stats = batch(store, _k_task(), EVN)
    assert calls == [("0100100417-024", "K26TPA", 910007, 1)]
    [(inv_id, header, items, hthdon)] = store.saved
    assert inv_id == 7 and hthdon == 2 and items[0]["don_vi_tinh"] == "kWh" and header["mkhang"]
    [(kind, reason)] = store.no_source
    assert kind == "summary_only" and "đã lưu tổng theo thuế suất" in reason
    assert stats["xml_gdt_detail"] == 1 and stats["xml_summary_only"] == 1 and not store.failed


def test_regular_invoice_without_xml_is_retried_not_parked(batch):
    store = _Store()
    _, stats = batch(store, _k_task(khhdon="K26TTM", shdon=2254), WINMART)
    assert store.saved and not store.no_source
    [(kind, msg)] = store.failed
    assert kind == "not_ready" and "hthdon=0" in msg


def test_detail_error_keeps_old_behaviour(batch):
    store = _Store()
    batch(store, _k_task(), api_client.GDTError("GDT quá tải (503)", kind="server"))
    assert not store.saved
    [(kind, reason)] = store.no_source
    assert kind == "summary_only" and "đã lưu" not in reason


def test_detail_not_called_for_coded_or_cash_register_or_known_hthdon(batch):
    for task in (_k_task(khhdon="C26TAA"), _k_task(sub_type="mua-mtt"), _k_task(hthdon=0)):
        calls, _ = batch(_Store(), task, EVN)
        assert calls == []


def test_detail_can_be_switched_off(batch, monkeypatch):
    monkeypatch.setenv("INVOICE_GDT_DETAIL", "0")
    store = _Store()
    calls, _ = batch(store, _k_task(), EVN)
    assert calls == [] and store.no_source[0][0] == "summary_only"


def test_save_failure_is_only_logged(batch):
    class _Boom(_Store):
        def save_gdt_detail(self, *a):
            raise RuntimeError('new row violates check constraint "invoice_details_nguon_check"')
    store = _Boom()
    batch(store, _k_task(), EVN)
    [(kind, reason)] = store.no_source
    assert kind == "summary_only" and "đã lưu" not in reason


# ---------------------------------------------------------------- lệnh gdt-detail

class _CmdStore(_Store):
    def __init__(self, rows):
        super().__init__()
        self.rows, self.requeued, self.runs = rows, [], []

    def gdt_detail_candidates(self, company, limit, redo=False):
        return self.rows

    def active_companies(self):
        return [{"company_mst": "3700860461"}]

    def start_run(self, command):
        self.runs.append(command)
        return 1

    def finish_run(self, run_id, status, stats):
        self.runs.append((status, stats))

    def requeue_tasks(self, ids):
        self.requeued += ids
        return len(ids)


def test_backfill_command(monkeypatch):
    monkeypatch.setattr(invoice_job.time, "sleep", lambda s: None)
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: _Tokens())
    answers = {910007: EVN, 2254: WINMART, 5: api_client.GDTError("GDT quá tải (503)", kind="server")}

    def fake_detail(tok, nbmst, khhdon, shdon, khmshdon):
        a = answers[shdon]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(invoice_job.api_client, "fetch_invoice_detail_json", fake_detail)
    rows = [_k_task(), _k_task(id=8, task_id=80, khhdon="K26TTM", shdon=2254), _k_task(id=9, task_id=90, shdon=5)]
    store = _CmdStore(rows)
    args = type("A", (), {"company": None, "limit": None, "redo": False})()
    assert invoice_job.cmd_gdt_detail(store, args) == 1            # có 1 HĐ lỗi
    assert store.requeued == [80]                                   # HĐ thường → tải lại XML
    assert [s[0] for s in store.saved] == [7, 8]
    status, stats = store.runs[-1]
    assert status == "partial" and stats == {"summary": 1, "full_requeued": 1, "not_saved": 0, "failed": 1,
                                             "skipped": 0}


# ---------------------------------------------------------------- store

class _Cur:
    def __init__(self, has_xml):
        self.has_xml, self.sql = has_xml, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return {"has_xml": self.has_xml}


class _Tx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _store_with(cur):
    st = InvoiceStore.__new__(InvoiceStore)
    st._tx = lambda: _Tx(cur)
    return st


def test_store_never_overwrites_xml_lines():
    cur = _Cur(has_xml=True)
    assert _store_with(cur).save_gdt_detail(7, {"_gdt_detail_at": "x"}, [{"thanh_tien": 1}], 2) is False
    assert len(cur.sql) == 1


def test_store_keeps_existing_list_values_and_tags_lines():
    cur = _Cur(has_xml=False)
    assert _store_with(cur).save_gdt_detail(7, {"_gdt_detail_at": "x"}, [{"thanh_tien": 1}], 2) is True
    update = cur.sql[1]
    assert "%s::jsonb || COALESCE(list_raw" in update and "hthdon = COALESCE(hthdon" in update
    assert any("DELETE FROM invoice_details" in s for s in cur.sql)
    assert "INSERT INTO invoice_details" in cur.sql[-1]
