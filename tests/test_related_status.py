"""
HĐ gốc của HĐ thay thế / điều chỉnh (UI Spec v1.2 §3.15): lưu XML có TTHDLQuan → HĐ gốc thành Bị thay thế / Bị điều chỉnh.
Offline: không cần DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from invoice_store import InvoiceStore   # noqa: E402


class _Cur:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def test_saving_an_xml_marks_its_original():
    cur = _Cur()
    InvoiceStore._save_xml(cur, 42, "xml/a.xml", "0" * 64, {"thong_tin_chung": {}, "hang_hoa": []}, None, "xml")
    sql, params = cur.calls[-1]
    assert sql.startswith("UPDATE invoices o SET tthai = u.new_tthai") and params == (42,)


def test_only_originals_still_marked_original_are_upgraded():
    sql = InvoiceStore._RELATED_ORIGINALS_SQL.format(scope="TRUE")
    assert "COALESCE(o.tthai, 1) = 1" in sql                       # never downgrade (e.g. cancelled = 6)
    assert "jj.j->>'tinh_chat' IN ('1', '2')" in sql               # only replacement / adjustment
    assert "CASE WHEN r.tchd = '2' THEN 5 ELSE 4 END" in sql
    assert "substr(jj.j->>'ky_hieu', 2)" in sql                    # "1C26TAA" → "C26TAA"
    assert "o.id <> r.id" in sql
