"""web/main_auto.py: lấy HĐ theo khoảng ngày → DB qua đúng luồng của job (offline, store giả).

Sai ở đây thì: chạy tay làm lệch mốc đồng bộ của tick (bỏ sót HĐ), chạy chồng với tick,
hoặc ghi vào nhầm công ty.

Run::

    pytest tests/test_main_auto.py -q
"""

from __future__ import annotations

import importlib
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "web"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

main_auto = importlib.import_module("main_auto")
invoice_job = main_auto.invoice_job


class _Store:
    def __init__(self, companies, locked=False):
        self.companies, self.locked, self.runs, self.closed = companies, locked, [], False
        self.company_runs = []

    def active_companies(self):
        return self.companies

    def try_lock(self):
        return not self.locked

    def unlock(self):
        pass

    def close(self):
        self.closed = True

    def start_run(self, cmd):
        self.runs.append([cmd, None, None])
        return 1

    def finish_run(self, run_id, status, stats):
        self.runs[-1][1:] = [status, stats]

    def begin_run_company(self, run_id, company_mst):
        self.company_runs.append(("begin", run_id, company_mst))

    def finish_run_company(self, run_id, company_mst, status, stats):
        self.company_runs.append(("finish", run_id, company_mst, status, stats))


CO = {"company_mst": "1201496252", "gdt_username": "1201496252-U002"}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    seen = {}

    def fake_sync(store, company, tokens, sub_types, windows_for, breaker, stats, move_cursor=True):
        seen.update(company=company["company_mst"], sub_types=sub_types, move_cursor=move_cursor,
                    windows=windows_for(sub_types[0]))
        stats["listed"] += 3
        stats["new_invoices"] += 2

    def fake_xml(store, company, tokens, breaker, run_id, limit, stats, out_dir):
        stats["xml_ok"] += 2

    monkeypatch.setattr(invoice_job, "sync_lists", fake_sync)
    monkeypatch.setattr(invoice_job, "process_xml", fake_xml)
    monkeypatch.setattr(invoice_job, "_setup_logging", lambda: None)
    monkeypatch.setattr(invoice_job, "output_dir", lambda: tmp_path)
    monkeypatch.setenv("DATABASE_URL", "x")
    return seen


def _run(monkeypatch, store, *argv):
    monkeypatch.setattr(invoice_job, "InvoiceStore", lambda dsn: store)
    return main_auto.main(list(argv))


def test_date_range_goes_to_db_without_moving_tick_cursor(monkeypatch, wired, tmp_path):
    store = _Store([CO])
    assert _run(monkeypatch, store, "--from", "2026-07-01", "--to", "2026-09-30") == 0
    assert not list(tmp_path.rglob("*.json"))          # không còn hoa_don.json / hoa_don_chitiet.json
    assert wired["company"] == "1201496252" and wired["move_cursor"] is False
    assert wired["windows"][0] == (date(2026, 7, 1), date(2026, 7, 30))           # ≤ 30 ngày/đoạn
    assert wired["windows"][-1][1] == date(2026, 9, 30)
    assert any(st.startswith("mua") for st in wired["sub_types"]) and any(st.startswith("ban") for st in wired["sub_types"])
    cmd, status, stats = store.runs[0]
    assert cmd == "manual" and status == "ok" and stats["stats"]["xml_ok"] == 2
    assert store.closed


def test_type_filter(monkeypatch, wired):
    _run(monkeypatch, _Store([CO]), "--from", "2026-09-01", "--to", "2026-09-30", "--type", "mua-all")
    assert wired["sub_types"] and all(st.startswith("mua") for st in wired["sub_types"])


def test_company_must_be_known(monkeypatch, wired):
    two = [CO, {"company_mst": "0100112233", "gdt_username": "0100112233"}]
    assert _run(monkeypatch, _Store(two), "--from", "2026-09-01") == 2              # >1 công ty, không chỉ rõ
    assert _run(monkeypatch, _Store([CO]), "--from", "2026-09-01", "--company", "0999999999") == 2
    assert "company" not in wired


def test_respects_running_job_lock(monkeypatch, wired):
    store = _Store([CO], locked=True)
    assert _run(monkeypatch, store, "--from", "2026-09-01", "--to", "2026-09-30") == 0
    assert store.runs == [] and "company" not in wired


def test_rejects_reversed_range(monkeypatch, wired):
    assert _run(monkeypatch, _Store([CO]), "--from", "2026-09-30", "--to", "2026-09-01") == 2
