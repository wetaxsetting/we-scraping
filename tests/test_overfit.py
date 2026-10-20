"""Phase 2 gate: the model must be able to memorise a handful of samples.

If the architecture, the charset offset, the ``(T, B, C)`` permute, the
``input_lengths`` wiring or the CTC blank index is wrong, the network *cannot*
drive the loss to zero on a handful of images -- and nothing else catches that reliably. A
model that trains to 90% on a big set can still be subtly wrong; a model that
cannot overfit a handful of samples is definitely wrong.

Run::

    pytest tests/ -q
    python tests/test_overfit.py          # same check, no pytest needed
"""

from __future__ import annotations

import sys
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from captcha.config import Config, default_config_path   # noqa: E402
from captcha.ctc import greedy_decode                    # noqa: E402
from captcha.dataset import (CaptchaDataset, collate, scan_index,  # noqa: E402
                             select_records)
from captcha.model import CRNN, conv_out_width           # noqa: E402

GROUP = "g_80x30"
N_SAMPLES = 16
EPOCHS = 400
TARGET = 1.0


def _tiny_model(charset: str, height: int) -> CRNN:
    """Same architecture, smaller hidden size so the gate stays fast on CPU.

    The point is the *wiring*, not the capacity -- a smaller model reaches the
    same conclusion about whether the plumbing is right.
    """
    return CRNN(num_classes=len(charset) + 1, in_channels=1, hidden=64,
                rnn_layers=1, dropout=0.0, input_height=height)


def test_conv_out_width_matches_the_real_cnn():
    """``conv_out_width`` is what ``input_lengths`` is derived from; if it drifts
    from the actual pooling arithmetic, CTC silently trains on the wrong lengths."""
    model = _tiny_model("0123456789", 32)
    for width in (8, 17, 40, 68, 85, 137, 200):
        x = torch.zeros(1, 1, 32, width)
        assert model(x).shape[0] == conv_out_width(width) == model.cnn(x).shape[3]


def test_overfits_a_small_batch():
    cfg_path = default_config_path(ROOT)
    if not cfg_path.exists():
        raise SystemExit(f"{cfg_path} missing; run "
                         f"'python -m captcha.inspect_data --write-config "
                         f"configs/groups.yaml' first")
    cfg = Config.load(cfg_path)
    group = cfg.by_id(GROUP)
    data_dir = ROOT / cfg.data_dir

    records, _ = scan_index(data_dir)
    kept, charset, _dropped, _note = select_records(records, group)
    assert charset, f"{GROUP} produced an empty charset"

    # Stratify the sample across label lengths so the gate exercises several
    # target lengths rather than N_SAMPLES copies of the same one. The quota is
    # filled round-robin rather than as an even split: g_80x30 is overwhelmingly
    # 4-character (1939 of 1952, with 12 of length 3 and exactly 1 of length 5),
    # so dividing evenly by the number of buckets caps that lone sample at 1 and
    # silently shrinks the batch to 11 instead of the 16 the gate is named for.
    by_len: dict[int, list] = {}
    for r in kept:
        by_len.setdefault(len(r.label), []).append(r)
    lengths = sorted(by_len)
    picked = []
    while len(picked) < N_SAMPLES:
        before = len(picked)
        for length in lengths:                      # one more from each bucket
            if len(picked) >= N_SAMPLES:
                break
            bucket = by_len[length]
            if bucket:
                picked.append(bucket.pop(0))
        if len(picked) == before:                   # every bucket is exhausted
            break
    # A group whose labels are all one length still has to yield a usable gate.
    assert len(picked) >= 4, (
        f"only {len(picked)} samples selected from {GROUP} "
        f"(lengths {dict((k, len(v)) for k, v in by_len.items())})")

    ds = CaptchaDataset(picked, data_dir, cfg.preproc_for(group), charset, aug=None)
    images, targets, lengths, labels, _names = collate([ds[i] for i in range(len(ds))])

    height = cfg.preproc.height
    model = _tiny_model(charset, height)
    width = images.shape[3]
    timesteps = conv_out_width(width)

    # The CTC design rule, checked on the real batch rather than on the config.
    max_len = int(lengths.max())
    assert timesteps >= 2 * max_len + 1, (
        f"T={timesteps} cannot emit a {max_len}-character label "
        f"(need >= {2 * max_len + 1})")
    assert len(charset) + 1 == model.num_classes
    assert int(targets.max()) < model.num_classes, "label index outside the charset"

    criterion = torch.nn.CTCLoss(blank=0, zero_infinity=True, reduction="mean")
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=0.0)

    model.train()
    for epoch in range(EPOCHS):
        opt.zero_grad()
        log_probs = model(images)
        assert log_probs.shape[0] == timesteps, (
            f"network emitted {log_probs.shape[0]} timesteps, expected {timesteps}")
        loss = criterion(log_probs, targets, torch.full_like(lengths, timesteps),
                         lengths)
        if not torch.isfinite(loss):
            raise AssertionError(f"loss became {loss} at epoch {epoch}")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()

    model.eval()
    with torch.no_grad():
        log_probs = model(images)
    # Decode with the production decoder, not a reimplementation -- the gate is
    # only meaningful if it exercises the code path that actually ships.
    predictions = [d.text for d in greedy_decode(log_probs, charset)]

    exact = sum(p == t for p, t in zip(predictions, labels)) / len(labels)
    misses = [(t, p) for t, p in zip(labels, predictions) if t != p]
    print(f"overfit {len(labels)} samples for {EPOCHS} epochs: "
          f"exact-match {exact:.1%}, final loss {float(loss.detach()):.4f}")
    if misses:
        print(f"  misses: {misses[:8]}")
    assert exact >= TARGET, (
        f"only {exact:.0%} exact-match on {len(labels)} memorisable samples; the "
        f"CTC wiring is wrong (charset offset, permute, input_lengths or blank "
        f"index). misses={misses[:5]}")


if __name__ == "__main__":
    test_conv_out_width_matches_the_real_cnn()
    print("conv_out_width: OK")
    test_overfits_a_small_batch()
    print("overfit gate: OK")
