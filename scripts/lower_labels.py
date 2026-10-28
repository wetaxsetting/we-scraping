"""Hạ chữ HOA trong nhãn (tên file) của một thư mục captcha về chữ thường.

Mặc định chỉ in kế hoạch (dry-run); thêm --apply mới đổi tên. Nhật ký ghi ở
<thư mục>/lower_labels_<ngày>.csv, chạy --undo <file csv> để lùi lại.

Windows không phân biệt hoa/thường trong tên file, nên đổi kiểu `x_Ab.png` →
`x_ab.png` phải qua 1 tên tạm, và so sánh bằng chuỗi tên chứ không bằng Path
(xem CLAUDE.md 1.3).

    python -X utf8 scripts/lower_labels.py env/g_200x80
    python -X utf8 scripts/lower_labels.py env/g_200x80 --apply
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
from pathlib import Path


def _rename(src: Path, dst: Path) -> None:
    tmp = src.with_name(f"__lower_tmp_{os.getpid()}_{src.name}")
    os.rename(src, tmp)
    os.rename(tmp, dst)


def plan(d: Path) -> list[tuple[Path, Path]]:
    moves = []
    for p in sorted(d.glob("*.png")):
        if "_" not in p.stem:
            continue
        prefix, label = p.stem.split("_", 1)
        new = f"{prefix}_{label.lower()}.png"
        if new != p.name:
            moves.append((p, p.with_name(new)))
    return moves


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="CSV")
    args = ap.parse_args()
    d = Path(args.data_dir)

    if args.undo:
        rows = list(csv.DictReader(open(args.undo, encoding="utf-8")))
        for r in reversed(rows):
            _rename(Path(r["to"]), Path(r["from"]))
        print(f"đã lùi {len(rows)} file")
        return 0

    before = set(os.listdir(d))
    moves = plan(d)
    names = [dst.name.lower() for _, dst in moves]
    others = {n.lower() for n in before} - {src.name.lower() for src, _ in moves}
    clash = [n for n in names if n in others] + [n for n in set(names) if names.count(n) > 1]
    if clash:
        print(f"dừng: {len(clash)} tên đích bị trùng, vd {clash[:5]}")
        return 1
    for src, dst in moves[:15]:
        print(f"  {src.name}  ->  {dst.name}")
    print(f"{len(moves)} file có chữ HOA" + ("" if args.apply else "  (dry-run, thêm --apply để đổi)"))
    if not args.apply or not moves:
        return 0

    log = d / f"lower_labels_{dt.date.today():%Y%m%d}.csv"
    with open(log, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["from", "to"])
        for src, dst in moves:
            _rename(src, dst)
            w.writerow([str(src), str(dst)])
    shutil.rmtree(d.parent / ".cache", ignore_errors=True)   # cache không thấy rename

    after = set(os.listdir(d))
    print(f"đã đổi {len(before - after)} file, nhật ký {log}")
    return 0 if len(before - after) == len(moves) else 1


if __name__ == "__main__":
    sys.exit(main())
