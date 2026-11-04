"""NCC chỉ khai báo (HT/EFY — reCAPTCHA server kiểm, đang nghiên cứu): nhận đúng MSTTCGP, không tự tra.

Run::

    pytest tests/test_pdf_manual_portals.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402


@pytest.mark.parametrize("mst, source_id", [("0315638251", "ht"), ("0102519041", "efy")])
def test_routes_and_never_fetches(mst, source_id):
    a = pdf_providers.adapter_for_mst_tcgp(mst)
    assert a.source_id == source_id
    assert pdf_providers.get_adapter(source_id) is a
    with pytest.raises(PdfFetchError) as e:
        a.fetch_pdf({"shdon": 1, "ma_tra_cuu": "X"})
    assert e.value.kind == "no_portal"


@pytest.mark.parametrize("host", ["tracuuhoadon.fpt.com.vn", "laphoadon.htinvoice.vn", "tracuu.ihoadon.vn"])
def test_mail_links_not_claimed(host):
    """Không nhận host → link mail giữ `new`, không bị đánh `ignored` vĩnh viễn."""
    a = pdf_providers.adapter_for_host(host)
    assert a is None or a.source_id not in {"fpt", "ht", "efy"}
