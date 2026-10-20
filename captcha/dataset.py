"""Indexing, per-group record selection, splitting and DataLoaders.

Two dataset quirks drive this module:

* **The 6-digit filename prefix is not a unique key** (18,777 distinct prefixes for
  25,976 files; 7,199 prefixes hold two files) so it is never used for anything.
* **227 exact-duplicate content groups exist (765 files, 2.9%), 3 of which carry
  conflicting labels.** Splitting is therefore assigned at the md5 content-group
  level, so a near-copy of a training image can never land in val/test.

Splitting is also stratified by label length, because label length is the one
thing that varies structurally inside a group (1-10 characters).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset, Sampler

from .charset import build_index, resolve_labels
from .config import AugCfg, GroupCfg, PreprocCfg, TrainCfg
from .transforms import preprocess_path, preprocess_train, resized_width

FILENAME_RE = re.compile(r"^(\d+)_(.+)\.png$")
CACHE_VERSION = 1


@dataclass(frozen=True)
class Record:
    name: str
    label: str
    w: int
    h: int
    md5: str

    @property
    def size(self) -> Tuple[int, int]:
        return (self.w, self.h)

    def path(self, data_dir: Path) -> Path:
        return Path(data_dir) / self.name

    def width_at(self, height: int) -> int:
        return resized_width(self.w, self.h, height)


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
def _md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _dir_signature(data_dir: Path) -> Dict[str, object]:
    names = [e.name for e in os.scandir(data_dir) if e.name.lower().endswith(".png")]
    total = 0
    newest = 0.0
    for name in names:
        st = (data_dir / name).stat()
        total += st.st_size
        newest = max(newest, st.st_mtime)
    return {"n": len(names), "total": total, "newest": round(newest, 3)}


def scan_index(data_dir: Path, cache_dir: Optional[Path] = None,
               progress: bool = False) -> Tuple[List[Record], List[str]]:
    """Read every PNG's label, size and content hash. Returns ``(records, skipped)``.

    The label comes from the filename. ``pass/labels.csv`` is deliberately never
    read as truth -- it has only 7,201 of 25,976 rows.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data dir not found: {data_dir}")

    cache_dir = Path(cache_dir) if cache_dir else data_dir.parent / ".cache"
    sig = _dir_signature(data_dir)
    key = hashlib.sha1(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:12]
    cache_path = cache_dir / f"index_{key}.json"

    if cache_path.exists():
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            if blob.get("version") == CACHE_VERSION and blob.get("signature") == sig:
                recs = [Record(*row) for row in blob["records"]]
                return recs, list(blob.get("skipped", []))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass  # corrupt/stale cache: fall through and rebuild

    records: List[Record] = []
    skipped: List[str] = []
    names = sorted(e.name for e in os.scandir(data_dir)
                   if e.name.lower().endswith(".png"))
    for i, name in enumerate(names):
        match = FILENAME_RE.match(name)
        if not match:
            skipped.append(name)
            continue
        path = data_dir / name
        with Image.open(path) as im:
            w, h = im.size
        records.append(Record(name=name, label=match.group(2), w=int(w), h=int(h),
                              md5=_md5_of(path)))
        if progress and (i + 1) % 5000 == 0:
            print(f"  indexed {i + 1}/{len(names)}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "version": CACHE_VERSION, "signature": sig,
        "records": [[r.name, r.label, r.w, r.h, r.md5] for r in records],
        "skipped": skipped,
    }), encoding="utf-8")
    return records, skipped


def duplicate_report(records: Sequence[Record]) -> Dict[str, object]:
    """Duplicate content groups, and the ones whose labels disagree.

    The conflicting ones are provable label errors -- useful evidence that the
    rare-character tails really are annotation noise.
    """
    by_md5: Dict[str, List[Record]] = defaultdict(list)
    for r in records:
        by_md5[r.md5].append(r)
    dups = {k: v for k, v in by_md5.items() if len(v) > 1}
    conflicts = []
    for md5, group in dups.items():
        labels = {r.label for r in group}
        if len(labels) > 1:
            conflicts.append({"md5": md5, "labels": sorted(labels),
                              "files": [r.name for r in group]})
    return {"n_groups": len(dups),
            "n_files": sum(len(v) for v in dups.values()),
            "conflicts": conflicts}


def crosscheck_csv(data_dir: Path, records: Sequence[Record],
                   limit_examples: int = 5) -> Dict[str, object]:
    """Soft check of the incomplete labels.csv against the filenames."""
    csv_path = Path(data_dir) / "labels.csv"
    if not csv_path.exists():
        return {"present": False}
    import csv as _csv

    by_name = {r.name: r.label for r in records}
    checked = 0
    mismatches = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        for row in _csv.DictReader(fh):
            name = (row.get("filename") or "").strip()
            label = (row.get("label") or "").strip()
            if name not in by_name:
                continue
            checked += 1
            if by_name[name] != label:
                mismatches.append({"file": name, "csv": label, "filename": by_name[name]})
    return {"present": True, "checked": checked, "n_mismatch": len(mismatches),
            "examples": mismatches[:limit_examples]}


