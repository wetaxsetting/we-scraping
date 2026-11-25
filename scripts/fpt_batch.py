"""
scripts/fpt_batch.py
====================
Tải JSON `GET /api/search` của FPT eInvoice cho các link tra cứu trong mail, thả vào hộp chờ của adapter `fpt`.

Cổng `tracuuhoadon.fpt.com.vn` có reCAPTCHA v3 và **server kiểm token**, nên job không tự tra:
`pdf_providers/fpt_import.py` chỉ nhập JSON đã có trong `<INVOICE_OUTPUT_DIR>/manual/fpt/*.json`.
Script này là mắt nối còn thiếu:

    mail (link ?mst=&sec=)  →  fpt_batch.py (bắt /api/search)  →  output/manual/fpt/*.json
                            →  web/invoice_job.py pdf (hoặc worker PDF)  →  invoice_pdf.source_id='fpt'

`sec` chỉ có trong mail — XML Thuế không có (kiểm 25/11: 0/1.732 XML FPT có mã/link tra cứu), nên không có
đường nào đi thẳng từ XML sang cổng FPT.

Lệnh:
    python -X utf8 scripts/fpt_batch.py --dry-run              # liệt kê link sẽ chạy, không mở trình duyệt
    python -X utf8 scripts/fpt_batch.py                        # link 'new' trong email_links
    python -X utf8 scripts/fpt_batch.py --limit 10 --import    # chạy 10 link rồi gọi luôn invoice_job.py pdf
    python -X utf8 scripts/fpt_batch.py --statuses new,mail_file
    python -X utf8 scripts/fpt_batch.py --url "https://tracuuhoadon.fpt.com.vn/?mst=2300680991&sec=xxxx"
    python -X utf8 scripts/fpt_batch.py --urls-file links.txt

Lưu ý:
- reCAPTCHA v3 chấm điểm theo phiên → giữ profile `fpt_profile` và chạy có giao diện. `--headless` chỉ để thử:
  điểm thấp thì server từ chối, khi đó không có response `/api/search` (báo timeout, không ghi JSON).
- Không chạy 2 tiến trình cùng lúc với cùng profile (Chromium khóa thư mục profile).
- 1 link = 1 HĐ; link trùng `(mst, sec)` được gộp. JSON đã có thì bỏ qua (trừ `--force`).
- JSON không hợp lệ (thiếu `doc`/`pdf`, PDF hỏng) được cất ở `output/manual/fpt/_invalid/` để soi, KHÔNG vào
  hộp chờ (adapter chỉ quét `*.json` ở cấp ngoài).
- Lưu JSON xong, script tự đưa task PDF của **đúng HĐ đó** về hàng đợi (worker PDF đang chạy nhặt
  ngay, kể cả hộp chờ rỗng); `--no-requeue` để tắt. `--import` chỉ cần khi muốn chạy hàng đợi ngay.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

log = logging.getLogger("fpt_batch")

BASE_URL = "https://tracuuhoadon.fpt.com.vn/"
SEARCH_API = "/api/search"
HOST_MARK = "tracuuhoadon.fpt.com.vn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Job:
    mst: str
    sec: str
    url: str
    link_ids: list[int] = field(default_factory=list)


def default_drop_dir() -> Path:
    """Cùng công thức với `pdf_providers.fpt_import.drop_dir()` (không import pdf_providers cho nhẹ)."""
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "manual" / "fpt"


def parse_link(url: str) -> tuple[str, str] | None:
    """Link FPT → (mst, sec). Không đủ tham số thì None."""
    url = html.unescape(url or "").strip()
    if not url:
        return None
    qs = parse_qs(urlsplit(url).query)
    mst = (qs.get("mst") or [""])[0].strip()
    sec = (qs.get("sec") or [""])[0].strip()
    if not mst or not sec:
        return None
    return mst, sec


def group_links(rows) -> list[Job]:
    """[(link_id, url)] → Job, gộp link trùng (mst, sec), giữ thứ tự."""
    jobs: list[Job] = []
    seen: dict[tuple[str, str], int] = {}
    for link_id, url in rows:
        parsed = parse_link(url)
        if parsed is None:
            log.debug("Bỏ qua link không có mst/sec: %s", url)
            continue
        key = parsed
        if key in seen:
            jobs[seen[key]].link_ids.append(link_id)
            continue
        seen[key] = len(jobs)
        jobs.append(Job(mst=parsed[0], sec=parsed[1], url=html.unescape(url.strip()), link_ids=[link_id]))
    return jobs


def load_from_db(statuses: list[str]) -> list[tuple[int, str]]:
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("Thiếu DATABASE_URL trong môi trường/.env — hoặc truyền --url/--urls-file.")
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, url FROM email_links
                    WHERE (host ILIKE %s OR url ILIKE %s)
                      AND url ILIKE %s
                      AND status = ANY(%s)
                    ORDER BY id""",
                (f"%{HOST_MARK}%", f"%{HOST_MARK}%", "%sec=%", list(statuses)),
            )
            return [(int(r[0]), r[1]) for r in cur.fetchall()]


