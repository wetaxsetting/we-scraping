# fetch_missing_xml.py
"""
Tải XML từ GDT cho danh sách HĐ thiếu XML (file CSV, vd docs/_missing_0318793907_2026.csv
xuất từ WeTax) → lưu file vào 1 thư mục. Không ghi DB, chỉ dùng lại JWT của job.

CSV cần các cột: company_tax_code, form_no, serial_no, invoice_no, và year + month (thiếu thì lấy
từ invoice_date dạng m/d/yyyy); các cột khác bỏ qua. 1 file CSV có thể gồm nhiều công ty.
HĐ trong file là HĐ BÁN RA của công ty → MST người bán = company_tax_code (Excel bỏ số 0 đầu
nên tự thêm lại cho đủ 10 số). Ký hiệu có ký tự thứ 4 = M (máy tính tiền) → endpoint sco-query.

Mỗi HĐ 1 file `<thư mục>/<MST>/<năm>/<tháng>/<mẫu>_<ký hiệu>_<số>.xml`; file đã có thì bỏ qua
(chạy lại được, chỉ tải phần còn thiếu). Kết quả từng dòng ghi vào `<thư mục>/_ket_qua.csv`.
Lọc bằng --mst / --year / --month (tháng 1–12 hoặc yyyy-mm).

Cần: công ty đã có trong DB (invoice_job.py add-company), mật khẩu HDT_PASSWORD_<MST> trong .env.

Usage:
    python -X utf8 web/fetch_missing_xml.py --csv docs/_missing_0318793907_2026.csv --out docs/xml --limit 5
    python -X utf8 web/fetch_missing_xml.py --csv docs/_missing_0318793907_2026.csv --out docs/xml
    python -X utf8 web/fetch_missing_xml.py --csv ... --out docs/xml --mst 0318793907 --month 2026-08
    python -X utf8 web/fetch_missing_xml.py --csv ... --out docs/xml --year 2026 --month 9 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

import invoice_job                     # web/invoice_job.py (cùng thư mục)
from invoice_job import GDTError, InvoiceStore, api_client, log, xml_parser

PAUSE_SECONDS = 0.7                    # nhịp giữa 2 lần tải, như xml_batch của job
RATE_LIMIT_WAITS = (60, 120, 240, 300) # GDT 429 → nghỉ rồi thử lại đúng HĐ đó
RESULT_NAME = "_ket_qua.csv"


def read_rows(path: Path) -> list[dict]:
    """Đọc CSV (Excel trên Windows lưu cp1258, file khác có thể là UTF-8)."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1258"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(text.splitlines()))


def normalize_mst(value: str) -> str:
    """'318793907' → '0318793907'; giữ nguyên đuôi chi nhánh '-001'."""
    head, sep, tail = value.strip().partition("-")
    return head.zfill(10) + sep + tail


def to_invoice(row: dict) -> dict:
    year, month = row.get("year"), row.get("month")
    if not (year and month):                     # invoice_date dạng m/d/yyyy (Excel Mỹ)
        m, _, y = row["invoice_date"].strip().split(" ")[0].split("/")
        year, month = year or y, month or m
    return {
        "nbmst": normalize_mst(row["company_tax_code"]),
        "khmshdon": int(row["form_no"] or 1),
        "khhdon": row["serial_no"].strip(),
        "shdon": int(row["invoice_no"]),
        "year": int(year),
        "month": int(month),
    }


def parse_month(value: str) -> tuple[int | None, int]:
    """'8' → (None, 8); '2026-08' → (2026, 8)."""
    year, sep, month = value.partition("-")
    return (int(year), int(month)) if sep else (None, int(year))


def select(invoices: list[dict], mst: str | None, year: int | None, month: str | None) -> list[dict]:
    want_year, want_month = (None, None) if not month else parse_month(month)
    if year and want_year and year != want_year:
        raise ValueError(f"--year {year} khác năm trong --month {month}")
    want_year = want_year or year
    return [inv for inv in invoices
            if (not mst or inv["nbmst"] == normalize_mst(mst))
            and (not want_year or inv["year"] == want_year)
            and (not want_month or inv["month"] == want_month)]


def is_mtt(khhdon: str) -> bool:
    return len(khhdon) >= 4 and khhdon[3].upper() == "M"


def file_path(out: Path, inv: dict) -> Path:
    """<out>/<MST>/<năm>/<tháng>/<mẫu>_<ký hiệu>_<số>.xml"""
    return (out / inv["nbmst"] / str(inv["year"]) / f"{inv['month']:02d}"
            / f"{inv['khmshdon']}_{inv['khhdon']}_{inv['shdon']}.xml")


