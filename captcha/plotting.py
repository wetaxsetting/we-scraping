"""Training curves from ``history.csv``.

matplotlib is imported lazily and only switched to the Agg backend when saving, so
the same module works inside a Colab notebook (inline) and headless on Windows.
Plotting is optional: training never fails because matplotlib is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .utils import read_history


def _numeric(rows: List[Dict[str, str]], key: str) -> List[float]:
    out = []
    for row in rows:
        value = row.get(key, "")
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def plot_history(history_csv: Path, out_dir: Path, group_id: str = "",
                 show: bool = False, save: bool = True) -> List[Path]:
    """Plot loss and accuracy against epoch. Returns the paths written."""
    import matplotlib
    if save and not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = read_history(history_csv)
    if not rows:
        raise ValueError(f"no history rows in {history_csv}")
    epochs = _numeric(rows, "epoch")
    written: List[Path] = []

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    axes[0].plot(epochs, _numeric(rows, "train_loss"), label="train")
    axes[0].plot(epochs, _numeric(rows, "val_loss"), label="val")
    axes[0].set_title(f"{group_id} loss" if group_id else "loss")
    axes[0].set_xlabel("epoch")
    axes[0].legend()
    axes[0].grid(alpha=0.3)

    axes[1].plot(epochs, [v * 100 for v in _numeric(rows, "val_em")],
                 label="val exact match")
    axes[1].plot(epochs, [v * 100 for v in _numeric(rows, "val_char_acc")],
                 label="val per-char")
    axes[1].plot(epochs, [v * 100 for v in _numeric(rows, "train_em")],
                 label="train exact match", alpha=0.6)
    axes[1].set_title(f"{group_id} accuracy" if group_id else "accuracy")
    axes[1].set_xlabel("epoch")
    axes[1].set_ylabel("%")
    axes[1].legend()
    axes[1].grid(alpha=0.3)

    fig.tight_layout()
    if save:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "curves.png"
        fig.savefig(target, dpi=120)
        written.append(target)
    if show:
        plt.show()
    plt.close(fig)
    return written
