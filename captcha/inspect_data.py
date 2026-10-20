"""Dataset report and config generation -- the gate that runs before any training.

Prints, per group: sample count, label-length range, the derived core alphabet, the
rare-character tail, how many samples a charset policy would drop, the measured
border-ring luminance (polarity), and whether the group satisfies the CTC
``T >= 2L+1`` rule.

Three groups lose >10% of their samples at the default threshold, and their tails
are too large and too structured to be pure noise -- they probably have a genuinely
larger alphabet. Rather than quietly deleting 17% of a group, this marks them
``review: true`` and ``--dump-tail-samples`` writes the offending images out so a
human can look at them.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from .charset import apply_policy, derive_core
from .config import Config, PreprocCfg, build_groups, group_id_for
from .ctc import required_timesteps
from .dataset import Record, crosscheck_csv, duplicate_report, scan_index
from .model import conv_out_width
from .transforms import border_ring_median, load_image, preprocess_path, resized_width
from .utils import setup_logging

RING_SAMPLE = 120
REVIEW_DROP_FRAC = 0.10
# The border ring must be brighter than the glyph cores. Comparing against the
# image's own 5th percentile rather than against zero is what makes this a real
# check: the percentile contrast stretch anchors on the glyphs (p2 lands on the
# text), so a perfectly canonical light background can still sit below the
# midpoint of the output range. Measured margins are 0.68..2.0 for correct
# polarity and about -2.0 for an inverted one, so 0.3 separates them cleanly.
RING_REL_MIN = 0.3


@dataclass
class GroupReport:
    group_id: str
    size: tuple
    n: int
    min_len: int
    max_len: int
    median_len: int
    core: List[str]
    tail: Dict[str, int]
    n_drop: int
    drop_frac: float
    width: int
    timesteps: int
    needed: int
    ring_raw: float
    ring_norm: float
    ring_rel: float
    review: bool

    @property
    def timesteps_ok(self) -> bool:
        return self.timesteps >= self.needed

    @property
    def polarity_ok(self) -> bool:
        # After normalization the background must be brighter than the glyphs.
        return self.ring_rel > RING_REL_MIN

    @property
    def ok(self) -> bool:
        return self.timesteps_ok and self.polarity_ok


def _ring_stats(record: Record, data_dir: Path, preproc: PreprocCfg
                ) -> Tuple[float, float]:
    """``(ring median, ring minus 5th percentile)`` of the *normalized* image.

    Measuring the real output validates the whole chain rather than re-deriving
    the polarity algebra, which would be trivially self-consistent. The relative
    form is the one that means something: it asks whether the border is brighter
    than the darkest pixels (the glyph cores), which stays true no matter where
    the contrast stretch put them.
    """
    image, _info = preprocess_path(record.path(data_dir), preproc)
    ring = border_ring_median(image[0], preproc.border_px)
    return ring, ring - float(np.percentile(image, 5))


def analyse(records: Sequence[Record], data_dir: Path, preproc: PreprocCfg,
            min_freq: float = 0.005, sample: int = RING_SAMPLE) -> List[GroupReport]:
    by_size: Dict[tuple, List[Record]] = defaultdict(list)
    for r in records:
        by_size[r.size].append(r)

    reports: List[GroupReport] = []
    for size in sorted(by_size, key=lambda s: -len(by_size[s])):
        group_records = by_size[size]
        labels = [r.label for r in group_records]
        core, tail = derive_core(labels, min_freq)
        policy = apply_policy(labels, "core", min_freq)
        lengths = sorted(len(lab) for lab in labels)
        width = resized_width(size[0], size[1], preproc.height)

        rings_raw: List[float] = []
        rings_norm: List[float] = []
        rings_rel: List[float] = []
        for r in group_records[:sample]:
            gray = load_image(r.path(data_dir)).mean(axis=2)
            rings_raw.append(border_ring_median(gray, preproc.border_px))
            ring_norm, ring_rel = _ring_stats(r, data_dir, preproc)
            rings_norm.append(ring_norm)
            rings_rel.append(ring_rel)

        reports.append(GroupReport(
            group_id=group_id_for(size), size=size, n=len(group_records),
            min_len=min(lengths), max_len=max(lengths),
            median_len=lengths[len(lengths) // 2],
            core=core, tail=dict(tail.most_common()), n_drop=len(policy.dropped),
            drop_frac=policy.drop_frac, width=width,
            timesteps=conv_out_width(width),
            needed=required_timesteps(max(lengths)),
            ring_raw=float(np.median(rings_raw)),
            ring_norm=float(np.median(rings_norm)),
            ring_rel=float(np.median(rings_rel)),
            review=policy.drop_frac > REVIEW_DROP_FRAC))
    return reports


def format_table(reports: Sequence[GroupReport]) -> str:
    lines = [
        f"{'group':11s} {'n':>6s} {'len':>7s} {'core':>5s} {'tail':>5s} "
        f"{'drop':>14s} {'ring':>7s} {'ringN':>7s} {'rel':>6s} {'W':>4s} {'T':>4s} "
        f"{'need':>5s} {'chk':>4s} {'rev':>4s}",
        "-" * 112,
    ]
    for r in reports:
        flag = "OK" if r.ok else "FAIL"
        lines.append(
            f"{r.group_id:11s} {r.n:6d} {r.min_len:3d}-{r.max_len:<3d} "
            f"{len(r.core):5d} {len(r.tail):5d} "
            f"{r.n_drop:6d} ({r.drop_frac:5.1%}) {r.ring_raw:7.1f} {r.ring_norm:7.2f} "
            f"{r.ring_rel:6.2f} {r.width:4d} {r.timesteps:4d} {r.needed:5d} {flag:>4s} "
            f"{'REVIEW' if r.review else '':>4s}")
    return "\n".join(lines)


def assert_healthy(reports: Sequence[GroupReport], expected_total: Optional[int] = None
                   ) -> List[str]:
    """Raise on anything that would silently ruin training. Returns warnings."""
    problems: List[str] = []
    warnings: List[str] = []

    total = sum(r.n for r in reports)
    if expected_total is not None and total != expected_total:
        problems.append(f"expected {expected_total} images, found {total}")
    for r in reports:
        if not r.timesteps_ok:
            problems.append(
                f"{r.group_id}: T={r.timesteps} < {r.needed} required for a "
                f"{r.max_len}-character label (T >= 2*maxlen+1); CTC would starve")
        if not r.polarity_ok:
            problems.append(
                f"{r.group_id}: background is not brighter than the glyphs after "
                f"normalization (ring {r.ring_norm:+.2f} vs p5, margin "
                f"{r.ring_rel:+.2f}); polarity canonicalization failed")
        if r.review:
            warnings.append(
                f"{r.group_id}: charset policy drops {r.drop_frac:.1%} of samples -- "
                f"the tail is probably a real alphabet, not noise; try "
                f"charset_mode: full and compare, or inspect with --dump-tail-samples")
    if problems:
        raise AssertionError("dataset sanity check failed:\n  - " + "\n  - ".join(problems))
    return warnings


def dump_tail_samples(reports: Sequence[GroupReport], records: Sequence[Record],
                      data_dir: Path, out_dir: Path, per_group: int = 20) -> int:
    """Copy images whose label contains a tail character, for human adjudication."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_size: Dict[tuple, List[Record]] = defaultdict(list)
    for r in records:
        by_size[r.size].append(r)

    written = 0
    for report in reports:
        if not report.tail:
            continue
        tail_chars = set(report.tail)
        picked = [r for r in by_size[report.size]
                  if set(r.label) & tail_chars][:per_group]
        for r in picked:
            target = out_dir / f"{report.group_id}__{r.name}"
            shutil.copy2(r.path(data_dir), target)
            written += 1
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report on the captcha dataset")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--config", default=None)
    parser.add_argument("--write-config", default=None,
                        help="write groups.yaml here (default: configs/groups.yaml)")
    parser.add_argument("--min-freq", type=float, default=0.005)
    parser.add_argument("--dump-tail-samples", default=None, metavar="DIR")
    parser.add_argument("--per-group", type=int, default=20)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--expected-total", type=int, default=25976,
                        help="assert the image count (0 to skip)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config)) if args.config else Config()
    data_dir = Path(args.data_dir) if args.data_dir else root / cfg.data_dir

    print(f"scanning {data_dir} ...")
    records, skipped = scan_index(data_dir, progress=True)
    if skipped:
        print(f"  WARNING: {len(skipped)} file(s) did not match "
              f"'<index>_<label>.png': {skipped[:5]}")

    print(f"  {len(records)} labelled images, "
          f"{len({r.size for r in records})} distinct sizes")

    dups = duplicate_report(records)
    print(f"  duplicate content: {dups['n_files']} files in {dups['n_groups']} "
          f"groups; {len(dups['conflicts'])} with CONFLICTING labels")
    for conflict in dups["conflicts"][:5]:
        print(f"    {conflict['files']} -> labels {conflict['labels']}")
    csv_check = crosscheck_csv(data_dir, records)
    if csv_check.get("present"):
        print(f"  labels.csv cross-check: {csv_check['checked']} rows compared, "
              f"{csv_check['n_mismatch']} mismatch")

    reports = analyse(records, data_dir, cfg.preproc, args.min_freq)
    print()
    print(format_table(reports))
    print()
    print(f"  charset mode 'core' at min_freq={args.min_freq}: "
          f"{sum(r.n_drop for r in reports)} samples dropped of {len(records)} "
          f"({sum(r.n_drop for r in reports) / len(records):.2%})")

    warnings = assert_healthy(reports,
                              args.expected_total if args.expected_total else None)
    for warning in warnings:
        print(f"  REVIEW: {warning}")

    if args.dump_tail_samples:
        n = dump_tail_samples(reports, records, data_dir, Path(args.dump_tail_samples),
                              args.per_group)
        print(f"  wrote {n} tail samples to {args.dump_tail_samples} -- open them and "
              f"judge whether the rare character is really what the label claims")

    if args.write_config:
        config_path = Path(args.write_config)
        charset_by_group = {r.group_id: "".join(r.core) for r in reports}
        widths = {r.group_id: r.width for r in reports}
        notes = {r.group_id: f"{(r.size[0], r.size[1])} n={r.n} "
                             f"len {r.min_len}-{r.max_len} "
                             f"drop {r.drop_frac:.1%} ring_raw {r.ring_raw:.0f}"
                 for r in reports}
        groups = build_groups([r.size for r in reports], charset_by_group, widths,
                              review={r.group_id: r.review for r in reports},
                              notes=notes, preproc=cfg.preproc)
        out = Config(data_dir=cfg.data_dir, preproc=cfg.preproc, aug=cfg.aug,
                     train=cfg.train, groups=groups)
        out.save(config_path)
        print(f"  wrote {config_path} with {len(groups)} groups")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps([
            {"group_id": r.group_id, "size": list(r.size), "n": r.n,
             "min_len": r.min_len, "max_len": r.max_len,
             "median_len": r.median_len,
             "core": "".join(r.core), "tail": r.tail, "n_drop": r.n_drop,
             "drop_frac": round(r.drop_frac, 4), "width": r.width,
             "timesteps": r.timesteps, "needed": r.needed,
             "ring_raw": round(r.ring_raw, 2), "ring_norm": round(r.ring_norm, 4),
             "ring_rel": round(r.ring_rel, 4),
             "review": r.review, "ok": r.ok}
            for r in reports], indent=2), encoding="utf-8")
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
