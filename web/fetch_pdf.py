# fetch_pdf.py
"""
Tải PDF bản gốc từ site tra cứu của đơn vị cung cấp HĐĐT (NCC) cho các file XML có sẵn
trên đĩa — KHÔNG cần DB, KHÔNG đăng nhập GDT.

Đọc từng XML (MSTTCGP, mã tra cứu, Fkey, MCCQT...) → chọn adapter theo MSTTCGP →
tải PDF → lưu cùng cấu trúc thư mục với XML:
    <xml-dir>/<MST công ty>/<năm>/<tháng>/<MST người bán>/1_C26TAA_123.xml
    <pdf-dir>/<MST công ty>/<năm>/<tháng>/<MST người bán>/1_C26TAA_123.pdf
PDF đã có thì bỏ qua (chạy lại bao nhiêu lần cũng được), trừ khi --force.

Lệnh:
    python -X utf8 web/fetch_pdf.py                         # output/xml → output/pdf
    python -X utf8 web/fetch_pdf.py --company 1201496252    # chỉ thư mục của 1 công ty
    python -X utf8 web/fetch_pdf.py --dry-run               # chỉ liệt kê HĐ nào tải được bằng adapter nào
    python -X utf8 web/fetch_pdf.py --mst-tcgp 0101243150   # chỉ HĐ của 1 NCC (vd MISA)
    python -X utf8 web/fetch_pdf.py --seller 0108055808 --limit 5 --force

Job có DB thì dùng `web/invoice_job.py pdf` (hàng đợi, thử lại, ghi invoice_pdf).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import job_policy                                 # noqa: E402
import pdf_providers                              # noqa: E402
import xml_parser                                 # noqa: E402

log = logging.getLogger("fetch_pdf")

FETCH_PAUSE_SECONDS = 0.5      # nghỉ giữa 2 lần tải, không dồn site tra cứu
MIN_PDF_BYTES = 1000


def invoice_from_xml(parsed: dict) -> dict:
    """Dict hóa đơn đúng các khóa adapter đọc (như InvoiceStore.claim_tasks trả về)."""
    tc = parsed.get("thong_tin_chung") or {}
    nb = parsed.get("nguoi_ban") or {}
    khhdon = tc.get("ky_hieu") or ""
    return {
        "nbmst": nb.get("mst"), "nbten": nb.get("ten"),
        "khmshdon": tc.get("ky_hieu_mau"), "khhdon": khhdon, "shdon": tc.get("so_hd"),
        "ngay_lap": tc.get("ngay_lap"),
        "tong_tien": (parsed.get("thanh_toan") or {}).get("tong_tien_tt"),
        "mst_tcgp": (tc.get("mst_tcgp") or "").strip() or None,
        "ma_tra_cuu": parsed.get("ma_tra_cuu"), "lookup_url": parsed.get("lookup_url"),
        "tt_khac": parsed.get("tt_khac") or {}, "ma_cqt": parsed.get("ma_cqt"),
        "dlhdon_id": parsed.get("dlhdon_id"),
        # Ký hiệu HĐ: ký tự thứ 4 = 'M' → HĐ khởi tạo từ máy tính tiền
        "loai_hd": "MTT" if len(khhdon) >= 4 and khhdon[3].upper() == "M" else None,
    }


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _kind(exc: Exception) -> str:
    if isinstance(exc, pdf_providers.PdfFetchError):
        return exc.kind
    name = type(exc).__name__
    if "Timeout" in name or "Connection" in name:
        return "network"
    return "other"


def run(xml_dir: Path, pdf_dir: Path, *, company: str | None = None, seller: str | None = None,
        mst_tcgp: str | None = None, limit: int | None = None, force: bool = False,
        dry_run: bool = False) -> dict:
    stats = Counter()
    no_adapter = defaultdict(lambda: {"hd": 0, "sellers": set()})
    failures = []
    breakers: dict = {}
    fetched = 0

    for xml_path in sorted(xml_dir.rglob("*.xml")):
        if company and xml_path.relative_to(xml_dir).parts[0] != company:
            continue
        try:
            inv = invoice_from_xml(xml_parser.parse_xml_bytes(xml_path.read_bytes()))
        except Exception as e:  # noqa: BLE001
            stats["xml_loi"] += 1
            failures.append((xml_path.name, "parse", str(e)))
            continue
        if seller and inv["nbmst"] != seller:
            continue
        if mst_tcgp and (inv["mst_tcgp"] or "").split("-")[0] != mst_tcgp.split("-")[0]:
            continue
        target = pdf_dir / xml_path.relative_to(xml_dir).with_suffix(".pdf")
        if target.exists() and not force:
            stats["da_co"] += 1
            continue

        adapter = pdf_providers.adapter_for_mst_tcgp(inv["mst_tcgp"])
        if adapter is None:
            stats["chua_co_adapter"] += 1
            slot = no_adapter[inv["mst_tcgp"] or "(không có MSTTCGP)"]
            slot["hd"] += 1
            slot["sellers"].add(f"{inv['nbmst']} {inv['nbten'] or ''}".strip())
            continue
        if limit is not None and fetched >= limit:
            stats["de_lan_sau"] += 1
            continue
        fetched += 1
        if dry_run:
            log.info("[dry-run] %-15s %s → %s", adapter.source_id, xml_path.name, target)
            stats["se_tai"] += 1
            continue

        br = breakers.setdefault(adapter.source_id, job_policy.CircuitBreaker())
        if br.open:
            stats["bo_qua_site_loi"] += 1
            continue
        t0 = time.time()
        try:
            pdf = adapter.fetch_pdf(inv)
            if not pdf.startswith(b"%PDF") or len(pdf) < MIN_PDF_BYTES:
                raise pdf_providers.PdfFetchError("Nội dung tải về không phải PDF hợp lệ", "bad_content")
            _atomic_write(target, pdf)
            br.record(True)
            stats["ok"] += 1
            log.info("OK   %-15s %s (%s KB, %.1fs)", adapter.source_id, target.name,
                     len(pdf) // 1024, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            kind = _kind(e)
            br.record(False, kind)
            stats["loi"] += 1
            failures.append((xml_path.name, f"{adapter.source_id}/{kind}", str(e)))
            log.warning("LỖI %-15s %s [%s] %s", adapter.source_id, xml_path.name, kind, e)
            if br.open:
                log.warning("%s lỗi tạm thời liên tiếp → bỏ qua các HĐ còn lại của site này",
                            adapter.source_id)
        time.sleep(FETCH_PAUSE_SECONDS)

    return {"stats": stats, "no_adapter": no_adapter, "failures": failures}


def print_report(res: dict, pdf_dir: Path) -> None:
    labels = {"ok": "tải mới", "se_tai": "sẽ tải (dry-run)", "da_co": "đã có PDF, bỏ qua",
              "chua_co_adapter": "NCC chưa có adapter", "loi": "lỗi",
              "bo_qua_site_loi": "bỏ qua do site NCC đang lỗi", "de_lan_sau": "vượt --limit, để lần sau",
              "xml_loi": "XML đọc lỗi"}
    print(f"\nKết quả ({pdf_dir}):")
    for key, label in labels.items():
        if res["stats"][key]:
            print(f"  {label:30} {res['stats'][key]:>5}")
    if res["no_adapter"]:
        print("\nNCC (MSTTCGP) chưa có adapter — nhiều HĐ nhất trước:")
        for mst, slot in sorted(res["no_adapter"].items(), key=lambda kv: -kv[1]["hd"]):
            print(f"  {mst:20} {slot['hd']:>4} HĐ  {'; '.join(sorted(slot['sellers']))[:110]}")
    if res["failures"]:
        print("\nLỗi:")
        for name, kind, msg in res["failures"]:
            print(f"  {name:32} {kind:22} {msg[:120]}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Tải PDF bản gốc từ NCC cho các XML có sẵn (không cần DB)")
    ap.add_argument("--xml-dir", type=Path, default=ROOT / "output" / "xml")
    ap.add_argument("--pdf-dir", type=Path, help="mặc định: thư mục 'pdf' cạnh --xml-dir")
    ap.add_argument("--company", help="chỉ thư mục của 1 công ty (cấp đầu dưới --xml-dir), vd 1201496252")
    ap.add_argument("--seller", help="chỉ HĐ của 1 người bán (MST)")
    ap.add_argument("--mst-tcgp", help="chỉ HĐ của 1 NCC hóa đơn điện tử (MSTTCGP)")
    ap.add_argument("--limit", type=int, help="tải tối đa N HĐ lượt này")
    ap.add_argument("--force", action="store_true", help="tải lại cả HĐ đã có PDF")
    ap.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không tải")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stdout)
    xml_dir = args.xml_dir.resolve()
    if not xml_dir.is_dir():
        print(f"Không thấy thư mục XML: {xml_dir}")
        return 2
    pdf_dir = (args.pdf_dir or xml_dir.parent / "pdf").resolve()
    log.info("XML: %s → PDF: %s | adapter: %s", xml_dir, pdf_dir, ", ".join(pdf_providers.available()))
    res = run(xml_dir, pdf_dir, company=args.company, seller=args.seller, mst_tcgp=args.mst_tcgp, limit=args.limit,
              force=args.force, dry_run=args.dry_run)
    print_report(res, pdf_dir)
    return 1 if res["stats"]["loi"] else 0


if __name__ == "__main__":
    sys.exit(main())
