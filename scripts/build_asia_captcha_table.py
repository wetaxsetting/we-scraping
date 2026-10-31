"""Dựng bảng tra captcha ASIA (asiainvoice.vn) từ env/g_150x30 → pdf_providers/data/asia_captcha.npz.

Cổng ASIA không sinh captcha mới mỗi lần mà phát lại từ 1 kho ảnh cố định: 7.561 lần tải chỉ ra 823
ảnh khác nhau, và 19/20 captcha tải mới 29/10 trùng **từng điểm ảnh** với 1 ảnh đã có. Nên adapter
tra bảng thay vì OCR. Ảnh trong env là JPEG của cổng đã giải mã rồi lưu PNG (không mất điểm ảnh).

Nhãn lấy từ tên file `<số>_<NHÃN>.png` (đã dọn trùng + sửa 31 nhãn mâu thuẫn bằng mắt 29/10). Thêm /
sửa ảnh trong env/g_150x30 thì chạy lại script này rồi commit file .npz.

`--with-learned DIR` gộp thêm ảnh adapter tự học lúc chạy (`<output>/captcha_learned/asiainvoice`,
tên `<sha1>_<NHÃN>.png`, chỉ gồm đáp án server đã nhận). Ảnh trùng với bảng gốc thì nhãn học được thắng.

    python -X utf8 scripts/build_asia_captcha_table.py
    python -X utf8 scripts/build_asia_captcha_table.py --with-learned output/captcha_learned/asiainvoice
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "env" / "g_150x30"
OUT = ROOT / "pdf_providers" / "data" / "asia_captcha.npz"
LABEL_RE = re.compile(r"^\d+_([A-Z]{5})$")


LEARNED_RE = re.compile(r"^[0-9a-f]{40}_([A-Z0-9]{5})$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-learned", metavar="DIR")
    args = ap.parse_args()
    imgs, labels, skipped = [], [], []
    for p in sorted(SRC.glob("*.png")):
        m = LABEL_RE.match(p.stem)
        if not m:
            skipped.append(p.name)
            continue
        a = np.asarray(Image.open(p).convert("L"), dtype=np.uint8)
        if a.shape != (30, 150):
            skipped.append(p.name)
            continue
        imgs.append(a)
        labels.append(m.group(1))
    n_env = len(labels)
    if args.with_learned:
        index = {a.tobytes(): i for i, a in enumerate(imgs)}
        added = fixed = 0
        for p in sorted(Path(args.with_learned).glob("*.png")):
            m = LEARNED_RE.match(p.stem)
            a = np.asarray(Image.open(p).convert("L"), dtype=np.uint8)
            if not m or a.shape != (30, 150):
                skipped.append(p.name)
                continue
            i = index.get(a.tobytes())
            if i is None:
                index[a.tobytes()] = len(imgs)
                imgs.append(a); labels.append(m.group(1)); added += 1
            elif labels[i] != m.group(1):
                print(f"  sửa nhãn bảng gốc {labels[i]} → {m.group(1)} (server đã nhận)")
                labels[i] = m.group(1); fixed += 1
        print(f"env: {n_env} ảnh, học thêm: +{added} ảnh mới, sửa {fixed} nhãn")
    if skipped:
        print(f"bỏ {len(skipped)} file (tên/kích thước lạ): {skipped[:5]}")
    stack = np.stack(imgs)
    # 2 ảnh giống hệt mà khác nhãn = bảng tự mâu thuẫn → dừng, đừng ghi
    seen: dict = {}
    for a, lab in zip(stack, labels):
        k = a.tobytes()
        if k in seen and seen[k] != lab:
            print(f"dừng: cùng 1 ảnh mang 2 nhãn {seen[k]} / {lab}")
            return 1
        seen[k] = lab
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT, images=stack, labels=np.array(labels))
    print(f"{len(labels)} ảnh → {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
