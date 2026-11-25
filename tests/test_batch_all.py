"""scripts/batch_all.py — chạy tuần tự FPT rồi M-Invoice, gộp mã thoát (offline).

Run::

    python -X utf8 -m pytest tests/test_batch_all.py -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import batch_all  # noqa: E402


def _fake(returncodes, calls):
    seq = list(returncodes)

    def run(cmd, cwd=None):
        calls.append(cmd)

        class _R:
            pass

        r = _R()
        r.returncode = seq.pop(0)
        return r

    return run


def test_runs_fpt_then_minvoice_with_limits(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake([0, 0], calls))
    assert batch_all.main(["--fpt-limit", "3", "--minvoice-limit", "5",
                           "--log-dir", str(tmp_path)]) == 0
    assert len(calls) == 2
    assert calls[0][3].endswith("fpt_batch.py") and calls[0][5] == "3"
    assert calls[1][3].endswith("minvoice_batch.py") and calls[1][5] == "5"
    assert calls[0][-1] == str(tmp_path / "fpt_batch.log")


def test_only_minvoice_runs_one(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake([0], calls))
    assert batch_all.main(["--only", "minvoice", "--log-dir", str(tmp_path)]) == 0
    assert len(calls) == 1 and calls[0][3].endswith("minvoice_batch.py")


def test_exit_code_aggregation(monkeypatch, tmp_path):
    for codes, want in (([2, 0], 2), ([0, 2], 2), ([1, 0], 1), ([1, 1], 1), ([0, 0], 0)):
        calls = []
        monkeypatch.setattr(subprocess, "run", _fake(codes, calls))
        assert batch_all.main(["--log-dir", str(tmp_path)]) == want


def test_dry_run_calls_nothing(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake([], calls))
    assert batch_all.main(["--dry-run", "--log-dir", str(tmp_path)]) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "fpt_batch.py" in out and "minvoice_batch.py" in out
