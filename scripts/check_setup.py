"""Check a fresh install before pointing the job at real tax accounts.

Run this on a new machine after installing the dependencies and before
``init-db``. Everything it checks fails in a way that is hard to read later:
a model left as a Git LFS pointer loads as a corrupt checkpoint rather than a
missing file, and a missing Playwright browser only surfaces when an invoice
from one particular provider comes through, days later.

    py -X utf8 scripts\\check_setup.py

Never prints secret values -- only whether a key is set.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# group -> what breaks if it is missing
PROD_MODELS = {
    "g_600x120": "dang nhap GDT (bat buoc)",
    "g_150x34": "adapter easyinvoice",
    "g_80x30": "adapter vnpt",
    "g_1500x350": "adapter visnam",
    "g_300x75": "adapter xcyber (CyberLotus)",
    "g_200x70": "adapter thaison (Thai Son)",
    "g_120x38": "adapter megabiz",
    "g_194x35": "adapter pvoil",
    "g_120x40_cloud": "adapter cloudteam (Nhom May)",
}
PACKAGES = ["torch", "numpy", "cv2", "PIL", "yaml", "requests", "psycopg2",
            "dotenv", "openpyxl", "fitz", "playwright", "skia", "ddddocr"]
ENV_REQUIRED = ["DATABASE_URL"]
ENV_OPTIONAL = ["INVOICE_MAIL_USER", "INVOICE_MAIL_AUTH", "INVOICE_OUTPUT_DIR",
                "INVOICE_LIST_SOURCE"]

fails: list[str] = []
warns: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  LOI   {msg}")
    fails.append(f"{msg}" + (f"\n          -> {fix}" if fix else ""))


def warn(msg: str, fix: str = "") -> None:
    print(f"  canh bao {msg}")
    warns.append(f"{msg}" + (f"\n          -> {fix}" if fix else ""))


print("\n[1] Python")
v = sys.version_info
if (v.major, v.minor) == (3, 13):
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
else:
    warn(f"Python {v.major}.{v.minor}.{v.micro}, du an chay tren 3.13",
         "ddddocr / skia-python co the khong co wheel cho ban nay")

print("\n[2] Thu vien")
missing = []
for mod in PACKAGES:
    try:
        importlib.import_module(mod)
    except Exception as exc:                       # noqa: BLE001
        missing.append(f"{mod} ({type(exc).__name__})")
if missing:
    bad("thieu: " + ", ".join(missing),
        "py -m pip install -r requirements.txt -r requirements-invoice.txt")
else:
    ok(f"{len(PACKAGES)} goi import duoc")

print("\n[3] Model captcha (Git LFS)")
for gid, use in PROD_MODELS.items():
    p = ROOT / "runs" / gid / "best.pt"
    if not p.exists():
        bad(f"{gid}: khong co {p.relative_to(ROOT)}  [{use}]",
            "git lfs install && git lfs pull")
        continue
    size = p.stat().st_size
    head = p.read_bytes()[:40]
    if head.startswith(b"version https://git-lfs"):
        bad(f"{gid}: moi la con tro LFS, chua tai ve ({size} byte)  [{use}]",
            "git lfs install && git lfs pull")
    elif size < 1_000_000:
        bad(f"{gid}: file qua nho ({size} byte), nghi hong  [{use}]",
            "git lfs pull")
    else:
        ok(f"{gid}: {size / 1024 / 1024:.1f} MB  [{use}]")

print("\n[4] Nap model that")
if not fails:
    try:
        import torch

        from captcha.config import Config, default_config_path
        from captcha.predict import Predictor

        cfg = Config.load(default_config_path(ROOT))
        for gid in PROD_MODELS:
            pr = Predictor(cfg, ROOT, backend="torch",
                           device=torch.device("cpu"), enforce_group=gid)
            pr.model_for(cfg.by_id(gid))
            ok(f"{gid} nap duoc")
    except Exception as exc:                       # noqa: BLE001
        bad(f"nap model that bai: {type(exc).__name__}: {exc}",
            "charset trong configs/groups.yaml phai bao duoc charset da train")
else:
    print("  bo qua (sua cac loi tren truoc)")

print("\n[5] Playwright chromium (adapter VNPT)")
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        b.close()
    ok("chromium chay duoc")
except Exception as exc:                           # noqa: BLE001
    bad(f"chromium khong chay: {type(exc).__name__}",
        "py -m playwright install chromium")

print("\n[6] File .env")
envf = ROOT / ".env"
if not envf.exists():
    bad(".env chua co", "copy .env.example .env  roi dien gia tri")
else:
    try:
        from dotenv import load_dotenv

        load_dotenv(envf)
    except Exception:                              # noqa: BLE001
        pass
    for k in ENV_REQUIRED:
        (ok if os.getenv(k) else lambda m: bad(m, "dien vao .env"))(
            f"{k} {'da dat' if os.getenv(k) else 'CHUA dat'}")
    pw_keys = [k for k in os.environ if k.startswith("HDT_PASSWORD")]
    if pw_keys:
        ok(f"mat khau GDT: {len(pw_keys)} khoa ({', '.join(sorted(pw_keys))})")
    else:
        bad("khong co HDT_PASSWORD_<MST>", "them vao .env cho tung MST cong ty")
    for k in ENV_OPTIONAL:
        print(f"  {'-' if not os.getenv(k) else 'OK   '} {k}"
              f" {'(khong dat, dung mac dinh)' if not os.getenv(k) else 'da dat'}")

print("\n[7] Ket noi PostgreSQL")
dsn = os.getenv("DATABASE_URL")
if not dsn:
    print("  bo qua (chua co DATABASE_URL)")
else:
    try:
        import psycopg2

        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
        ok("ket noi duoc")
    except Exception as exc:                       # noqa: BLE001
        bad(f"khong ket noi duoc: {type(exc).__name__}",
            "kiem DATABASE_URL, firewall, PostgreSQL da chay chua")

print("\n" + "=" * 62)
if fails:
    print(f"CHUA CHAY DUOC - {len(fails)} loi:")
    for f in fails:
        print(f"  * {f}")
else:
    print("San sang. Buoc tiep: py -X utf8 web\\invoice_job.py init-db")
if warns:
    print(f"\n{len(warns)} canh bao:")
    for w in warns:
        print(f"  * {w}")
sys.exit(1 if fails else 0)
