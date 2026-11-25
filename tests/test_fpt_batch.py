"""scripts/fpt_batch.py — đọc link FPT, gộp HĐ, kiểm JSON /api/search (offline, không mở trình duyệt).

Luồng thật: link mail `?mst=<MST bán>&sec=<mã>` → Chromium bắt `GET /api/search` → JSON
(`doc` + `pdf` base64) → `output/manual/fpt/*.json` → adapter `pdf_providers/fpt_import.py`.

Run::

    python -X utf8 -m pytest tests/test_fpt_batch.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import fpt_batch  # noqa: E402

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def payload(**over) -> dict:
    doc = {"stax": "2300680991", "form": "1", "serial": "C25TAB", "seq": "00000943"}
    doc.update(over.pop("doc", {}))
    data = {"doc": doc,
            "pdf": "data:application/pdf;base64," + base64.b64encode(PDF).decode(),
            "xml": "<HDon/>"}
    data.update(over)
    return data


class TestParseLink:
    def test_https(self):
        assert fpt_batch.parse_link(
            "https://tracuuhoadon.fpt.com.vn/?mst=2300680991&sec=rwkd9dym0r"
        ) == ("2300680991", "rwkd9dym0r")

    def test_html_entity(self):
        # mail_reader lưu URL trong HTML → có thể còn &amp;
        assert fpt_batch.parse_link(
            "http://tracuuhoadon.fpt.com.vn/?mst=0313330856&amp;sec=r2djpwnj2g"
        ) == ("0313330856", "r2djpwnj2g")

    def test_missing_sec(self):
        assert fpt_batch.parse_link("https://tracuuhoadon.fpt.com.vn/?mst=2300680991") is None

    def test_homepage(self):
        assert fpt_batch.parse_link("https://tracuuhoadon.fpt.com.vn/") is None


class TestGroupLinks:
    def test_dedup_same_sec(self):
        # 2 link cùng (mst, sec) trong 2 mail (id 29, 357) → 1 HĐ
        rows = [(29, "https://tracuuhoadon.fpt.com.vn/?mst=0107622966&sec=ry09wq7o4g"),
                (357, "https://tracuuhoadon.fpt.com.vn/?mst=0107622966&sec=ry09wq7o4g"),
                (5240, "http://tracuuhoadon.fpt.com.vn/?mst=0313330856&sec=r2djpwnj2g")]
        jobs = fpt_batch.group_links(rows)
        assert [(j.mst, j.sec, j.link_ids) for j in jobs] == [
            ("0107622966", "ry09wq7o4g", [29, 357]),
            ("0313330856", "r2djpwnj2g", [5240]),
        ]

    def test_drop_unusable(self):
        assert fpt_batch.group_links([(1, "https://tracuuhoadon.fpt.com.vn/")]) == []


class TestCheckPayload:
    def test_ok_data_url(self):
        desc = fpt_batch.check_payload(payload())
        assert "2300680991" in desc and "C25TAB" in desc and "943" in desc

    def test_ok_raw_base64(self):
        data = payload()
        data["pdf"] = base64.b64encode(PDF).decode()
        fpt_batch.check_payload(data)

    def test_missing_doc(self):
        with pytest.raises(ValueError, match="'doc'"):
            fpt_batch.check_payload({"pdf": data_url()})

    def test_doc_missing_fields(self):
        with pytest.raises(ValueError, match="serial"):
            fpt_batch.check_payload(payload(doc={"serial": ""}))

    def test_pdf_not_pdf(self):
        data = payload()
        data["pdf"] = base64.b64encode(b"<html>captcha</html>").decode()
        with pytest.raises(ValueError, match="không phải PDF"):
            fpt_batch.check_payload(data)

    def test_no_pdf(self):
        data = payload()
        data.pop("pdf")
        with pytest.raises(ValueError, match="'pdf'"):
            fpt_batch.check_payload(data)


def data_url() -> str:
    return "data:application/pdf;base64," + base64.b64encode(PDF).decode()


def test_requeue_keys_maps_json_to_task(monkeypatch):
    """JSON vừa lấy → tìm đúng task PDF (MST bán, mẫu, ký hiệu, số) và đưa về hàng đợi."""
    import psycopg2

    import invoice_store

    class _Cur:
        def __init__(self):
            self.calls, self.rows = [], [(42,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return self.rows

    cur = _Cur()

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return cur

    class _Store:
        ids = None

        def __init__(self, dsn):
            pass

        def requeue_tasks(self, ids):
            _Store.ids = ids
            return len(ids)

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(invoice_store, "InvoiceStore", _Store)

    matched, n = fpt_batch.requeue_keys([("2300680991", "1", "C25TAB", "00000943")])
    assert (matched, n) == (1, 1) and _Store.ids == [42]
    sql, params = cur.calls[0]
    assert params == ("2300680991", 1, "C25TAB", 943)
    assert "upper(i.khhdon)" in sql
