"""
scripts/batch_all.py
====================
Chạy **tuần tự** 2 script tải PDF bằng trình duyệt (FPT, M-Invoice) trong 1 tiến trình / 1 task Scheduler.

    python -X utf8 scripts/batch_all.py
    python -X utf8 scripts/batch_all.py --fpt-limit 10 --minvoice-limit 100
    python -X utf8 scripts/batch_all.py --only minvoice
    python -X utf8 scripts/batch_all.py --dry-run

Mỗi script con tự: đọc DB → mở Chromium (profile riêng `fpt_profile` / `minvoice_profile`) → lưu file vào
`output/manual/*` → requeue đúng HĐ vừa lấy. Worker PDF nhặt file trong ≤ 60 s.

Vì sao tuần tự: không mở 2 Chromium cùng lúc (nhẹ RAM/CPU, không tranh nhau); script con không có việc thì
thoát trong 1–2 s (chỉ query DB), nên chạy cả hai mỗi lượt gần như không tốn gì.

Mã thoát: 0 = không lỗi; 1 = có HĐ lỗi (partial — giống `sync-lists`); 2 = không chạy được
(thiếu Playwright / không mở được trình duyệt / thiếu DB). Task Scheduler sẽ thấy 0x1 / 0x2.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

log = logging.getLogger("batch_all")

STEPS = {"fpt": ("fpt_batch.py", "FPT"), "minvoice": ("minvoice_batch.py", "M-Invoice")}


def default_log_dir() -> Path:
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "logs"


def build_cmd(step: str, limit: int, log_dir: Path) -> list[str]:
    script, _ = STEPS[step]
    return [sys.executable, "-X", "utf8", str(SCRIPTS / script),
            "--limit", str(limit), "--log", str(Path(log_dir) / f"{Path(script).stem}.log")]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Chạy tuần tự batch FPT rồi M-Invoice (1 task)")
    ap.add_argument("--only", choices=("both", "fpt", "minvoice"), default="both")
    ap.add_argument("--fpt-limit", type=int, default=20, help="số HĐ FPT mỗi lượt (mặc định 20)")
    ap.add_argument("--minvoice-limit", type=int, default=50, help="số HĐ M-Invoice mỗi lượt (mặc định 50)")
    ap.add_argument("--log-dir", metavar="DIR", help="thư mục log của 2 script con (mặc định <out>/logs)")
    ap.add_argument("--dry-run", action="store_true", help="chỉ in lệnh, không chạy")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler()],
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    steps = [("fpt", args.fpt_limit), ("minvoice", args.minvoice_limit)]
    if args.only != "both":
        steps = [s for s in steps if s[0] == args.only]

    codes: list[tuple[str, int]] = []
    for step, limit in steps:
        title = STEPS[step][1]
        if limit < 1:
            log.warning("%s: limit %d < 1 → bỏ qua", title, limit)
            continue
        cmd = build_cmd(step, limit, log_dir)
        log.info("→ %s: limit %d", title, limit)
        log.debug("  %s", " ".join(cmd))
        if args.dry_run:
            print(" ".join(cmd))
            continue
        try:
            rc = subprocess.run(cmd, cwd=ROOT).returncode
        except OSError as e:                       # không gọi được python, file thiếu…
            log.error("%s: không chạy được: %s", title, e)
            rc = 2
        codes.append((title, rc))
        log.info("← %s: exit %d", title, rc)
        if rc == 2:
            log.error("%s không chạy được (exit 2) — xem log %s", title, log_dir)
        elif rc == 1:
            log.warning("%s xong nhưng có HĐ lỗi (exit 1)", title)

    if args.dry_run:
        return 0
    if any(rc == 2 for _, rc in codes):
        return 2
    if any(rc == 1 for _, rc in codes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
