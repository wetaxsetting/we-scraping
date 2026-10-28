"""Mô hình hàng đợi: sync-lists sinh việc, worker rút việc xoay vòng qua các công ty.

Những thứ ở đây sai thì worker vẫn "chạy" nhưng hỏng theo kiểu khó thấy: công ty nhỏ
chờ công ty lớn cả buổi, nhiều worker cùng đăng nhập GDT đá phiên nhau, task bị kẹt
'running' khi Ctrl+C, hoặc quay vòng vô ích trên một site NCC đang sập.

Run::

    pytest tests/test_invoice_worker.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import job_policy   # noqa: E402


def _load_invoice_job():
    spec = importlib.util.spec_from_file_location("invoice_job", ROOT / "web" / "invoice_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


invoice_job = _load_invoice_job()

A, B = "1201496252", "0100112233"


def _task(mst, tid):
    return {"company_mst": mst, "task_id": tid, "attempts": 0, "nbmst": "0312650437",
            "khmshdon": 1, "khhdon": "C26TAA", "shdon": tid}


class _WorkerStore:
    """Store giả: trả các mẻ đã định sẵn, ghi lại mọi lời gọi."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.claims, self.released, self.runs, self.company_runs = [], [], [], []

    def reclaim_stale(self, minutes=60):
        return 0

    def requeue(self, *a, **k):
        return 0

    def no_source_without_tcgp(self):
        return []

    def requeue_tasks(self, ids):
        return 0

    def claim_round_robin(self, task_type, per_company, limit, exclude_msts=()):
        self.claims.append((task_type, per_company, limit, sorted(exclude_msts)))
        return self.batches.pop(0) if self.batches else []

    def active_companies(self):
        return [{"company_mst": A, "gdt_username": A}, {"company_mst": B, "gdt_username": B}]

    def release_tasks(self, ids, delay_minutes=0):
        if ids:
            self.released.append((list(ids), delay_minutes))

    def start_run(self, command):
        self.runs.append([command, None, None])
        return len(self.runs)

    def finish_run(self, run_id, status, stats):
        self.runs[run_id - 1][1:] = [status, stats]

    def begin_run_company(self, run_id, mst):
        self.company_runs.append(("begin", run_id, mst))

    def finish_run_company(self, run_id, mst, status, stats):
        self.company_runs.append(("finish", run_id, mst, status, stats))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(invoice_job.time, "sleep", lambda s: None)
    monkeypatch.setattr(invoice_job, "XML_THROTTLE_SECONDS", 0)


@pytest.fixture
def xml_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())
    monkeypatch.setattr(invoice_job, "_do_xml_task",
                        lambda store, company, tokens, task, run_id, out_dir:
                        seen.append((company["company_mst"], task["task_id"], run_id)))
    return seen


# ---------------------------------------------------------------------------
# Rút việc + vòng đời job_runs
# ---------------------------------------------------------------------------

def test_worker_processes_every_company_of_a_batch_then_stops_when_empty(xml_seen, tmp_path):
    store = _WorkerStore([[_task(A, 1), _task(A, 2), _task(B, 3)]])
    assert invoice_job.run_worker(store, "xml", tmp_path, once=True) == 0

    assert xml_seen == [(A, 1, 1), (A, 2, 1), (B, 3, 1)]
    assert store.claims[0][:3] == ("xml", invoice_job.WORKER_PER_COMPANY, invoice_job.WORKER_BATCH)
    # 1 đợt việc = 1 dòng job_runs, đóng khi hộp chờ rỗng
    assert store.runs == [["worker-xml", "ok", {"companies": {A: {"xml_ok": 2}, B: {"xml_ok": 1}}}]]
    assert ("finish", 1, A, "ok", {"xml_ok": 2}) in store.company_runs
    assert ("finish", 1, B, "ok", {"xml_ok": 1}) in store.company_runs


def test_idle_worker_writes_no_job_runs(xml_seen, tmp_path):
    """Worker ngồi chờ cả ngày không được sinh hàng trăm dòng job_runs rỗng."""
    store = _WorkerStore([])
    invoice_job.run_worker(store, "xml", tmp_path, once=True)
    assert store.runs == [] and store.company_runs == []


def test_worker_sleeps_instead_of_exiting_when_not_once(xml_seen, tmp_path, monkeypatch):
    slept, calls = [], {"n": 0}
    monkeypatch.setattr(invoice_job.time, "sleep", slept.append)
    store = _WorkerStore([[], [_task(A, 1)]])

    def stop():
        calls["n"] += 1
        return calls["n"] > 3

    invoice_job.run_worker(store, "xml", tmp_path, idle=42, stop=stop)
    assert 42 in slept                      # hộp chờ rỗng → ngủ rồi xem lại
    assert xml_seen == [(A, 1, 1)]          # việc tới sau vẫn được làm


