"""Contact sheet for tail samples, so a human can adjudicate them.

``inspect_data --dump-tail-samples`` writes the images whose labels contain a rare
character. Reviewing 150 loose 80x30 PNGs one at a time is impractical; this lays
them out in a grid, upscaled with nearest-neighbour so individual pixels stay
visible.

The question to answer while looking: **is the rare character visually distinct
from the core characters?** If it looks identical to one of them -- as ``l`` and
``I`` do to the serif ``1`` in ``g_80x30`` -- the label is wrong and the ``core``
policy is right to drop it. If it is clearly its own glyph, the group has a
genuinely larger alphabet and wants ``charset_mode: full``.

Usage::

    python -m tools.contact_sheet out/tail_samples --prefix g_80x30 -o out/sheet.png
    python -m tools.contact_sheet out/tail_samples -o out/all_sheets/ --all
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional, Sequence

import cv2
import numpy as np

CELL_H = 120
LABEL_H = 26


def load_flat(path: Path) -> np.ndarray:
    """Read any PNG as BGR, compositing alpha over white."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        img = (img[:, :, :3].astype(np.float32) * alpha
               + 255.0 * (1 - alpha)).astype(np.uint8)
    elif img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def fit(img: np.ndarray, cell_h: int, max_w: int) -> np.ndarray:
    """Scale to ``cell_h`` tall with nearest-neighbour, capping the width."""
    scale = cell_h / img.shape[0]
    img = cv2.resize(img, (max(1, round(img.shape[1] * scale)), cell_h),
                     interpolation=cv2.INTER_NEAREST)
    if img.shape[1] > max_w:
        img = cv2.resize(img, (max_w, cell_h), interpolation=cv2.INTER_AREA)
    return img


def sheet(paths: Sequence[Path], out: Path, cols: int = 2, cell_h: int = CELL_H,
          max_w: int = 430) -> Path:
    if not paths:
        raise ValueError("no images to lay out")
    cells: List[np.ndarray] = []
    for path in paths:
        img = fit(load_flat(path), cell_h, max_w)
        cell = np.full((cell_h + LABEL_H, max_w, 3), 245, np.uint8)
        cell[LABEL_H:LABEL_H + img.shape[0], :img.shape[1]] = img
        cv2.putText(cell, path.stem[:56], (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 0, 0), 1, cv2.LINE_AA)
        cells.append(cell)

    blank = np.full_like(cells[0], 245)
    rows = []
    for i in range(0, len(cells), cols):
        chunk = list(cells[i:i + cols])
        chunk += [blank] * (cols - len(chunk))
        rows.append(np.hstack(chunk))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack(rows))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Contact sheet of tail samples")
    parser.add_argument("src", help="directory written by --dump-tail-samples")
    parser.add_argument("-o", "--out", default="out/sheet.png")
    parser.add_argument("--prefix", default=None,
                        help="only files starting with this group id, e.g. g_80x30")
    parser.add_argument("--all", action="store_true",
                        help="one sheet per group, written under --out as a directory")
    parser.add_argument("--cols", type=int, default=2)
    parser.add_argument("--cell-h", type=int, default=CELL_H)
    args = parser.parse_args(argv)

    src = Path(args.src)
    if not src.is_dir():
        parser.error(f"{src} is not a directory; run "
                     f"'python -m captcha.inspect_data --dump-tail-samples "
                     f"{src}' first")

    if args.all:
        groups = sorted({p.name.split("__", 1)[0] for p in src.glob("*.png")})
        for gid in groups:
            paths = sorted(src.glob(f"{gid}__*.png"))
            target = sheet(paths, Path(args.out) / f"{gid}.png", args.cols,
                           args.cell_h)
            print(f"wrote {target} ({len(paths)} samples)")
        return 0

    paths = sorted(src.glob(f"{args.prefix}__*.png" if args.prefix else "*.png"))
    target = sheet(paths, Path(args.out), args.cols, args.cell_h)
    print(f"wrote {target} ({len(paths)} samples)")
    print("Ask: does the rare character look identical to a core character? "
          "If yes, the label is wrong and 'core' is correct.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