def mark_done(link_ids: list[int]) -> int:
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn or not link_ids:
        return 0
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE email_links SET status='done', updated_at=now()
                    WHERE id = ANY(%s) AND status = 'new'""",
                (list(link_ids),),
            )
            return cur.rowcount


def check_payload(data) -> str:
    """Kiểm JSON /api/search đủ thứ adapter cần. Trả mô tả HĐ; ném ValueError nếu hỏng."""
    if not isinstance(data, dict):
        raise ValueError("response không phải JSON object")
    doc = data.get("doc")
    if not isinstance(doc, dict):
        keys = ", ".join(sorted(data)[:8]) or "không có khóa nào"
        raise ValueError(f"JSON không có 'doc' (khóa: {keys})")
    missing = [k for k in ("stax", "form", "serial", "seq") if not str(doc.get(k) or "").strip()]
    if missing:
        raise ValueError(f"doc thiếu {', '.join(missing)}")
    raw = str(data.get("pdf") or "")
    if not raw:
        raise ValueError("JSON không có 'pdf'")
    raw = raw.split(",", 1)[-1].strip()
    try:
        pdf = base64.b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"'pdf' base64 hỏng: {e}") from e
    if not pdf.startswith(b"%PDF"):
        raise ValueError(f"'pdf' không phải PDF ({len(pdf)} bytes)")
    return f"MST bán {doc.get('stax')} · {doc.get('form')}/{doc.get('serial')}/{doc.get('seq')}"


def already_have(drop: Path, target: Path) -> bool:
    """JSON còn trong hộp chờ, hoặc đã nhập xong → adapter chuyển sang `used/`."""
    return target.exists() or (drop / "used" / target.name).exists()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def fetch_payload(page, url: str, timeout: float) -> dict:
    """/api/search trả về gì lấy nguyên cái đó (adapter đọc `doc` + `pdf`)."""
    from playwright.async_api import TimeoutError as PWTimeout

    async with page.expect_response(lambda r: SEARCH_API in r.url, timeout=timeout * 1000) as info:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except PWTimeout:
            pass                              # trang chậm nhưng request có thể đã bắn — vẫn chờ response
    resp = await info.value
    if resp.status != 200:
        raise RuntimeError(f"{SEARCH_API} trả HTTP {resp.status}")
    try:
        return await resp.json()
    except Exception:  # noqa: BLE001  đôi khi content-type sai
        return json.loads(await resp.text())


async def run_browser(args, jobs: list[Job], drop: Path) -> tuple[int, int, list[int], list[tuple]]:
    """Chạy lần lượt từng link. Trả (số lấy được, số lỗi, link_id lấy được, khóa HĐ đã lấy)."""
    from playwright.async_api import TimeoutError as PWTimeout, async_playwright

    fetched = failed = 0
    ok_ids: list[int] = []
    saved_keys: list[tuple] = []
    invalid_dir = drop / "_invalid"
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
            page.set_default_timeout(args.timeout * 1000)

            log.info("Mở %s (profile %s)", BASE_URL, profile)
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
            except PWTimeout:
                log.warning("Trang chủ FPT không tải xong sau 60s — vẫn thử từng link")

            # Trang công khai; nếu có lớp chặn (consent/robot) thì chờ người bấm, tối đa --login-wait.
            try:
                await page.wait_for_selector('input[placeholder="MST bên bán"]',
                                             timeout=args.login_wait * 1000)
            except PWTimeout:
                log.warning("Không thấy form tra cứu sau %ss — vẫn thử từng link", args.login_wait)

            total = len(jobs)
            for i, job in enumerate(jobs, 1):
                target = drop / f"{job.mst}_{job.sec}.json"
                if not args.force and already_have(drop, target):
                    log.info("[%d/%d] bỏ qua %s (đã có/nhập %s)", i, total, job.mst, target.name)
                    continue
                tag = f"{job.mst}/{job.sec}"
                data = None
                try:
                    data = await fetch_payload(page, job.url, args.timeout)
                    desc = check_payload(data)
                except Exception as e:  # noqa: BLE001  1 link lỗi không dừng cả mẻ
                    log.warning("[%d/%d] LỖI %s: %s", i, total, tag, e)
                    failed += 1
                    # response sai vẫn giữ lại để soi — nằm ngoài tầm quét *.json của adapter
                    if isinstance(e, ValueError) and data is not None:
                        write_json(invalid_dir / f"{job.mst}_{job.sec}.json", data)
                    data = None
                    await asyncio.sleep(args.delay)
                    continue
                write_json(target, data)
                ok_ids.extend(job.link_ids)
                doc = data.get("doc") or {}
                saved_keys.append((str(doc.get("stax") or "").strip(),
                                   str(doc.get("form") or "").strip(),
                                   str(doc.get("serial") or "").strip(),
                                   str(doc.get("seq") or "").strip()))
                fetched += 1
                log.info("[%d/%d] OK   %s → %s (%d KB)", i, total, desc, target.name,
                         target.stat().st_size // 1024)
                await asyncio.sleep(args.delay)
        finally:
            await context.close()
    return fetched, failed, ok_ids, saved_keys


def requeue_keys(keys: list[tuple]) -> tuple[int, int]:
    """JSON vừa lấy → requeue đúng task PDF của HĐ đó, worker nhặt được dù hộp chờ đang rỗng."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        log.warning("Không có DATABASE_URL → không requeue task (JSON vẫn nằm trong hộp chờ)")
        return 0, 0
    import psycopg2

    task_ids: set[int] = set()
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for stax, form, serial, seq in keys:
                try:
                    shdon = int(str(seq).strip())
                except ValueError:
                    continue
                try:
                    cond, form_val = "i.khmshdon = %s", int(str(form).strip())
                except ValueError:
                    cond, form_val = "i.khmshdon::text = %s", str(form).strip()
                cur.execute(f"""
                    SELECT t.id FROM tasks t JOIN invoices i ON i.id = t.invoice_id
                     WHERE t.task_type = 'pdf' AND i.nbmst = %s AND {cond}
                       AND upper(i.khhdon) = upper(%s) AND i.shdon = %s
                """, (stax, form_val, serial, shdon))
                task_ids.update(r[0] for r in cur.fetchall())
    if not task_ids:
        return 0, 0
    import invoice_store

    store = invoice_store.InvoiceStore(dsn)
    return len(task_ids), store.requeue_tasks(sorted(task_ids))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Tải JSON /api/search của FPT eInvoice cho link mail → output/manual/fpt/",
        epilog="Sau khi chạy: python -X utf8 web/invoice_job.py pdf (hoặc worker PDF tự nhặt).")
    ap.add_argument("--url", action="append", default=[], metavar="URL",
                    help="link FPT đầy đủ (?mst=&sec=) — lặp lại được; có --url thì không đọc DB")
    ap.add_argument("--urls-file", metavar="FILE", help="file text, mỗi dòng 1 link FPT")
    ap.add_argument("--statuses", default="new",
                    help="trạng thái email_links sẽ lấy (mặc định: new; vd new,mail_file)")
    ap.add_argument("--limit", type=int, help="số HĐ tối đa trong lượt này")
    ap.add_argument("--out", metavar="DIR", help="thư mục hộp chờ (mặc định <INVOICE_OUTPUT_DIR>/manual/fpt)")
    ap.add_argument("--profile", default=str(ROOT / "fpt_profile"),
                    help="thư mục profile Chromium (mặc định <dự án>/fpt_profile)")
    ap.add_argument("--headless", action="store_true",
                    help="chạy ẩn (reCAPTCHA v3 dễ bị chấm thấp — chỉ để thử)")
    ap.add_argument("--timeout", type=float, default=60.0, help="giây chờ /api/search mỗi link (mặc định 60)")
    ap.add_argument("--login-wait", type=float, default=10.0,
                    help="giây chờ form tra cứu lúc mở trang (mặc định 10; tăng nếu cần bấm tay)")
    ap.add_argument("--delay", type=float, default=1.5, help="giây nghỉ giữa 2 link (mặc định 1,5)")
    ap.add_argument("--force", action="store_true", help="ghi đè JSON đã có")
    ap.add_argument("--mark-done", action="store_true",
                    help="đánh dấu email_links đã chạy là 'done' (mặc định để 'new')")
    ap.add_argument("--no-requeue", action="store_true",
                    help="không đưa task PDF của HĐ vừa lấy về hàng đợi (mặc định đưa)")
    ap.add_argument("--import", dest="import_after", action="store_true",
                    help="chạy luôn web/invoice_job.py pdf sau khi tải xong")
    ap.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không mở trình duyệt")
    ap.add_argument("--log", metavar="FILE", help="ghi log ra file (mặc định chỉ ra màn hình)")
    return ap.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  console cũ không cho reconfigure
        pass
    args = parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    urls = list(args.url)
    if args.urls_file:
        text = Path(args.urls_file).read_text(encoding="utf-8")
        urls.extend(line.strip() for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))

    if urls:
        rows = [(0, u) for u in urls]
    else:
        statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
        rows = load_from_db(statuses)

    jobs = group_links(rows)
    if args.limit is not None:
        jobs = jobs[: max(args.limit, 0)]
    drop = Path(args.out).expanduser().resolve() if args.out else default_drop_dir()

    if not jobs:
        log.info("Không có link FPT nào cần chạy (status: %s)", args.statuses)
        return 0

    log.info("%d HĐ sẽ chạy → %s", len(jobs), drop)
    if args.dry_run:
        for i, job in enumerate(jobs, 1):
            target = drop / f"{job.mst}_{job.sec}.json"
            state = "đã có/đã nhập, bỏ qua" if already_have(drop, target) and not args.force else "sẽ tải"
            print(f"{i:3d}. {job.mst}  sec={job.sec}  [{state}]  links={len(job.link_ids)}")
        print(f"\nTổng {len(jobs)} HĐ. Bỏ --dry-run để chạy.")
        return 0

    drop.mkdir(parents=True, exist_ok=True)
    try:
        fetched, failed, ok_ids, saved_keys = asyncio.run(run_browser(args, jobs, drop))
    except ImportError:
        log.error("Chưa có Playwright: pip install -r requirements-invoice.txt && playwright install chromium")
        return 2
    except Exception as e:  # noqa: BLE001  profile bị khóa, Chromium không mở được…
        log.error("Không chạy được trình duyệt: %s", e)
        return 2

    if args.mark_done and ok_ids:
        n = mark_done(ok_ids)
        log.info("Đánh dấu 'done' %d link", n)

    if saved_keys and not args.no_requeue:
        matched, n = requeue_keys(saved_keys)
        log.info("Khớp %d HĐ trong DB → đưa %d task về hàng đợi PDF", matched, n)

    log.info("Xong: lấy %d, lỗi %d, bỏ qua %d → %s",
             fetched, failed, len(jobs) - fetched - failed, drop)
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
