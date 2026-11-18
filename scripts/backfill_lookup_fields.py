"""
Tính lại mã tra cứu / link tra cứu cho XML đã lưu (invoice_xml), theo cách parser mới (18/11/2026).

Trước đó parser chỉ nhận vài tên trường mã tra cứu cố định (bỏ sót "Mã số bí mật", "Mã tra cứu"… → 15.775 XML có
mã mà `ma_tra_cuu` trống, web hiện "!" cần mã) và lấy link = URL đầu tiên trong XML (→ ~3.900 link cổng Thuế,
~1.000 website người bán, 17 link cắt hỏng của Thái Sơn).

Chỉ dùng dữ liệu đã parse trong DB (`parsed`), không cần file XML:
- `ma_tra_cuu`: chỉ ĐIỀN khi đang trống (không đổi mã đang có, vd Fkey của VNPT nằm ngoài TTKhac).
- `lookup_url`: link tra cứu trong TTKhac nếu có; không thì giữ link cũ nếu dùng được (cắt ở '['); link cũ là cổng
  Thuế / website công ty → bỏ (NULL).
Cập nhật cả cột lẫn `parsed.ma_tra_cuu` / `parsed.lookup_url`.

Mặc định chạy thử (không ghi): in số dòng đổi, ví dụ, và các HĐ sẽ đổi adapter PDF (HĐ không có MSTTCGP được chọn
adapter theo host của link). Ghi thật: --apply.

    python -X utf8 scripts/backfill_lookup_fields.py [--apply] [--company MST]
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg2  # noqa: E402
from psycopg2.extras import Json, execute_values  # noqa: E402

import pdf_providers  # noqa: E402
import xml_parser  # noqa: E402


def recompute(parsed: dict, old_code: str | None, old_url: str | None) -> tuple[str | None, str | None]:
    tt = parsed.get("tt_khac") or {}
    code = old_code or xml_parser._guess_ma_tra_cuu(tt) or None
    sites = xml_parser.site_urls(parsed)
    url = xml_parser.lookup_url_from_ttkhac(tt, sites)
    if not url and old_url:
        m = xml_parser._URL_RE.match(old_url.strip())
        cleaned = m.group(0) if m else ""
        url = cleaned if xml_parser._usable(cleaned, sites) else ""
    return code, url or None


def adapter_by_host(url: str | None, tt: dict) -> str | None:
    for u in (url, (tt or {}).get("PortalLink")):
        a = pdf_providers.adapter_for_host(urlsplit(u or "").hostname)
        if a is not None:
            return a.source_id
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="ghi vào DB (mặc định chỉ chạy thử)")
    ap.add_argument("--company", help="chỉ 1 MST công ty")
    args = ap.parse_args(argv)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    sql = """SELECT x.invoice_id, x.ma_tra_cuu, x.lookup_url, x.parsed,
                    NULLIF(COALESCE(NULLIF(x.mst_tcgp, ''), i.list_raw->>'msttcgp'), '') AS tcgp,
                    (SELECT t.status FROM tasks t WHERE t.invoice_id = x.invoice_id AND t.task_type = 'pdf') AS pdf_task
               FROM invoice_xml x JOIN invoices i ON i.id = x.invoice_id"""
    params = ()
    if args.company:
        sql += " WHERE i.company_mst = %s"
        params = (args.company,)
    cur.execute(sql, params)

    updates = []
    stats = collections.Counter()
    examples: dict = {}
    routing = collections.Counter()
    for iid, old_code, old_url, parsed, tcgp, pdf_task in cur.fetchall():
        parsed = parsed or {}
        code, url = recompute(parsed, old_code, old_url)
        if code == (old_code or None) and url == (old_url or None):
            continue
        if code != (old_code or None):
            stats["ma_tra_cuu điền thêm"] += 1
            examples.setdefault(("code", tcgp), (iid, code))
        if url != (old_url or None):
            kind = "link mới từ TTKhac" if url and url != old_url else "link bỏ (cổng Thuế / website / hỏng)" if not url else "link đổi"
            if url and old_url and url != old_url:
                kind = "link đổi"
            stats[kind] += 1
            examples.setdefault((kind, (urlsplit(old_url or "").hostname, urlsplit(url or "").hostname)), (iid, old_url, url))
            if not tcgp:                     # job chọn adapter theo host của link
                before = adapter_by_host(old_url, parsed.get("tt_khac"))
                after = adapter_by_host(url, parsed.get("tt_khac"))
                if before != after:
                    routing[(before, after, pdf_task)] += 1
        updates.append((iid, code, url))

    print(f"Dòng sẽ cập nhật: {len(updates)}")
    for k, n in stats.most_common():
        print(f"  {k}: {n}")
    print("Ví dụ:")
    for k, v in list(examples.items())[:25]:
        print(f"  {k}: {v}")
    print("HĐ không có MSTTCGP đổi adapter theo host (trước → sau, task pdf):", dict(routing) or "không có")

    if not args.apply:
        print("Chạy thử — chưa ghi gì. Ghi thật: --apply")
        return 0
    execute_values(cur, """
        UPDATE invoice_xml x
           SET ma_tra_cuu = v.code, lookup_url = v.url,
               parsed = jsonb_set(jsonb_set(x.parsed, '{ma_tra_cuu}', to_jsonb(COALESCE(v.code, ''))),
                                  '{lookup_url}', to_jsonb(COALESCE(v.url, '')))
          FROM (VALUES %s) AS v(invoice_id, code, url)
         WHERE x.invoice_id = v.invoice_id
    """, updates, template="(%s::bigint, %s::text, %s::text)", page_size=1000)
    conn.commit()
    print(f"Đã ghi {len(updates)} dòng.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
