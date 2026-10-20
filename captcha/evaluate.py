"""Metrics: exact-match, per-character accuracy, CER, confusion pairs, worst-N.

The confusion table is the key diagnostic for this dataset. If the top
substitutions are all ``c<->C, x<->X, v<->V``, that is annotation noise, not model
error -- which is why accuracy is reported on the noise-filtered set separately
from the noisy samples the charset policy dropped.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch

from .ctc import greedy_decode, input_lengths_for


def levenshtein(a: str, b: str) -> int:
    """Edit distance. Hand-rolled so scikit-learn is not a dependency."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1,
                               current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass
class Metrics:
    n: int = 0
    exact_match: float = 0.0
    mean_edit_distance: float = 0.0
    cer: float = 0.0
    loss: float = float("nan")

    @property
    def char_acc(self) -> float:
        return 1.0 - self.cer

    def as_dict(self) -> Dict[str, float]:
        return {"n": self.n, "exact_match": self.exact_match,
                "mean_edit_distance": self.mean_edit_distance, "cer": self.cer,
                "char_acc": self.char_acc, "loss": self.loss}


@dataclass
class Row:
    """One evaluated sample."""

    name: str
    truth: str
    pred: str
    confidence: float
    min_char_prob: float = 0.0
    loss: float = float("nan")

    @property
    def correct(self) -> bool:
        return self.pred == self.truth


def compute_metrics(rows: Sequence[Row]) -> Metrics:
    if not rows:
        return Metrics()
    n = len(rows)
    exact = sum(1 for r in rows if r.correct)
    distances = [levenshtein(r.pred, r.truth) for r in rows]
    truth_chars = sum(len(r.truth) for r in rows)
    total_edit = sum(distances)
    losses = [r.loss for r in rows if not np.isnan(r.loss)]
    return Metrics(
        n=n,
        exact_match=exact / n,
        mean_edit_distance=total_edit / n,
        cer=(total_edit / truth_chars) if truth_chars else 0.0,
        loss=float(np.mean(losses)) if losses else float("nan"),
    )


def confusion_pairs(rows: Sequence[Row], top: int = 20) -> List[Dict[str, object]]:
    """Most frequent substitutions, aligned positionally by edit path.

    A cheap position-wise comparison is used rather than full alignment: for
    near-miss predictions (the interesting case) the strings have equal length and
    the pairs are exact.
    """
    pairs: Counter = Counter()
    for row in rows:
        if row.pred == row.truth:
            continue
        for a, b in zip(row.pred, row.truth):
            if a != b:
                pairs[(b, a)] += 1        # (truth, predicted)
        # length mismatch: record the insertions/deletions as a marker
        if len(row.pred) != len(row.truth):
            pairs[("<len>", f"{len(row.truth)}->{len(row.pred)}")] += 1
    return [{"truth": t, "pred": p, "count": c} for (t, p), c in pairs.most_common(top)]


@torch.no_grad()
def predict_loader(model: torch.nn.Module, loader, charset: Sequence[str],
                   device: torch.device, criterion=None,
                   max_batches: int = 0) -> List[Row]:
    """Run a loader and decode every sample."""
    model.eval()
    rows: List[Row] = []
    for step, (images, targets, target_lengths, labels, names) in enumerate(loader):
        if max_batches and step >= max_batches:
            break
        images = images.to(device, non_blocking=True)
        log_probs = model(images)
        decoded = greedy_decode(log_probs, charset)

        # CTCLoss is instantiated with reduction="none" in train.py, so this is a
        # per-sample vector -- taking a single scalar for the whole batch would
        # attribute one sample's difficulty to all of them.
        losses: List[float] = [float("nan")] * len(names)
        if criterion is not None and targets.numel() > 0:
            input_lengths = input_lengths_for(images.shape[0], log_probs.shape[0],
                                              log_probs.device)
            loss_vec = criterion(log_probs.float(), targets.to(log_probs.device),
                                 input_lengths, target_lengths.to(log_probs.device))
            values = loss_vec.detach().reshape(-1).cpu().tolist()
            if len(values) == 1 and len(names) > 1:      # a reduced criterion
                values = values * len(names)
            for i, value in enumerate(values[:len(names)]):
                # CTCLoss is never exactly zero. A zero here means zero_infinity
                # masked a sample whose target was longer than T; it carries no
                # gradient, so report it as unknown rather than as a perfect loss.
                losses[i] = float("nan") if value == 0.0 else float(value)

        for i, (name, truth, dec) in enumerate(zip(names, labels, decoded)):
            rows.append(Row(name=str(name), truth=str(truth), pred=dec.text,
                            confidence=dec.confidence,
                            min_char_prob=dec.min_char_prob, loss=losses[i]))
    return rows


