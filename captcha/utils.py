"""Seeding, device selection, thread tuning, logging and checkpoint IO."""

from __future__ import annotations

import csv
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import torch

log = logging.getLogger("captcha")


# --------------------------------------------------------------------------- #
# reproducibility
# --------------------------------------------------------------------------- #
def set_seed(seed: int) -> None:
    """Seed python/numpy/torch. Called once per group so runs are reproducible."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def rng_state() -> Dict[str, Any]:
    state = {"python": random.getstate(), "numpy": np.random.get_state(),
             "torch": torch.get_rng_state()}
    if torch.cuda.is_available():
        state["cuda"] = torch.cuda.get_rng_state_all()
    return state


def set_rng_state(state: Dict[str, Any]) -> None:
    random.setstate(state["python"])
    np.random.set_state(state["numpy"])
    torch.set_rng_state(state["torch"])
    if "cuda" in state and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(state["cuda"])


# --------------------------------------------------------------------------- #
# device / threads
# --------------------------------------------------------------------------- #
def get_device(pref: str = "auto") -> torch.device:
    """Device-agnostic: the same code runs on this CPU box and on Colab GPU."""
    if pref and pref != "auto":
        return torch.device(pref)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def use_amp_for(device: torch.device) -> bool:
    """AMP is CUDA-only, hard gate.

    ``torch.autocast("cpu", bfloat16)`` is a trap here: it is slower on CPU and it
    breaks CTCLoss numerics. Never allow an override.
    """
    return device.type == "cuda"


def configure_threads(num_workers: int) -> None:
    """Stop torch and OpenCV thread pools from oversubscribing each other.

    Left alone, torch grabs all 14 cores *and* each OpenCV worker spins up its own
    pool, which collapses throughput. With DataLoader workers, torch should stay
    single-threaded-ish in the main process and OpenCV must not thread at all.
    """
    cpu = os.cpu_count() or 4
    torch.set_num_threads(1 if num_workers > 0 else max(1, min(8, cpu)))
    try:
        import cv2
        cv2.setNumThreads(1 if num_workers > 0 else max(1, min(4, cpu)))
    except ImportError:  # pragma: no cover - cv2 is a declared dependency
        pass


def worker_init_fn(_worker_id: int) -> None:
    """DataLoader worker init: kill OpenCV threading inside workers."""
    try:
        import cv2
        cv2.setNumThreads(1)
    except ImportError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


# --------------------------------------------------------------------------- #
# checkpoint IO
# --------------------------------------------------------------------------- #
def save_checkpoint(path: Path, **payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(payload, tmp)
    tmp.replace(path)  # atomic: a crash mid-save cannot leave a truncated best.pt


def load_checkpoint(path: Path, map_location: str = "cpu") -> Dict[str, Any]:
    return torch.load(Path(path), map_location=map_location, weights_only=False)


# --------------------------------------------------------------------------- #
# history CSV
# --------------------------------------------------------------------------- #
HISTORY_FIELDS = ["epoch", "lr", "train_loss", "train_em", "val_loss", "val_em",
                  "val_char_acc", "zero_inf_frac", "sec"]


def append_history(path: Path, row: Dict[str, Any]) -> None:
    """Append one epoch to history.csv.

    Written every epoch (not at the end) so a Colab disconnect does not lose the
    curves, and so plotting never has to re-run training.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in HISTORY_FIELDS})


def read_history(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