# ---------------------------------------------------------------------------
# Một công ty hỏng không kéo theo công ty khác
# ---------------------------------------------------------------------------

def test_login_failure_cools_down_only_that_company(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())

    def fake_do(store, company, tokens, task, run_id, out_dir):
        if company["company_mst"] == A:
            raise invoice_job.LoginError("sai mật khẩu")
        seen.append(task["task_id"])

    monkeypatch.setattr(invoice_job, "_do_xml_task", fake_do)
    store = _WorkerStore([[_task(A, 1), _task(A, 2), _task(B, 3)], [_task(B, 4)]])
    invoice_job.run_worker(store, "xml", tmp_path, once=True)

    assert seen == [3, 4]                                   # B vẫn chạy bình thường
    assert store.released[0] == ([1, 2], invoice_job.COOLDOWN_MINUTES)   # A trả lại, hẹn sau
    assert store.claims[1][3] == [A]                        # mẻ sau không nhận việc của A
    assert store.runs[0][1] == "partial"
    assert ("finish", 1, A, "error", {"login_error": "sai mật khẩu"}) in store.company_runs


def test_ctrl_c_returns_unfinished_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())

    def fake_do(store, company, tokens, task, run_id, out_dir):
        if task["task_id"] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(invoice_job, "_do_xml_task", fake_do)
    store = _WorkerStore([[_task(A, 1), _task(A, 2), _task(B, 3)]])
    with pytest.raises(KeyboardInterrupt):
        invoice_job.run_worker(store, "xml", tmp_path, once=True)

    # Cả mẻ được trả (store thật chỉ đụng task còn 'running' → task 1 đã xong không bị ảnh hưởng)
    assert store.released[-1] == ([1, 2, 3], 0)
    assert store.runs[0][1] == "error"


def test_pdf_worker_defers_tasks_of_a_tripped_site_with_delay(monkeypatch, tmp_path):
    """Site NCC đang sập: task hẹn lại DEFER_MINUTES, không trả ngay để mẻ sau nhận lại."""
    class _Adapter:
        source_id = "hilo"

        def fetch_pdf(self, task):
            raise invoice_job.TaskError("timeout", "network")

    monkeypatch.setattr(invoice_job.pdf_providers, "get_adapter", lambda sid: None)
    monkeypatch.setattr(invoice_job.pdf_providers, "adapter_for_mst_tcgp", lambda m: _Adapter())
    monkeypatch.setattr(invoice_job.pdf_providers, "supported_mst_tcgp", lambda: [])
    failed = []
    store = _WorkerStore([[_task(A, i) for i in range(1, 9)]])
    store.fail_task = lambda task, kind, err, run_id: failed.append(task["task_id"]) or "pending"

    invoice_job.run_worker(store, "pdf", tmp_path, once=True)

    breaker_threshold = job_policy.CircuitBreaker().threshold
    assert failed == list(range(1, breaker_threshold + 1))          # gọi tới khi ngắt mạch
    assert store.released == [(list(range(breaker_threshold + 1, 9)), invoice_job.DEFER_MINUTES)]


# ---------------------------------------------------------------------------
# Đăng nhập GDT dưới khóa theo công ty
# ---------------------------------------------------------------------------

class _TokenStore:
    def __init__(self, db_token):
        self.db_token, self.locked, self.invalidated, self.saved = db_token, [], 0, []

    @contextmanager
    def login_lock(self, mst):
        self.locked.append(mst)
        yield

    def get_token(self, mst, min_ttl_seconds=600):
        return self.db_token

    def invalidate_token(self, mst):
        self.invalidated += 1
        self.db_token = None

    def save_token(self, mst, token, expires):
        self.saved.append(token)
        self.db_token = token


COMPANY = {"company_mst": A, "gdt_username": A + "-U002"}


def _login_counter(monkeypatch):
    calls = []

    def fake_auth(user, password):
        calls.append(user)
        return {"ok": True, "token": f"new-{len(calls)}", "attempts": 1}

    monkeypatch.setattr(invoice_job.api_client, "authenticate_auto", fake_auth)
    monkeypatch.setattr(invoice_job.api_client, "jwt_expiry", lambda tok: None)
    monkeypatch.setenv("HDT_PASSWORD_" + A, "x")
    return calls


def test_refresh_uses_token_another_worker_just_got(monkeypatch):
    """Worker khác vừa đăng nhập (token trong DB đã khác cái bị từ chối) → không giải captcha."""
    logins = _login_counter(monkeypatch)
    store = _TokenStore(db_token="tok-old")
    tp = invoice_job.TokenProvider(store, COMPANY, Counter())
    assert tp.get() == "tok-old"

    store.db_token = "tok-from-other-worker"
    assert tp.refresh() == "tok-from-other-worker"
    assert logins == [] and store.invalidated == 0 and store.locked == [A]