# --------------------------------------------------------------------------- #
# per-group selection + split
# --------------------------------------------------------------------------- #
@dataclass
class GroupData:
    """Everything one group needs: charset, the three splits, and what was dropped."""

    group: GroupCfg
    charset: List[str]
    train: List[Record]
    val: List[Record]
    test: List[Record]
    n_raw: int = 0
    n_dropped: int = 0
    note: str = ""
    charset_mismatch: bool = False

    def all_records(self) -> List[Record]:
        return list(self.train) + list(self.val) + list(self.test)


def select_records(records: Sequence[Record], group: GroupCfg
                   ) -> Tuple[List[Record], List[str], int, str]:
    """Keep the records this group owns and apply its charset policy.

    Uses :func:`captcha.charset.resolve_labels` so training sees exactly the
    labels the report accounted for -- including ``map``-mode folding.
    """
    sizes = {tuple(s) for s in group.sizes}
    subset = [r for r in records if r.size in sizes]

    # "fixed": the configured charset is the alphabet, full stop -- no frequency
    # threshold, nothing derived from what this particular dump happens to
    # contain. Use it when the source's alphabet is known (e.g. a 0-9A-Z captcha)
    # and the model must be able to emit every character of it, including ones
    # that are rare or absent here. Labels carrying anything outside it are
    # dropped, because they cannot be encoded.
    if group.charset_mode == "fixed":
        if not group.charset:
            raise ValueError(f"group {group.group_id}: charset_mode 'fixed' "
                             "needs an explicit charset in the config")
        charset = sorted(set(group.charset))
        allowed = set(charset)
        kept = [r for r in subset if set(r.label) <= allowed]
        note = f"fixed alphabet from config ({len(charset)} chars)"
        return kept, charset, len(subset) - len(kept), note

    charset, resolved, _tail, note = resolve_labels(
        [r.label for r in subset], group.charset_mode, group.min_freq)
    kept = [replace(r, label=final) for r, final in zip(subset, resolved)
            if final is not None]
    return kept, charset, len(subset) - len(kept), note


def split_records(records: Sequence[Record], val_frac: float = 0.1,
                  test_frac: float = 0.1, seed: int = 1337
                  ) -> Tuple[List[Record], List[Record], List[Record]]:
    """Stratified-by-label-length split, assigned per md5 content group."""
    rng = random.Random(seed)
    by_md5: Dict[str, List[Record]] = defaultdict(list)
    for r in records:
        by_md5[r.md5].append(r)

    # A dup group's records share content, so they share a label length in
    # practice; use the longest to keep stratification conservative.
    buckets: Dict[int, List[List[Record]]] = defaultdict(list)
    for group in by_md5.values():
        buckets[max(len(r.label) for r in group)].append(group)

    train: List[Record] = []
    val: List[Record] = []
    test: List[Record] = []
    for length in sorted(buckets):
        groups = buckets[length][:]
        rng.shuffle(groups)
        total = sum(len(g) for g in groups)
        # Quotas are per label-length bucket, so every length is represented in
        # all three splits rather than the first buckets eating the whole quota.
        test_target = int(round(total * test_frac))
        val_target = int(round(total * val_frac))
        n_test = n_val = 0
        for group in groups:
            if n_test < test_target:
                test.extend(group)
                n_test += len(group)
            elif n_val < val_target:
                val.extend(group)
                n_val += len(group)
            else:
                train.extend(group)
    return train, val, test


def prepare_group(records: Sequence[Record], group: GroupCfg,
                  train_cfg: Optional[TrainCfg] = None) -> GroupData:
    train_cfg = train_cfg or TrainCfg()
    kept, charset, dropped, note = select_records(records, group)
    if not kept:
        raise ValueError(f"group {group.group_id}: no records survived selection")
    tr, va, te = split_records(kept, train_cfg.val_frac, train_cfg.test_frac,
                               train_cfg.seed)
    mismatch = bool(group.charset) and "".join(charset) != group.charset
    if not group.charset:
        group.charset = "".join(charset)
    return GroupData(group=group, charset=charset, train=tr, val=va, test=te,
                     n_raw=len(kept) + dropped, n_dropped=dropped, note=note,
                     charset_mismatch=mismatch)


