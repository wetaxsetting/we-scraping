"""Adapter ATOM / 1invoice — kế thừa EPAY (cổng M-invoice white-label). Phần luồng chung ở
tests/test_pdf_epay.py; ở đây chỉ kiểm khai báo + host riêng.

Run::

    pytest tests/test_pdf_atom.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, atom, epay   # noqa: E402

import pytest   # noqa: E402


def test_declares():
    a = atom.AtomAdapter()
    assert isinstance(a, epay.EpayAdapter)
    assert a.source_id == "atom" and a.mst_tcgp == ("0316887099",)
    assert a.BASE == "https://tracuuhoadon.1invoice.vn" and a.hosts == ("tracuuhoadon.1invoice.vn",)


def test_tenant_not_found_is_no_pdf(monkeypatch):
    class R:
        status_code, content, text = 500, b"", ""

        @staticmethod
        def json():
            return {"code": "5000", "message": "Get Tenant fail because not found  Tenant with code = ['x']"}

    class S:
        url = None

        def request(self, method, url, **kw):
            S.url = url
            return R()
    monkeypatch.setattr(epay, "new_session", lambda: S())
    with pytest.raises(PdfFetchError) as ei:
        atom.AtomAdapter().fetch_pdf({"shdon": 1, "nbmst": "0101234567", "tt_khac": {"Số bảo mật": "ABCD1234"}})
    assert ei.value.kind == "no_pdf"
    assert S.url.startswith("https://tracuuhoadon.1invoice.vn/api/Search/SearchInvoice")
