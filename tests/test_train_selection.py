"""Regression gate for the plateau-aware checkpoint selection in ``train.py``.

A CTC model predicting nothing but blank sits at exact-match 0.0 for many epochs.
An EM-only rule reads that as "no progress" for the whole plateau, which had two
demonstrated effects on ``g_80x30``: the LR schedule halved the LR at epoch 4 --
squarely inside the plateau, when the model needed room to escape it -- and
``best.pt`` stayed pinned to epoch 1, a blank model that ``evaluate``/``predict``
then load and report confident emptiness from. Early stopping is the third
casualty but the conditional one: it only fires if the plateau outlasts
``patience`` (8) epochs, which ``g_80x30`` escapes at epoch 6 but the harder
groups -- 62 classes, 15% of labels dropped -- plausibly do not.

These tests pin the fix: during the plateau the *loss* is what distinguishes one
epoch from the next, and it must be allowed to move the best checkpoint.

Run::

    pytest tests/ -q
    python tests/test_train_selection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from captcha.train import _improved   # noqa: E402

MIN_DELTA = 0.001


def test_exact_match_improvement_wins():
    # 0.0 -> 0.5 exact-match is progress no matter what the loss did.
    assert _improved(0.5, 9.9, 0.0, 1.0, MIN_DELTA)
    assert _improved(0.02, 5.0, 0.0, 1.0, MIN_DELTA)


def test_lower_loss_wins_while_exact_match_is_stuck_at_zero():
    """The plateau case the fix exists for."""
    assert _improved(0.0, 10.45, 0.0, 10.67, MIN_DELTA)


def test_the_plateau_keeps_a_run_alive():
    """Walk a plausible plateau: loss falls every epoch, EM stays 0.0.

    This is the regime that matters for the groups that do not escape quickly.
    Under the old EM-only rule every one of these epochs tied at 0.0, so
    ``no_improve`` climbed to 8 and early stopping ended the run at epoch 9 while
    the loss was still falling -- and ``best.pt`` stayed frozen at epoch 1. Each
    epoch here must register as improvement, and the best loss must track the
    newest, lowest one.
    """
    best_em, best_loss, no_improve = -1.0, float("inf"), 0
    losses = [13.30, 10.74, 10.67, 10.45, 10.10, 9.40, 8.10, 6.00, 3.50, 1.20]
    for loss in losses:
        if _improved(0.0, loss, best_em, best_loss, MIN_DELTA):
            best_em, best_loss, no_improve = 0.0, loss, 0
        else:
            no_improve += 1
    assert no_improve == 0, "a falling loss must not count as no-improvement"
    assert best_loss == 1.20, "best.pt must track the lowest-loss plateau epoch"


def test_flat_epoch_is_not_an_improvement():
    assert not _improved(0.0, 10.67, 0.0, 10.67, MIN_DELTA)   # identical
    assert not _improved(0.0, 10.6699, 0.0, 10.67, MIN_DELTA)  # sub-threshold


def test_a_real_plateau_still_stops_the_run():
    """The fix must not defeat early stopping -- it must only stop for the right
    reason. Once both EM and loss are flat, patience has to run out."""
    best_em, best_loss, no_improve = 0.0, 10.0, 0
    for _ in range(8):
        if _improved(0.0, 10.0, best_em, best_loss, MIN_DELTA):
            no_improve = 0
        else:
            no_improve += 1
    assert no_improve == 8, "a genuinely flat run must exhaust patience"


def test_a_worse_exact_match_is_never_preferred():
    """Once EM is moving, the loss tie-break must stay inert: a lower loss with
    meaningfully worse EM is not an improvement."""
    assert not _improved(0.80, 1.0, 0.90, 5.0, MIN_DELTA)


def test_nan_loss_does_not_fake_an_improvement():
    """``zero_infinity`` can leave a validation pass with no finite loss at all."""
    nan = float("nan")
    assert not _improved(0.0, nan, 0.0, 10.0, MIN_DELTA)
    assert not _improved(0.0, nan, 0.0, float("inf"), MIN_DELTA)
    # but a genuine EM gain must still land even when the loss is unknown
    assert _improved(0.3, nan, 0.0, float("inf"), MIN_DELTA)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("checkpoint selection gate: OK")