def download(tokens, inv: dict) -> bytes:
    """Tải ZIP 1 HĐ, 429 thì nghỉ rồi thử lại. Lỗi khác → GDTError."""
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return invoice_job.call_gdt(tokens, lambda tok: api_client.fetch_invoice_zip(
                tok, inv["nbmst"], inv["khhdon"], inv["shdon"], inv["khmshdon"],
                use_sco=is_mtt(inv["khhdon"])))
        except GDTError as e:
            if e.kind != "rate_limit" or wait is None:
                raise
            log.warning("GDT 429 → nghỉ %d s", wait)
            time.sleep(wait)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tải XML từ GDT cho danh sách HĐ thiếu XML (CSV)")
    ap.add_argument("--csv", required=True, type=Path, help="file CSV danh sách HĐ thiếu XML")
    ap.add_argument("--out", required=True, type=Path, help="thư mục lưu XML")
    ap.add_argument("--mst", help="chỉ tải HĐ của MST này")
    ap.add_argument("--year", type=int, help="chỉ tải HĐ của năm này")
    ap.add_argument("--month", help="chỉ tải HĐ của tháng này: 1–12 hoặc yyyy-mm")
    ap.add_argument("--limit", type=int, default=0, help="chỉ tải N HĐ (0 = tất cả)")
    ap.add_argument("--dry-run", action="store_true", help="chỉ đọc CSV, in số HĐ sẽ tải")
    args = ap.parse_args(argv)
    invoice_job._setup_logging()

    invoices = [to_invoice(r) for r in read_rows(args.csv)]
    try:
        picked = select(invoices, args.mst, args.year, args.month)
    except ValueError as e:
        log.error("%s", e)
        return 2
    todo = [inv for inv in picked if not file_path(args.out, inv).exists()]
    by_month = Counter((i["nbmst"], i["year"], i["month"]) for i in todo)
    log.info("CSV %d HĐ, khớp bộ lọc %d: đã có file %d, cần tải %d (MTT %d)", len(invoices), len(picked),
             len(picked) - len(todo), len(todo), sum(is_mtt(i["khhdon"]) for i in todo))
    for (mst, year, month), n in sorted(by_month.items()):
        log.info("  %s %d-%02d: %d HĐ", mst, year, month, n)
    if args.limit:
        todo = todo[:args.limit]
    if args.dry_run or not todo:
        return 0

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        log.error("Thiếu DATABASE_URL trong .env")
        return 2
    store = InvoiceStore(dsn)
    stats = Counter()
    args.out.mkdir(parents=True, exist_ok=True)
    result_path = args.out / RESULT_NAME
    new_file = not result_path.exists()
    try:
        companies = {c["company_mst"]: c for c in invoice_job._active_companies(store)}
        with open(result_path, "a", newline="", encoding="utf-8-sig") as f:
            out = csv.writer(f)
            if new_file:
                out.writerow(["thoi_gian", "mst", "nam", "thang", "mau", "ky_hieu", "so",
                              "ket_qua", "chi_tiet", "file"])
            for mst in sorted({i["nbmst"] for i in todo}):
                rows = [i for i in todo if i["nbmst"] == mst]
                if mst not in companies:
                    log.error("[%s] chưa có trong DB (invoice_job.py add-company) → bỏ %d HĐ", mst, len(rows))
                    stats["no_company"] += len(rows)
                    continue
                fetch_company(store, companies[mst], rows, args.out, out, f, stats)
    finally:
        store.close()
    log.info("xong: %s → %s", dict(stats), result_path)
    return 0 if stats["ok"] else 1


def fetch_company(store, company: dict, rows: list[dict], out_dir: Path, out, f, stats: Counter) -> None:
    """Tải XML cho các HĐ của 1 công ty; không đăng nhập được thì bỏ cả công ty."""
    mst = company["company_mst"]
    tokens = invoice_job.TokenProvider(store, company, stats)
    for n, inv in enumerate(rows, 1):
        status, detail, saved = "ok", "", ""
        try:
            xml = xml_parser.extract_xml(download(tokens, inv))
            target = file_path(out_dir, inv)
            invoice_job.atomic_write(target, xml)
            saved = target.relative_to(out_dir).as_posix()
        except invoice_job.LoginError as e:
            log.error("[%s] không đăng nhập được GDT, bỏ %d HĐ còn lại: %s", mst, len(rows) - n + 1, e)
            stats["login_error"] += len(rows) - n + 1
            return
        except GDTError as e:
            status, detail = e.kind, str(e)
        except ValueError as e:          # ZIP hỏng / không có XML
            status, detail = "bad_content", str(e)
        stats[status] += 1
        out.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), mst, inv["year"], inv["month"],
                      inv["khmshdon"], inv["khhdon"], inv["shdon"], status, detail, saved])
        f.flush()
        if n % 20 == 0 or n == len(rows):
            log.info("[%s] %d/%d: %s", mst, n, len(rows), dict(stats))
        time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