# --------------------------------------------------------------------------- #
# datasets
# --------------------------------------------------------------------------- #
class CaptchaDataset(Dataset):
    """Yields ``{'image', 'label', 'encoded', 'name'}``.

    Augmentation RNG is seeded per (seed, index) instead of sharing one Generator,
    which keeps it reproducible and safe across DataLoader workers.
    """

    def __init__(self, records: Sequence[Record], data_dir: Path,
                 preproc: PreprocCfg, charset: Sequence[str],
                 aug: Optional[AugCfg] = None, seed: int = 0, limit: int = 0):
        records = list(records)
        if limit and len(records) > limit:
            # ``split_records`` appends label-length buckets in ascending order,
            # so the train list is length-sorted, not shuffled, and a head slice
            # is not a uniform subsample -- it takes every short label first.
            # Measured on g_80x30: all 10 of the length-3 records fall inside the
            # first 250 of 1562, and the lone length-5 record falls outside. On a
            # group with a wide length range (g_250x40 spans 3-9) a ``--limit``
            # run would see no long labels at all and measure a different
            # distribution than the full one.
            random.Random(seed).shuffle(records)
            records = records[:limit]
        self.records = records
        self.data_dir = Path(data_dir)
        self.preproc = preproc
        self.charset = list(charset)
        self.index = build_index(self.charset)
        self.aug = aug
        self.seed = int(seed)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, i: int) -> Dict[str, object]:
        rec = self.records[i]
        path = rec.path(self.data_dir)
        if self.aug is not None:
            rng = np.random.default_rng((self.seed, i))
            image, _info = preprocess_train(path, self.preproc, self.aug, rng)
        else:
            image, _info = preprocess_path(path, self.preproc)
        encoded = [self.index[ch] for ch in rec.label]
        return {"image": torch.from_numpy(image), "label": rec.label,
                "encoded": encoded, "name": rec.name}


def collate(batch: List[Dict[str, object]]):
    """Stack images; concatenate targets (1-D) with their per-sample lengths.

    Within a group every image has the same width, so ``input_lengths`` is a
    constant and cuDNN can use its fused CTC path.
    """
    images = torch.stack([b["image"] for b in batch], dim=0)  # type: ignore[arg-type]
    labels = [str(b["label"]) for b in batch]
    names = [str(b["name"]) for b in batch]
    lengths = torch.tensor([len(b["encoded"]) for b in batch], dtype=torch.long)  # type: ignore[arg-type]
    flat = [c for b in batch for c in b["encoded"]]  # type: ignore[union-attr]
    targets = torch.tensor(flat, dtype=torch.long)
    return images, targets, lengths, labels, names


class WidthBucketBatchSampler(Sampler):
    """Batch indices so every sample in a batch shares a width -- used only when a
    group merges several sizes, where widths differ across samples.

    Exact-width bucketing means no padding and therefore no ``input_lengths``
    bookkeeping at all.
    """

    def __init__(self, widths: Sequence[int], batch_size: int, shuffle: bool = True,
                 seed: int = 0):
        self.buckets: Dict[int, List[int]] = defaultdict(list)
        for idx, w in enumerate(widths):
            self.buckets[int(w)].append(idx)
        self.batch_size = int(batch_size)
        self.shuffle = bool(shuffle)
        self.seed = int(seed)
        self.epoch = 0
        self._batches = self._build()

    def _build(self) -> List[List[int]]:
        batches: List[List[int]] = []
        for width in sorted(self.buckets):
            idxs = self.buckets[width][:]
            for start in range(0, len(idxs), self.batch_size):
                batches.append(idxs[start:start + self.batch_size])
        return batches

    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)

    def __iter__(self):
        batches = [b[:] for b in self._batches]
        if self.shuffle:
            rng = random.Random(self.seed + self.epoch)
            rng.shuffle(batches)
            for b in batches:
                rng.shuffle(b)
        yield from batches

    def __len__(self) -> int:
        return len(self._batches)


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def make_loader(dataset: CaptchaDataset, group: GroupCfg, batch_size: int,
                shuffle: bool, num_workers: int, device: torch.device,
                seed: int = 0) -> DataLoader:
    pin = device.type == "cuda"
    common = dict(num_workers=max(0, num_workers), collate_fn=collate,
                  pin_memory=pin)
    if num_workers > 0:
        common.update(persistent_workers=True, prefetch_factor=4)

    if group.is_merged:
        widths = [r.width_at(group.height) for r in dataset.records]
        sampler = WidthBucketBatchSampler(widths, batch_size, shuffle=shuffle,
                                         seed=seed)
        return DataLoader(dataset, batch_sampler=sampler, **common)
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle,
                      drop_last=False, **common)


def build_loaders(gd: GroupData, preproc: PreprocCfg, aug: AugCfg,
                  data_dir: Path, batch_size: int, num_workers: int,
                  device: torch.device, train_cfg: TrainCfg, limit: int = 0):
    """Returns ``(train_loader, val_loader, test_loader, train_sampler)``."""
    val_aug = replace(aug, enabled=False)
    tr_ds = CaptchaDataset(gd.train, data_dir, preproc, gd.charset, aug=aug,
                           seed=train_cfg.seed, limit=limit)
    va_ds = CaptchaDataset(gd.val, data_dir, preproc, gd.charset, aug=val_aug,
                           seed=train_cfg.seed)
    te_ds = CaptchaDataset(gd.test, data_dir, preproc, gd.charset, aug=val_aug,
                           seed=train_cfg.seed)
    tr_loader = make_loader(tr_ds, gd.group, batch_size, True, num_workers, device,
                            train_cfg.seed)
    va_loader = make_loader(va_ds, gd.group, batch_size, False, num_workers, device)
    te_loader = make_loader(te_ds, gd.group, batch_size, False, num_workers, device)
    sampler = tr_loader.batch_sampler if gd.group.is_merged else None
    return tr_loader, va_loader, te_loader, sampler
