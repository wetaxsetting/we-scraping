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


XML = (
    '<HDon><DLHDon Id="_1"><TTChung><PBan>2.1.0</PBan><THDon>HOA DON</THDon>'
    "<KHMSHDon>1</KHMSHDon><KHHDon>C25TAB</KHHDon><SHDon>943</SHDon><NLap>2025-05-31</NLap>"
    "<DVTTe>VND</DVTTe><MSTTCGP>0104128565</MSTTCGP></TTChung><NDHDon>"
    "<NBan><Ten>SAMSUNG</Ten><MST>2300680991</MST></NBan>"
    "<NMua><Ten>WEBCASH</Ten><MST>1201496252</MST></NMua>"
    "<DSHHDVu><HHDVu><TChat>1</TChat><STT>1</STT><THHDVu>Phi</THHDVu><SLuong>1</SLuong>"
    "<DGia>5000</DGia><ThTien>5000</ThTien><TSuat>10%</TSuat></HHDVu></DSHHDVu>"
    "<TToan><TgTCThue>5000</TgTCThue><TgTThue>500</TgTThue><TgTTTBSo>5500</TgTTTBSo></TToan>"
    "</NDHDon></DLHDon></HDon>"
)


def _drop(tmp_path, monkeypatch, name="api_rwkd9dym0r.json", seq="00000943", pdf=None,
          xml="<HDon/>") -> Path:
    monkeypatch.setenv("INVOICE_OUTPUT_DIR", str(tmp_path))
    d = fpt_import.drop_dir()
    d.mkdir(parents=True, exist_ok=True)
    pdf = pdf if pdf is not None else _pdf_with("So 00000943")
    data = {"doc": {"stax": "2300680991", "form": "1", "serial": "C25TAB", "seq": seq, "sec": "rwkd9dym0r",
                    "idt": "2025-05-31 00:00:00"},
            "pdf": "data:application/pdf;base64, " + base64.b64encode(pdf).decode(), "xml": xml}
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


# ---------------------------------------------------------------------------
# Định tuyến HĐ không có XML/MSTTCGP + XML lấy kèm PDF (25/11/2026)
# ---------------------------------------------------------------------------

def test_lookup_host_routes_only():
    """Cổng FPT định tuyến được HĐ 'không mã' nhưng KHÔNG nhận link mail (link phải giữ 'new')."""
    assert pdf_providers.adapter_for_lookup_host("tracuuhoadon.fpt.com.vn").source_id == "fpt"
    assert pdf_providers.adapter_for_lookup_host("TRACUUHOADON.FPT.COM.VN").source_id == "fpt"
    assert pdf_providers.adapter_for_lookup_host("example.com") is None
    assert pdf_providers.adapter_for_host("tracuuhoadon.fpt.com.vn") is None


def test_pick_adapter_falls_back_to_mail_link_portal():
    from web import invoice_job

    class _Store:
        def seller_mst_tcgp(self, nbmst):
            return None

        def seller_link_hosts(self, nbmst):
            return ["tracuuhoadon.fpt.com.vn"]

    adapter = invoice_job.pick_adapter(_Store(), {"nbmst": "0107622966", "mst_tcgp": None})
    assert adapter is not None and adapter.source_id == "fpt"


def test_pick_adapter_unknown_portal_still_none():
    from web import invoice_job

    class _Store:
        def seller_mst_tcgp(self, nbmst):
            return None

        def seller_link_hosts(self, nbmst):
            return ["example.com"]

    assert invoice_job.pick_adapter(_Store(), {"nbmst": "0107622966", "mst_tcgp": None}) is None


def test_fetch_xml_before_and_after_pdf(tmp_path, monkeypatch):
    """JSON có `xml` → lấy được XML cả trước và sau khi fetch_pdf chuyển JSON sang used/."""
    _drop(tmp_path, monkeypatch, xml=XML)
    a = fpt_import.FptAdapter()
    assert a.fetch_xml(INV).lstrip().startswith(b"<HDon")
    a.fetch_pdf(INV)
    assert a.fetch_xml(INV).lstrip().startswith(b"<HDon")


def test_fetch_xml_other_invoice_is_no_portal(tmp_path, monkeypatch):
    _drop(tmp_path, monkeypatch, seq="00001128", xml=XML)
    with pytest.raises(PdfFetchError) as e:
        fpt_import.FptAdapter().fetch_xml(INV)
    assert e.value.kind == "no_portal"


def test_fetch_xml_rejects_non_xml(tmp_path, monkeypatch):
    _drop(tmp_path, monkeypatch, xml="")
    with pytest.raises(PdfFetchError) as e:
        fpt_import.FptAdapter().fetch_xml(INV)
    assert e.value.kind == "bad_content"


def test_save_portal_xml_writes_file(tmp_path):
    """pdf_batch gọi helper này khi HĐ chưa có XML mà adapter có fetch_xml_with_pdf."""
    from datetime import date

    from web import invoice_job

    class _Adapter:
        source_id = "fpt"
        fetch_xml_with_pdf = True

        def fetch_xml(self, invoice):
            return XML.encode("utf-8")

    class _Store:
        def __init__(self):
            self.args = None

        def apply_email_xml(self, *args):
            self.args = args
            return True

    task = {"id": 7, "task_id": 9, "company_mst": "1201496252", "nbmst": "2300680991",
            "khmshdon": 1, "khhdon": "C25TAB", "shdon": 943, "ngay_lap": date(2025, 5, 31)}
    store = _Store()
    assert invoice_job._save_portal_xml(store, task, _Adapter(), None, tmp_path)
    invoice_id, run_id, rel, sha, parsed, partner = store.args
    assert invoice_id == 7 and run_id is None and len(sha) == 64
    assert (tmp_path / rel).exists() and rel.startswith("xml/1201496252/2025/05/2300680991/")
    assert parsed["thong_tin_chung"]["mst_tcgp"] == "0104128565"
    assert partner["mst"] == "2300680991"
