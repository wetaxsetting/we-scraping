"""Rewrite the case of filename labels from glyph geometry.

The labels in ``env/g_180x50_vn`` came from ddddocr, which is case-insensitive
by design: it reports a normalised case rather than the case that was drawn, and
it normalises every image to a fixed height, discarding the very signal that
separates ``C`` from ``c``. The result is a systematic bias towards lowercase
(4,578 all-lowercase labels against 45 all-uppercase), which the CRNN then
learns: on real captchas it reads every glyph correctly and still fails, because
it lowercases what should be capital.

This pass keeps the letters from the existing label -- on these images the CRNN
only reproduces them anyway, having memorised the training set -- and re-decides
the case of each one from the glyph's shape. See
:mod:`captcha.case_from_geometry` for what geometry can and cannot settle:
x-height letters are decided outright, ascender pairs are left alone.

Dry run unless ``--apply`` is passed. Every rename is written to a CSV that
``--undo`` reads back.
"""
from __future__ import annotations

import argparse
import collections
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .case_from_geometry import apply_case
from .utils import setup_logging

log = logging.getLogger("captcha")

FILENAME_RE = re.compile(r"^(\d+)_(.+)\.png$")
DEFAULT_REPORT = "relabel_case.csv"


def _next_free(used: set, start: int, width: int) -> Tuple[int, str]:
    n = start
    while f"{n:0{width}d}" in used:
        n += 1
    return n, f"{n:0{width}d}"


def _rename(src: Path, dst: Path) -> bool:
    """Rename, including a change that only differs in case. True if it moved.

    Compares the names as strings: ``Path`` equality is case-insensitive on
    Windows, so ``src == dst`` is True for exactly the case-only renames this
    pass is made of, and guarding on it skips every one of them silently.
    Going through a temporary name then keeps the rename itself well defined,
    since the filesystem also considers the two names the same.
    """
    if src.name == dst.name:
        return False
    if src.name.lower() == dst.name.lower():
        tmp = src.with_name(f"__relabel_tmp_{os.getpid()}_{src.stem}.png")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)
    return True


def plan(data_dir: Path, limit: int = 0) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    files = sorted(p for p in data_dir.iterdir()
                   if p.is_file() and p.suffix.lower() == ".png")
    if limit:
        files = files[:limit]

    taken = {p.stem for p in files}
    rows: List[Dict[str, str]] = []
    counts = collections.Counter()
    flips = collections.Counter()
    seq_hint = 0

    for p in files:
        m = FILENAME_RE.match(p.name)
        if not m:
            counts["skipped_name"] += 1
            continue
        index, label = m.group(1), m.group(2)
        try:
            res = apply_case(label, p)
        except Exception as exc:                      # unreadable/corrupt png
            log.warning("%s: %s", p.name, exc)
            counts["error"] += 1
            continue

        if not res.segmented:
            counts["unsegmented"] += 1
            continue
        counts["uncertain_positions"] += len(res.uncertain)
        if res.text == label:
            counts["unchanged"] += 1
            continue

        for a, b in zip(label, res.text):
            if a != b:
                flips[f"{a}->{b}"] += 1

        new_name = f"{index}_{res.text}.png"
        if new_name != p.name and new_name[:-4] in taken:
            width = len(index)
            seq_hint, new_index = _next_free(taken, max(seq_hint, 1), width)
            new_name = f"{new_index}_{res.text}.png"
            counts["reindexed"] += 1
        taken.discard(p.stem)
        taken.add(new_name[:-4])

        counts["changed"] += 1
        rows.append({"old": p.name, "new": new_name, "label": label,
                     "new_label": res.text,
                     "uncertain": "".join(str(i) for i in res.uncertain)})

    counts["total"] = len(files)
    counts["top_flips"] = 0
    log.info("top case flips: %s", ", ".join(f"{k} x{v}" for k, v in flips.most_common(12)))
    return rows, counts


def clear_cache(data_dir: Path) -> int:
    """Drop the index cache: its signature is (count, bytes, newest mtime), none
    of which a rename changes, so a stale cache would serve the old labels."""
    cache_dir = data_dir.parent / ".cache"
    if not cache_dir.is_dir():
        return 0
    gone = 0
    for p in cache_dir.glob("index_*.json"):
        p.unlink()
        gone += 1
    return gone


def write_report(rows: List[Dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["old", "new", "label", "new_label",
                                           "uncertain"])
        w.writeheader()
        w.writerows(rows)


def undo(data_dir: Path, report: Path) -> int:
    with report.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    done = 0
    for r in reversed(rows):
        src, dst = data_dir / r["new"], data_dir / r["old"]
        if src.exists() and _rename(src, dst):
            done += 1
    return done


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Re-case filename labels from glyph geometry")
    ap.add_argument("--data-dir", required=True,
                    help="folder of <index>_<label>.png, e.g. env/g_180x50_vn")
    ap.add_argument("--apply", action="store_true",
                    help="actually rename (default: dry run)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", default=None,
                    help=f"CSV of renames (default: <data-dir>/{DEFAULT_REPORT})")
    ap.add_argument("--undo", action="store_true",
                    help="reverse the renames recorded in the report")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    data_dir = Path(args.data_dir)
    report = Path(args.report) if args.report else data_dir / DEFAULT_REPORT

    if args.undo:
        if not report.exists():
            log.error("no report at %s", report)
            return 2
        n = undo(data_dir, report)
        log.info("reverted %d renames; cleared %d cache files",
                 n, clear_cache(data_dir))
        return 0

    rows, counts = plan(data_dir, args.limit)
    log.info("scanned %d | change %d | unchanged %d | unsegmented %d | error %d "
             "| reindexed %d", counts["total"], counts["changed"],
             counts["unchanged"], counts["unsegmented"], counts["error"],
             counts["reindexed"])
    log.info("positions geometry could not decide (ascender pairs): %d",
             counts["uncertain_positions"])

    for r in rows[:15]:
        log.info("  %s -> %s", r["old"], r["new"])
    if len(rows) > 15:
        log.info("  ... %d more", len(rows) - 15)

    if not args.apply:
        write_report(rows, report)
        log.info("DRY RUN. Plan written to %s. Re-run with --apply to rename.",
                 report)
        return 0

    done, skipped = 0, 0
    applied = []
    for r in rows:
        try:
            if _rename(data_dir / r["old"], data_dir / r["new"]):
                applied.append(r)
                done += 1
            else:
                skipped += 1
        except OSError as exc:
            log.error("%s -> %s: %s", r["old"], r["new"], exc)
    if skipped:
        log.warning("%d planned renames were no-ops", skipped)
    write_report(applied, report)
    log.info("renamed %d files; cleared %d cache files", done, clear_cache(data_dir))
    log.info("report: %s  (undo with --undo)", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