def test_refresh_logs_in_when_db_still_has_rejected_token(monkeypatch):
    logins = _login_counter(monkeypatch)
    store = _TokenStore(db_token="tok-old")
    tp = invoice_job.TokenProvider(store, COMPANY, Counter())
    tp.get()

    assert tp.refresh() == "new-1"
    assert logins == [A + "-U002"] and store.invalidated == 1 and store.saved == ["new-1"]


def test_first_login_rechecks_db_under_lock(monkeypatch):
    """Không có token: chờ khóa xong thấy worker kia đã đăng nhập → dùng luôn."""
    logins = _login_counter(monkeypatch)

    class _Racing(_TokenStore):
        @contextmanager
        def login_lock(self, mst):
            self.db_token = "tok-from-other-worker"      # worker kia đăng nhập trong lúc ta chờ khóa
            yield

    store = _Racing(db_token=None)
    assert invoice_job.TokenProvider(store, COMPANY, Counter()).get() == "tok-from-other-worker"
    assert logins == []


# ---------------------------------------------------------------------------
# sync-lists: chỉ danh sách, không tải XML, khóa riêng
# ---------------------------------------------------------------------------

class _SyncStore(_WorkerStore):
    def __init__(self, locked=False):
        super().__init__([])
        self.locked, self.lock_keys = locked, []

    def try_lock(self, key=None):
        self.lock_keys.append(key)
        return not self.locked

    def unlock(self, key=None):
        self.lock_keys.append(("unlock", key))

    def last_ok_run(self, command, on_day):
        return True

    def get_cursor(self, mst, st):
        return None

    def status_report(self):
        return {"counts": []}


def test_sync_lists_only_lists_with_its_own_lock(monkeypatch):
    from invoice_store import SYNC_LOCK_KEY
    seen = []
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())

    def fake_sync(store, company, tokens, sub_types, windows_for, breaker, stats, move_cursor=True):
        seen.append((company["company_mst"], move_cursor))
        stats["listed"] += 10
        stats["new_invoices"] += 2

    monkeypatch.setattr(invoice_job, "sync_lists", fake_sync)
    monkeypatch.setattr(invoice_job, "_do_xml_task", lambda *a, **k: pytest.fail("không được tải XML"))
    store = _SyncStore()
    args = invoice_job.argparse.Namespace(company=None)

    assert invoice_job.cmd_sync_lists(store, args) == 0
    assert seen == [(A, True), (B, True)]                   # mốc đồng bộ vẫn tiến như tick
    assert store.lock_keys == [SYNC_LOCK_KEY, ("unlock", SYNC_LOCK_KEY)]
    assert store.runs[0][:2] == ["sync-lists", "ok"]


def test_sync_lists_skips_when_another_is_running(monkeypatch):
    monkeypatch.setattr(invoice_job, "sync_lists", lambda *a, **k: pytest.fail("không được chạy"))
    store = _SyncStore(locked=True)
    assert invoice_job.cmd_sync_lists(store, invoice_job.argparse.Namespace(company=None)) == 0
    assert store.runs == []


def test_cli_wires_worker_and_sync_lists(monkeypatch):
    seen = {}

    class _Store:
        def __init__(self, dsn):
            pass

        def close(self):
            pass

    monkeypatch.setattr(invoice_job, "InvoiceStore", _Store)
    monkeypatch.setattr(invoice_job, "_setup_logging", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(invoice_job, "run_worker",
                        lambda store, tt, out, **k: seen.update(type=tt, **k) or 0)
    assert invoice_job.main(["worker", "--type", "pdf", "--per-company", "5", "--once"]) == 0
    assert seen["type"] == "pdf" and seen["per_company"] == 5 and seen["once"] is True
    assert seen["batch"] == invoice_job.WORKER_BATCH


def test_gdt_429_backs_off_instead_of_hammering(monkeypatch, tmp_path):
    """26/10: 5 lần 429 trong 4 giây (nhịp 0,7 s) làm ngắt mạch dừng cả lượt main_auto."""
    slept, calls = [], {"n": 0}
    monkeypatch.setattr(invoice_job.time, "sleep", slept.append)

    def flaky(store, company, tokens, task, run_id, out_dir):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise invoice_job.api_client.GDTError("429 – Quá nhiều request, cần chờ", kind="rate_limit")

    class _Fail(_WorkerStore):
        def fail_task(self, task, kind, msg, run_id):
            return "pending"

    monkeypatch.setattr(invoice_job, "_do_xml_task", flaky)
    stats = invoice_job.Counter()
    breaker = invoice_job.job_policy.CircuitBreaker()
    co = {"company_mst": A, "gdt_username": A}
    invoice_job.xml_batch(_Fail([]), co, object(), breaker, 1, [_task(A, i) for i in range(1, 5)], stats, tmp_path)
    assert [s for s in slept if s >= 60] == [60, 120]          # nghỉ tăng dần sau mỗi 429
    assert stats["xml_ok"] == 2 and not breaker.open
