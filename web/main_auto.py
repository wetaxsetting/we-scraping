# main_auto.py
"""
Lấy hóa đơn GDT theo KHOẢNG NGÀY tự chọn rồi ghi thẳng vào DB — chạy tay 1 lần
(job định kỳ thì dùng web/invoice_job.py tick).

Dùng đúng luồng của job: danh sách → bảng invoices (HĐ đã có thì bỏ qua) → tải XML →
invoice_xml + invoice_details + partners, file XML/ZIP lưu theo MST công ty
(output/xml/<MST>/<năm>/<tháng>/<MST bán>/...). Dùng lại JWT đã lưu trong DB, chỉ giải
captcha khi cần. Không còn ghi hoa_don.json / hoa_don_chitiet.json.

Cần: công ty đã có trong DB (invoice_job.py add-company), mật khẩu HDT_PASSWORD_<MST> trong .env.

Usage:
    python -X utf8 web/main_auto.py --from 2026-08-01 --to 2026-09-30
    python -X utf8 web/main_auto.py --from 2026-09-01 --to 2026-09-30 --type mua-all --pdf
    python -X utf8 web/main_auto.py --from 2026-09-01 --to 2026-09-30 --excel   # xuất thêm Excel từ DB
    python -X utf8 web/main_auto.py --from 2023-01-01 --to 2026-10-26 --type mua-all   # khoảng dài

GDT chỉ cho tìm tối đa 30 ngày mỗi lần → khoảng dài tự chia đoạn ≤ 30 ngày (`--chunk-days`),
mỗi đoạn lấy danh sách rồi tải XML luôn. Đoạn nào lỗi được in ra cuối lượt để chạy lại riêng.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import invoice_job                     # web/invoice_job.py (cùng thư mục)
from invoice_job import api_client, job_policy, log

TYPES = ["all", "ban", "mua", "ban-all", "mua-all"]


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def split_range(start: date, end: date, days: int = job_policy.MAX_SPAN_DAYS) -> list:
    """[start, end] → các đoạn liên tiếp (từ cũ đến mới), mỗi đoạn ≤ `days` ngày tính cả 2 đầu.
    GDT chỉ cho tìm tối đa 30 ngày mỗi lần nên `days` bị chặn ở MAX_SPAN_DAYS."""
    days = max(1, min(days, job_policy.MAX_SPAN_DAYS))
    return job_policy.chunk_range(start, end, days)


def _pick_company(store, mst: str | None) -> dict | None:
    companies = store.active_companies()
    if mst:
        return next((c for c in companies if c["company_mst"] == mst), None)
    return companies[0] if len(companies) == 1 else None


def export_excel(store, mst: str, start: date, end: date, path: Path) -> int:
    import pandas as pd
    rows = store.invoices_between(mst, start, end)
    cols = {"ngay_lap": "Ngày lập", "direction": "Chiều", "khmshdon": "Mẫu số", "khhdon": "Ký hiệu",
            "shdon": "Số HĐ", "nbmst": "MST người bán", "nbten": "Người bán", "nmmst": "MST người mua",
            "nmten": "Người mua", "tien_chua_thue": "Tiền chưa thuế", "tien_thue": "Tiền thuế",
            "tong_tien": "Tổng tiền TT", "loai_hd": "Loại", "has_xml": "Có XML", "has_pdf": "Có PDF"}
    df = pd.DataFrame([{label: r.get(k) for k, label in cols.items()} for r in rows])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(path, index=False)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Lấy hóa đơn GDT theo khoảng ngày → DB")
    ap.add_argument("--company", help="MST công ty (bỏ trống nếu DB chỉ có 1 công ty)")
    ap.add_argument("--from", dest="from_date", required=True, type=_parse_date, help="Từ ngày yyyy-mm-dd")
    ap.add_argument("--to", dest="to_date", type=_parse_date, default=None,
                    help="Đến ngày yyyy-mm-dd (mặc định hôm nay)")
    ap.add_argument("--type", dest="invoice_type", default="all", choices=TYPES,
                    help="all = mua-all + ban-all (mặc định)")
    ap.add_argument("--chunk-days", type=int, default=job_policy.MAX_SPAN_DAYS,
                    help="số ngày mỗi đoạn (tối đa %d — giới hạn của GDT)" % job_policy.MAX_SPAN_DAYS)
    ap.add_argument("--xml-limit", type=int, default=10000, help="số XML tối đa tải trong lượt này")
    ap.add_argument("--pdf", action="store_true", help="chạy luôn hàng đợi PDF sau khi lấy XML")
    ap.add_argument("--excel", action="store_true", help="xuất danh sách HĐ trong khoảng ngày ra Excel (từ DB)")
    args = ap.parse_args(argv)

    invoice_job._setup_logging()
    end = args.to_date or invoice_job.today_vn()
    if args.from_date > end:
        log.error("--from (%s) sau --to (%s)", args.from_date, end)
        return 2
    dsn = invoice_job.os.getenv("DATABASE_URL")
    if not dsn:
        log.error("Thiếu DATABASE_URL trong .env")
        return 2

    store = invoice_job.InvoiceStore(dsn)
    try:
        company = _pick_company(store, args.company)
        if company is None:
            log.error("Không xác định được công ty: thêm --company <MST> "
                      "(công ty phải có trong DB: invoice_job.py add-company)")
            return 2
        if not store.try_lock():
            log.info("Đang có lượt chạy khác (tick/sweep/pdf) → thử lại sau")
            return 0
        try:
            return _run(store, company, args, end)
        finally:
            store.unlock()
    finally:
        store.close()


def _run_chunks(store, company, tokens, breaker, run_id, sub_types, chunks, xml_limit, stats, out_dir) -> list:
    """Từng đoạn ≤ 30 ngày: lấy danh sách đoạn đó → tải XML ngay, rồi mới sang đoạn sau.
    Khoảng dài (vài năm) vì vậy có XML dần dần thay vì chờ hết danh sách, và 1 đoạn lỗi chỉ
    bỏ đoạn đó. Trả về các đoạn lấy danh sách lỗi."""
    mst = company["company_mst"]
    failed = []
    for i, (start, end) in enumerate(chunks, 1):
        if breaker.open:
            log.warning("[%s] GDT lỗi liên tiếp → dừng, còn %d đoạn chưa chạy (từ %s)",
                        mst, len(chunks) - i + 1, start)
            failed.extend(chunks[i - 1:])
            break
        log.info("[%s] Đoạn %d/%d: %s → %s", mst, i, len(chunks), start, end)
        errors_before = stats["sync_errors"]
        try:
            # move_cursor=False: chạy tay theo khoảng ngày không làm lệch mốc đồng bộ của tick
            invoice_job.sync_lists(store, company, tokens, sub_types,
                                   lambda _st, s=start, e=end: [(s, e)],
                                   breaker, stats, move_cursor=False)
            remaining = xml_limit - stats["xml_ok"] - stats["xml_failed"]
            if remaining > 0:
                invoice_job.process_xml(store, company, tokens, breaker, run_id, remaining, stats, out_dir)
        except invoice_job.LoginError as e:
            stats["login_error"] = str(e)
            log.error("[%s] %s", mst, e)
            failed.extend(chunks[i - 1:])       # không đăng nhập được thì các đoạn sau cũng vậy
            break
        if stats["sync_errors"] > errors_before:
            failed.append((start, end))
    return failed


def _run(store, company: dict, args, end: date) -> int:
    mst = company["company_mst"]
    types = ["mua-all", "ban-all"] if args.invoice_type == "all" else [args.invoice_type]
    sub_types = api_client.expand_invoice_types(types)
    out_dir = invoice_job.output_dir()
    stats = Counter()
    run_id = store.start_run("manual")
    store.begin_run_company(run_id, mst)
    status = "ok"
    try:
        tokens = invoice_job.TokenProvider(store, company, stats)
        breaker = job_policy.CircuitBreaker()
        chunks = split_range(args.from_date, end, args.chunk_days)
        log.info("[%s] Lấy HĐ %s → %s (%s): %d đoạn", mst, args.from_date, end, ", ".join(types), len(chunks))
        failed = _run_chunks(store, company, tokens, breaker, run_id, sub_types, chunks,
                             args.xml_limit, stats, out_dir)
        if failed:
            log.warning("[%s] %d đoạn lấy danh sách lỗi, chạy lại riêng từng đoạn: %s", mst, len(failed),
                        "; ".join(f"--from {s} --to {e}" for s, e in failed))
            stats["failed_chunks"] = [f"{s}→{e}" for s, e in failed]
        if args.pdf:
            invoice_job._prepare_queues(store)
            invoice_job.process_pdf(store, company, run_id, args.xml_limit, stats, out_dir)
        if stats["sync_errors"] or "login_error" in stats or breaker.open:
            status = "partial"
        if args.excel:
            path = out_dir / f"hoa_don_{mst}_{args.from_date:%Y%m%d}_{end:%Y%m%d}.xlsx"
            n = export_excel(store, mst, args.from_date, end, path)
            log.info("Xuất Excel %d HĐ → %s", n, path)
    except Exception:
        status = "error"
        raise
    finally:
        store.finish_run(run_id, status, {"company": mst, "from": str(args.from_date), "to": str(end),
                                          "types": types, "stats": dict(stats)})
        store.finish_run_company(run_id, mst, status, dict(stats))
    log.info("[%s] xong: %d HĐ trên GDT, %d HĐ mới vào DB, %d XML, %d XML lỗi",
             mst, stats["listed"], stats["new_invoices"], stats["xml_ok"], stats["xml_failed"])
    print("\nXem tình trạng: python -X utf8 web/invoice_job.py status")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
