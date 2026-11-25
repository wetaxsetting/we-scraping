"""
scripts/minvoice_batch.py
=========================
Tải PDF gốc của M-invoice cho các HĐ đã có **MST bán + mã tra cứu trong XML**, qua cổng tra cứu chung
`tracuuhoadon.minvoice.com.vn` (có reCAPTCHA v3, server kiểm token).

Khác FPT: `sec` của FPT chỉ có trong mail, còn M-invoice có sẵn `nbmst` + `invoice_xml.ma_tra_cuu`
("Số bảo mật"/"Mã tra cứu") → chạy hoàn toàn từ DB, không cần mail.

Cơ chế (đọc từ JS của cổng 25/11/2026):
    GET /api/Search/GetRecaptchaSiteKey                      → {"siteKey": "..."}
    grecaptcha.execute(siteKey, {action: "search_invoice"})  → token (sống ~2 phút, dùng 1 lần)
    GET /api/Search/SearchInvoiceCaptcha
        ?masothue=<MST bán>&sobaomat=<mã>&type=PDF&inchuyendoi=false
        header X-Recaptcha-Token: <token>                    → application/pdf | 400/500 JSON

Script mở **1 Chromium** (profile `minvoice_profile`, có giao diện — reCAPTCHA v3 chấm điểm theo phiên),
lấy token cho từng HĐ và lưu PDF vào:

    <INVOICE_OUTPUT_DIR>/manual/minvoice/<MST>_<mẫu>_<ký hiệu>_<số>.pdf

rồi (mặc định) đưa task PDF về hàng đợi. Adapter `minvoice` (`pdf_providers/minvoice.py`) đọc file này,
kiểm số HĐ trong PDF, ghi `invoice_pdf.source_id='minvoice'` — KHÔNG gọi cổng.

    python -X utf8 scripts/minvoice_batch.py --dry-run                 # liệt kê, không mở trình duyệt
    python -X utf8 scripts/minvoice_batch.py --limit 50 --import       # chạy 50 HĐ rồi nhập luôn
    python -X utf8 scripts/minvoice_batch.py --statuses dead,no_source
    python -X utf8 scripts/minvoice_batch.py --company 0108515678-001 --limit 10

Lưu ý:
- Chạy **có giao diện**; `--headless` dễ bị chấm điểm thấp (server từ chối token). Giữ profile để tích điểm.
- Nghỉ `--delay` giây giữa 2 HĐ (mặc định 3 s ±30%). Cổng có thể giới hạn tần suất / chặn IP nếu chạy dồn;
  chạy theo lô (--limit) thay vì cả 4.000 HĐ một lượt.
- Dừng tự động khi `--max-fail` HĐ lỗi liên tiếp (dấu hiệu bị chặn).
- JSON đã có PDF trong hộp chờ → bỏ qua (trừ `--force`). Task `dead` được đưa lại hàng đợi (trừ `--no-requeue`).
- Đường chính thức khi có tài khoản phát hành của người bán vẫn là `MINVOICE_USER_/PASSWORD_<MST>` (không captcha).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

log = logging.getLogger("minvoice_batch")

BASE = "https://tracuuhoadon.minvoice.com.vn"
PAGE = BASE + "/tra-cuu-hoa-don"
API = BASE + "/api/Search/SearchInvoiceCaptcha"
SITE_KEY_API = BASE + "/api/Search/GetRecaptchaSiteKey"
ACTION = "search_invoice"
MST_TCGPS = ("0106026495", "0106026495-001")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Job:
    invoice_id: int
    task_id: int
    nbmst: str
    khmshdon: int | str
    khhdon: str
    shdon: int
    code: str
    inv: dict          # dict cho adapter / local_name / check_pdf_is_invoice


def load_jobs(statuses: list[str], company: str | None, limit: int | None) -> list[Job]:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("Thiếu DATABASE_URL trong môi trường/.env.")
    sql = """
        SELECT i.id AS invoice_id, i.company_mst, i.nbmst, i.khmshdon, i.khhdon, i.shdon,
               i.ngay_lap, x.ma_tra_cuu, x.parsed -> 'tt_khac' AS tt_khac, t.id AS task_id
          FROM invoices i
          JOIN invoice_xml x ON x.invoice_id = i.id
          JOIN tasks t ON t.invoice_id = i.id AND t.task_type = 'pdf'
          LEFT JOIN invoice_pdf p ON p.invoice_id = i.id
         WHERE x.mst_tcgp = ANY(%s)
           AND p.invoice_id IS NULL
           AND t.status = ANY(%s)
    """
    params: list = [list(MST_TCGPS), list(statuses)]
    if company:
        sql += " AND i.company_mst = %s"
        params.append(company)
    sql += " ORDER BY i.ngay_lap DESC NULLS LAST, i.id DESC"      # mới nhất trước (kỳ đang làm)
    if limit:
        sql += " LIMIT %s"
        params.append(limit * 3)          # chừa chỗ cho HĐ không có mã (bị lọc bên dưới)

    from pdf_providers import minvoice

    jobs: list[Job] = []
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                inv = {"id": r["invoice_id"], "nbmst": r["nbmst"], "khmshdon": r["khmshdon"],
                       "khhdon": r["khhdon"], "shdon": r["shdon"], "ngay_lap": r["ngay_lap"],
                       "ma_tra_cuu": r["ma_tra_cuu"], "tt_khac": r["tt_khac"] or {}}
                code = minvoice.MinvoiceAdapter.lookup_code(inv)
                if not code:
                    continue
                jobs.append(Job(invoice_id=r["invoice_id"], task_id=r["task_id"], nbmst=r["nbmst"],
                                khmshdon=r["khmshdon"], khhdon=r["khhdon"], shdon=r["shdon"],
                                code=code, inv=inv))
                if limit and len(jobs) >= limit:
                    break
    return jobs


def file_name(job: Job) -> str:
    """Tên file dùng chung với adapter (`pdf_providers.minvoice.local_name`)."""
    from pdf_providers import minvoice

    return minvoice.local_name(job.inv)


def already_have(drop: Path, job: Job) -> bool:
    name = file_name(job)
    return (drop / name).exists() or (drop / "used" / name).exists()


def write_pdf(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


async def get_token(page, site_key: str) -> str:
    token = await page.evaluate(
        """async ({siteKey, action}) => await new Promise((resolve, reject) => {
               grecaptcha.ready(() => grecaptcha.execute(siteKey, {action}).then(resolve, reject));
           })""",
        {"siteKey": site_key, "action": ACTION})
    if not token:
        raise RuntimeError("reCAPTCHA không trả token")
    return token


async def fetch_pdf(context, page, site_key: str, job: Job, timeout: float) -> bytes:
    """Thử tối đa 2 lần: token v3 sống ngắn, đôi khi server từ chối token cũ."""
    params = {"masothue": job.nbmst, "sobaomat": job.code, "type": "PDF", "inchuyendoi": "false"}
    headers = {"Accept": "application/json, text/plain, */*", "Referer": PAGE}
    last = ""
    for attempt in range(2):
        headers["X-Recaptcha-Token"] = await get_token(page, site_key)
        r = await context.request.get(API, params=params, headers=headers, timeout=timeout * 1000)
        body = await r.body()
        if r.status == 200 and body.startswith(b"%PDF"):
            return body
        try:
            js = json.loads(body.decode("utf-8", "ignore"))
            msg = str((js or {}).get("message") or js or "")
        except ValueError:
            msg = body[:200].decode("utf-8", "ignore")
        last = f"HTTP {r.status}: {msg}"
        if "recaptcha" in msg.lower() and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        break
    raise RuntimeError(last)


async def run_browser(args, jobs: list[Job], drop: Path) -> tuple[int, int, list[int]]:
    from playwright.async_api import TimeoutError as PWTimeout, async_playwright
    from pdf_providers import common

    fetched = failed = consecutive = 0
    ok_tasks: list[int] = []
    profile = Path(args.profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=USER_AGENT,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            accept_downloads=False,
        )
        try:
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.pages[0] if context.pages else await context.new_page()
            log.info("Mở %s (profile %s)", PAGE, profile)
            await page.goto(PAGE, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_function(
                    "typeof window.grecaptcha === 'object' && typeof window.grecaptcha.ready === 'function'",
                    timeout=args.login_wait * 1000)
            except PWTimeout:
                log.warning("reCAPTCHA chưa nạp sau %ss — vẫn thử từng HĐ", args.login_wait)
            site = await page.evaluate(
                "async () => { const r = await fetch('/api/Search/GetRecaptchaSiteKey');"
                " return r.ok ? r.json() : null; }")
            site_key = (site or {}).get("siteKey") if isinstance(site, dict) else None
            if not site_key:
                raise RuntimeError("Không lấy được siteKey reCAPTCHA của M-invoice")
            log.info("siteKey %s…", site_key[:14])

            total = len(jobs)
            for i, job in enumerate(jobs, 1):
                if already_have(drop, job) and not args.force:
                    log.info("[%d/%d] bỏ qua %s (đã có PDF)", i, total, file_name(job))
                    continue
                try:
                    pdf = await fetch_pdf(context, page, site_key, job, args.timeout)
                    common.check_pdf_is_invoice(pdf, job.inv)      # chặn PDF của HĐ khác
                except Exception as e:  # noqa: BLE001  1 HĐ lỗi không dừng cả mẻ
                    failed += 1
                    consecutive += 1
                    log.warning("[%d/%d] LỖI %s: %s", i, total, file_name(job), e)
                    if consecutive >= args.max_fail:
                        log.error("%d HĐ lỗi liên tiếp → dừng (nghi bị chặn); chạy lại sau.", consecutive)
                        break
                    await asyncio.sleep(random.uniform(args.delay * 0.7, args.delay * 1.3))
                    continue
                write_pdf(drop / file_name(job), pdf)
                ok_tasks.append(job.task_id)
                fetched += 1
                consecutive = 0
                log.info("[%d/%d] OK   %s (%d KB)", i, total, file_name(job), len(pdf) // 1024)
                await asyncio.sleep(random.uniform(args.delay * 0.7, args.delay * 1.3))
        finally:
            await context.close()
    return fetched, failed, ok_tasks


def requeue(task_ids: list[int]) -> int:
    if not task_ids:
        return 0
    import invoice_store

    store = invoice_store.InvoiceStore(os.environ["DATABASE_URL"])
    return store.requeue_tasks(task_ids)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Tải PDF M-invoice qua cổng tra cứu (reCAPTCHA v3) cho HĐ có MST + mã trong XML",
        epilog="Sau khi chạy: python -X utf8 web/invoice_job.py pdf (hoặc worker PDF tự nhặt).")
    ap.add_argument("--statuses", default="dead,no_source",
                    help="trạng thái task PDF sẽ chạy (mặc định dead,no_source; tránh task worker đang giữ)")
    ap.add_argument("--company", metavar="MST", help="chỉ HĐ của 1 công ty")
    ap.add_argument("--limit", type=int, help="số HĐ tối đa trong lượt này")
    ap.add_argument("--out", metavar="DIR", help="thư mục hộp chờ (mặc định <INVOICE_OUTPUT_DIR>/manual/minvoice)")
    ap.add_argument("--profile", default=str(ROOT / "minvoice_profile"),
                    help="thư mục profile Chromium (mặc định <dự án>/minvoice_profile)")
    ap.add_argument("--headless", action="store_true",
                    help="chạy ẩn (reCAPTCHA v3 dễ bị chấm thấp — chỉ để thử)")
    ap.add_argument("--timeout", type=float, default=60.0, help="giây chờ mỗi request (mặc định 60)")
    ap.add_argument("--login-wait", type=float, default=20.0,
                    help="giây chờ reCAPTCHA nạp (mặc định 20; tăng nếu mạng chậm)")
    ap.add_argument("--delay", type=float, default=3.0, help="giây nghỉ giữa 2 HĐ (mặc định 3 ±30%%)")
    ap.add_argument("--max-fail", type=int, default=8, help="dừng khi N HĐ lỗi liên tiếp (mặc định 8)")
    ap.add_argument("--force", action="store_true", help="tải lại cả HĐ đã có PDF trong hộp chờ")
    ap.add_argument("--no-requeue", action="store_true", help="không đưa task đã có PDF về hàng đợi")
    ap.add_argument("--import", dest="import_after", action="store_true",
                    help="chạy luôn web/invoice_job.py pdf sau khi tải xong")
    ap.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không mở trình duyệt")
    ap.add_argument("--log", metavar="FILE", help="ghi log ra file (mặc định chỉ ra màn hình)")
    return ap.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    jobs = load_jobs(statuses, args.company, args.limit)
    drop = Path(args.out).expanduser().resolve() if args.out else Path(
        os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "manual" / "minvoice"

    if not jobs:
        log.info("Không có HĐ M-invoice nào cần chạy (status: %s)", args.statuses)
        return 0

    log.info("%d HĐ sẽ chạy → %s", len(jobs), drop)
    if args.dry_run:
        for i, job in enumerate(jobs, 1):
            state = "đã có, bỏ qua" if already_have(drop, job) and not args.force else "sẽ tải"
            print(f"{i:4d}. {job.nbmst} {job.khmshdon}/{job.khhdon}/{job.shdon}  "
                  f"code={job.code}  [{state}]")
        print(f"\nTổng {len(jobs)} HĐ. Bỏ --dry-run để chạy.")
        return 0

    drop.mkdir(parents=True, exist_ok=True)
    try:
        fetched, failed, ok_tasks = asyncio.run(run_browser(args, jobs, drop))
    except ImportError:
        log.error("Chưa có Playwright: pip install -r requirements-invoice.txt && playwright install chromium")
        return 2
    except Exception as e:  # noqa: BLE001  profile bị khóa, không lấy được siteKey…
        log.error("Không chạy được trình duyệt: %s", e)
        return 2

    if ok_tasks and not args.no_requeue:
        n = requeue(ok_tasks)
        log.info("Đưa %d task về hàng đợi PDF", n)

    log.info("Xong: lấy %d, lỗi %d, bỏ qua %d → %s", fetched, failed,
             len(jobs) - fetched - failed, drop)
    if fetched:
        if args.import_after:
            log.info("Chạy invoice_job.py pdf …")
            subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "web" / "invoice_job.py"), "pdf"],
                           cwd=ROOT, check=False)
        else:
            print("Bước tiếp: python -X utf8 web/invoice_job.py pdf")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
