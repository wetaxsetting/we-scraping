"""web/fetch_pdf.py: tải PDF từ NCC cho XML có sẵn trên đĩa (không DB, không mạng trong test).

Sai ở đây thì: tải lại PDF đã có (tốn captcha site NCC), lưu PDF lệch thư mục so với XML,
hoặc âm thầm bỏ HĐ của NCC chưa có adapter mà không báo.

Run::

    pytest tests/test_fetch_pdf.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch_pdf = _load("fetch_pdf", "web/fetch_pdf.py")

PDF = b"%PDF-1.4\n" + b"x" * 2000


class _FakeAdapter(pdf_providers.PdfAdapter):
    source_id = "fake_ncc"
    name = "NCC giả"
    mst_tcgp = ("0109999999",)

    def __init__(self):
        self.seen = []
        self.fail_kind = None

    def fetch_pdf(self, invoice):
        self.seen.append(invoice)
        if self.fail_kind:
            raise pdf_providers.PdfFetchError("site lỗi", self.fail_kind)
        return PDF


@pytest.fixture
def adapter(monkeypatch):
    a = _FakeAdapter()
    monkeypatch.setitem(pdf_providers._REGISTRY, a.source_id, a)
    monkeypatch.setattr(fetch_pdf, "FETCH_PAUSE_SECONDS", 0)
    # XML trong test là JSON của dict đã parse → không phụ thuộc parser
    monkeypatch.setattr(fetch_pdf.xml_parser, "parse_xml_bytes", lambda b: json.loads(b))
    return a


def _xml(xml_dir, seller, name, tcgp, khhdon="C26TAA", shdon=1, ma_cqt=None):
    parsed = {"thong_tin_chung": {"ky_hieu_mau": "1", "ky_hieu": khhdon, "so_hd": shdon,
                                  "ngay_lap": "2026-09-30", "mst_tcgp": tcgp},
              "nguoi_ban": {"mst": seller, "ten": f"NB {seller}"},
              "thanh_toan": {"tong_tien_tt": 1000}, "ma_tra_cuu": "ABC", "ma_cqt": ma_cqt,
              "tt_khac": {"Fkey": "K1"}, "lookup_url": None}
    p = xml_dir / "1201496252" / "2026" / "09" / seller / f"{name}.xml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parsed), encoding="utf-8")
    return p


def test_saves_pdf_mirroring_xml_layout_and_skips_existing(tmp_path, adapter):
    xml_dir, pdf_dir = tmp_path / "xml", tmp_path / "pdf"
    _xml(xml_dir, "0100000001", "1_C26TAA_1", "0109999999")
    res = fetch_pdf.run(xml_dir, pdf_dir)
    out = pdf_dir / "1201496252" / "2026" / "09" / "0100000001" / "1_C26TAA_1.pdf"
    assert out.read_bytes() == PDF and res["stats"]["ok"] == 1
    assert not list(tmp_path.rglob("*.tmp"))

    res = fetch_pdf.run(xml_dir, pdf_dir)                 # chạy lại: không tải lần 2
    assert res["stats"]["da_co"] == 1 and len(adapter.seen) == 1

    fetch_pdf.run(xml_dir, pdf_dir, force=True)
    assert len(adapter.seen) == 2


def test_passes_lookup_fields_adapters_need(tmp_path, adapter):
    _xml(tmp_path / "xml", "0100000001", "1_C26MAA_7", "0109999999", khhdon="C26MAA", shdon=7,
         ma_cqt="M1-26-ABC")
    fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf")
    inv = adapter.seen[0]
    assert inv["nbmst"] == "0100000001" and inv["khhdon"] == "C26MAA" and inv["shdon"] == 7
    assert inv["tt_khac"] == {"Fkey": "K1"} and inv["ma_tra_cuu"] == "ABC"
    assert inv["ma_cqt"] == "M1-26-ABC" and inv["tong_tien"] == 1000
    assert inv["loai_hd"] == "MTT"                         # ký tự thứ 4 'M' = máy tính tiền


def test_reports_providers_without_adapter(tmp_path, adapter):
    xml_dir = tmp_path / "xml"
    no_ncc = "0109888888"                  # MSTTCGP giả, không adapter nào nhận
    _xml(xml_dir, "0312650437", "1_C26TNA_1", no_ncc)
    _xml(xml_dir, "0312650437", "1_C26TNA_2", no_ncc, shdon=2)
    _xml(xml_dir, "0318793907", "1_C26MSC_3", None, khhdon="C26MSC", shdon=3)
    res = fetch_pdf.run(xml_dir, tmp_path / "pdf")
    assert res["stats"]["chua_co_adapter"] == 3 and not adapter.seen
    assert res["no_adapter"][no_ncc]["hd"] == 2
    assert res["no_adapter"]["(không có MSTTCGP)"]["hd"] == 1
    assert not (tmp_path / "pdf").exists()


def test_branch_mst_tcgp_uses_parent_adapter(tmp_path, adapter):
    _xml(tmp_path / "xml", "0100000001", "1_C26TAA_1", "0109999999-001")
    res = fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf")
    assert res["stats"]["ok"] == 1
    assert pdf_providers.adapter_for_mst_tcgp("0109999999-001") is adapter


def test_dry_run_and_filters_download_nothing(tmp_path, adapter):
    xml_dir = tmp_path / "xml"
    _xml(xml_dir, "0100000001", "1_C26TAA_1", "0109999999")
    _xml(xml_dir, "0100000002", "1_C26TAA_2", "0109999999", shdon=2)
    res = fetch_pdf.run(xml_dir, tmp_path / "pdf", dry_run=True)
    assert res["stats"]["se_tai"] == 2 and not adapter.seen and not (tmp_path / "pdf").exists()

    res = fetch_pdf.run(xml_dir, tmp_path / "pdf", seller="0100000002")
    assert [i["nbmst"] for i in adapter.seen] == ["0100000002"]
    res = fetch_pdf.run(xml_dir, tmp_path / "pdf", mst_tcgp="0100684378")
    assert res["stats"]["ok"] == 0 and len(adapter.seen) == 1


def test_company_filter_uses_top_level_folder(tmp_path, adapter):
    xml_dir = tmp_path / "xml"
    _xml(xml_dir, "0100000001", "1_C26TAA_1", "0109999999")
    old = xml_dir / "1201496252-U002" / "2026" / "09" / "0100000001"
    old.mkdir(parents=True)
    (old / "1_C26TAA_1.xml").write_bytes(next(xml_dir.rglob("*.xml")).read_bytes())
    res = fetch_pdf.run(xml_dir, tmp_path / "pdf", company="1201496252")
    assert res["stats"]["ok"] == 1 and len(adapter.seen) == 1
    assert not (tmp_path / "pdf" / "1201496252-U002").exists()


def test_limit_leaves_rest_for_next_run(tmp_path, adapter):
    for n in range(1, 4):
        _xml(tmp_path / "xml", "0100000001", f"1_C26TAA_{n}", "0109999999", shdon=n)
    res = fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf", limit=2)
    assert res["stats"]["ok"] == 2 and res["stats"]["de_lan_sau"] == 1
    res = fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf", limit=2)
    assert res["stats"]["ok"] == 1 and res["stats"]["da_co"] == 2


def test_failing_site_is_skipped_after_consecutive_transient_errors(tmp_path, adapter):
    for n in range(1, 9):
        _xml(tmp_path / "xml", "0100000001", f"1_C26TAA_{n}", "0109999999", shdon=n)
    adapter.fail_kind = "network"
    res = fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf")
    assert res["stats"]["loi"] == 5 and res["stats"]["bo_qua_site_loi"] == 3
    assert not (tmp_path / "pdf").exists()


def test_rejects_non_pdf_content(tmp_path, adapter, monkeypatch):
    monkeypatch.setattr(adapter, "fetch_pdf", lambda inv: b"<html>loi</html>")
    _xml(tmp_path / "xml", "0100000001", "1_C26TAA_1", "0109999999")
    res = fetch_pdf.run(tmp_path / "xml", tmp_path / "pdf")
    assert res["stats"]["loi"] == 1 and res["failures"][0][1] == "fake_ncc/bad_content"
    assert not (tmp_path / "pdf").exists()
