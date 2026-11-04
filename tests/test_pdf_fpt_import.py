"""Adapter FPT: nhập JSON /api/search do script trình duyệt tải về (thư mục chờ manual/fpt).

Run::

    pytest tests/test_pdf_fpt_import.py -q
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import fpt_import       # noqa: E402

INV = {"nbmst": "2300680991", "khmshdon": 1, "khhdon": "C25TAB", "shdon": 943}


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((40, 40 + i * 18), f"{text} dong {i}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


def _drop(tmp_path, monkeypatch, name="api_rwkd9dym0r.json", seq="00000943", pdf=None) -> Path:
    monkeypatch.setenv("INVOICE_OUTPUT_DIR", str(tmp_path))
    d = fpt_import.drop_dir()
    d.mkdir(parents=True, exist_ok=True)
    pdf = pdf if pdf is not None else _pdf_with("So 00000943")
    data = {"doc": {"stax": "2300680991", "form": "1", "serial": "C25TAB", "seq": seq, "sec": "rwkd9dym0r",
                    "idt": "2025-05-31 00:00:00"},
            "pdf": "data:application/pdf;base64, " + base64.b64encode(pdf).decode(), "xml": "<HDon/>"}
    (d / name).write_text(json.dumps(data), encoding="utf-8")
    return d


def test_registered():
    a = pdf_providers.adapter_for_mst_tcgp("0104128565")
    assert a.source_id == "fpt" and isinstance(a, fpt_import.FptAdapter)
    assert pdf_providers.adapter_for_host("tracuuhoadon.fpt.com.vn") is None or \
        pdf_providers.adapter_for_host("tracuuhoadon.fpt.com.vn").source_id != "fpt"


def test_matches_and_moves_json(tmp_path, monkeypatch):
    d = _drop(tmp_path, monkeypatch)
    a = fpt_import.FptAdapter()
    assert a.has_pending_input()
    pdf = a.fetch_pdf(INV)
    assert common.is_pdf(pdf)
    assert not (d / "api_rwkd9dym0r.json").exists() and (d / "used" / "api_rwkd9dym0r.json").exists()
    assert not a.has_pending_input()


def test_no_json_is_no_portal(tmp_path, monkeypatch):
    _drop(tmp_path, monkeypatch, seq="00001128")          # JSON của HĐ khác
    with pytest.raises(PdfFetchError) as e:
        fpt_import.FptAdapter().fetch_pdf(INV)
    assert e.value.kind == "no_portal"
    assert (fpt_import.drop_dir() / "api_rwkd9dym0r.json").exists()   # không đụng JSON không khớp


def test_missing_dir_is_no_portal(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICE_OUTPUT_DIR", str(tmp_path))
    a = fpt_import.FptAdapter()
    assert not a.has_pending_input()
    with pytest.raises(PdfFetchError) as e:
        a.fetch_pdf(INV)
    assert e.value.kind == "no_portal"


def test_pdf_of_other_invoice_rejected(tmp_path, monkeypatch):
    _drop(tmp_path, monkeypatch, pdf=_pdf_with("So 00000999"))
    with pytest.raises(PdfFetchError) as e:
        fpt_import.FptAdapter().fetch_pdf(INV)
    assert e.value.kind == "bad_content"