def worst_rows(rows: Sequence[Row], n: int = 30) -> List[Row]:
    return sorted((r for r in rows if not r.correct),
                  key=lambda r: (-levenshtein(r.pred, r.truth), r.confidence))[:n]


def length_histogram(rows: Sequence[Row]) -> Dict[int, int]:
    return dict(sorted(Counter(len(r.truth) for r in rows).items()))


def build_report(rows: Sequence[Row], top_confusions: int = 20,
                 n_worst: int = 30) -> Dict[str, object]:
    return {
        "metrics": compute_metrics(rows).as_dict(),
        "confusions": confusion_pairs(rows, top_confusions),
        "worst": [{"file": r.name, "truth": r.truth, "pred": r.pred,
                   "confidence": round(r.confidence, 4)} for r in worst_rows(rows, n_worst)],
        "length_hist": length_histogram(rows),
    }


def format_report(title: str, report: Dict[str, object],
                  noisy: Optional[Dict[str, object]] = None) -> str:
    m = report["metrics"]
    lines = [
        f"=== {title} ===",
        f"  samples           : {m['n']}",
        f"  exact match       : {m['exact_match']:.2%}",
        f"  per-character acc : {m['char_acc']:.2%}   (CER {m['cer']:.4f})",
        f"  mean edit dist    : {m['mean_edit_distance']:.3f}",
        f"  loss              : {m['loss']:.4f}",
    ]
    if report.get("confusions"):
        lines.append("  top substitutions (truth -> predicted):")
        for c in report["confusions"][:8]:  # type: ignore[index]
            lines.append(f"    {c['truth']!r} -> {c['pred']!r}  x{c['count']}")
    if noisy is not None:
        nm = noisy["metrics"]
        lines.append(f"  -- noise-filtered subset (labels outside the charset) --")
        lines.append(f"     samples {nm['n']}, exact match {nm['exact_match']:.2%} "
                     f"(these labels the model cannot possibly emit)")
    if report.get("worst"):
        lines.append("  worst samples:")
        for w in report["worst"][:5]:  # type: ignore[index]
            lines.append(f"    {w['file']}  truth={w['truth']!r} pred={w['pred']!r} "
                         f"conf={w['confidence']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_group_model(run_dir: Path, device: torch.device,
                      charset: Optional[Sequence[str]] = None
                      ) -> Tuple[torch.nn.Module, dict]:
    """Load a checkpoint, refusing one whose charset disagrees with the caller's.

    Decoding a checkpoint with a charset it was not trained on produces confident
    nonsense rather than an error, so this is checked rather than assumed.
    """
    from .model import CRNN

    ckpt_path = Path(run_dir) / "best.pt"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"no checkpoint at {ckpt_path}; train the group first")
    ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    meta = ckpt["meta"]
    if charset is not None and meta.get("charset") != "".join(charset):
        raise ValueError(
            f"checkpoint {ckpt_path} was trained with charset "
            f"{meta.get('charset')!r} but this group now resolves to "
            f"{''.join(charset)!r}; retrain the group or restore the charset")
    model = CRNN(num_classes=meta["num_classes"], in_channels=meta["in_channels"],
                 hidden=meta["hidden"], rnn_layers=meta["rnn_layers"],
                 input_height=meta["height"])
    model.load_state_dict(ckpt["model"])
    model.to(device).eval()
    return model, meta


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json

    from .config import Config, default_config_path, runs_dir
    from .dataset import prepare_group, scan_index
    from .utils import get_device, setup_logging

    parser = argparse.ArgumentParser(description="Evaluate a trained group")
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--group", required=True)
    parser.add_argument("--split", choices=["val", "test"], default="test")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config) if args.config else default_config_path(root))
    data_dir = Path(args.data_dir) if args.data_dir else root / cfg.data_dir
    device = get_device(args.device)

    records, _skipped = scan_index(data_dir)
    group = cfg.by_id(args.group)
    group.require_unmerged("evaluate")
    gd = prepare_group(records, group, cfg.train)
    split_records_list = gd.val if args.split == "val" else gd.test

    from .dataset import CaptchaDataset, make_loader
    from dataclasses import replace as _replace

    ds = CaptchaDataset(split_records_list, data_dir, cfg.preproc_for(group), gd.charset,
                        aug=_replace(cfg.aug, enabled=False))
    loader = make_loader(ds, group, args.batch_size, False, args.num_workers, device)
    model, meta = _load_group_model(runs_dir(root, args.group), device, gd.charset)

    # reduction="none" so each row carries its own loss, matching what Row.loss
    # claims; a mean criterion would attribute one number to every sample.
    criterion = torch.nn.CTCLoss(blank=0, zero_infinity=True, reduction="none")
    rows = predict_loader(model, loader, gd.charset, device, criterion)
    report = build_report(rows)
    print(format_report(f"{args.group} / {args.split}", report))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
