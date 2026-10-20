"""CTC helpers: timestep validation, greedy decoding, confidence.

``zero_infinity=True`` is a safety net, not a strategy: it converts "this label is
longer than T" into a silent zero-gradient sample, so the model just never learns
those images and nothing complains. That is why the timestep rule is asserted at
*dataset build time* (here, via :func:`check_timesteps`) and zero-inf samples are
counted per epoch during training.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np
import torch


class TimestepError(ValueError):
    """Raised when the CNN cannot emit enough timesteps for a group's labels."""


def required_timesteps(max_label_len: int) -> int:
    """Minimum T for a label of ``max_label_len`` characters.

    The design rule is ``T >= 2L+1``, not ``T >= L``: every character needs at
    least one timestep, and each occurrence after the first needs a separating
    blank, so alternating labels force 2 per character plus a boundary.
    """
    return 2 * int(max_label_len) + 1


@dataclass
class Decoded:
    """One greedy CTC decode result."""

    text: str
    confidence: float = 0.0
    min_char_prob: float = 0.0
    char_probs: List[float] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return len(self.text) == 0


def input_lengths_for(batch_size: int, timesteps: int, device: torch.device
                      ) -> torch.Tensor:
    """All samples share T in per-group training (constant width), which is also
    what lets cuDNN take its fused CTC path."""
    return torch.full((batch_size,), int(timesteps), dtype=torch.long, device=device)


def check_timesteps(width: int, max_label_len: int, out_width: Callable[[int], int],
                    group_id: str = "?", height: Optional[int] = None) -> int:
    """Assert the group can be supervised, and return T.

    Raises :class:`TimestepError` naming the offender rather than letting the
    training loop quietly produce inf losses.
    """
    timesteps = int(out_width(width))
    need = required_timesteps(max_label_len)
    if timesteps < need:
        raise TimestepError(
            f"group {group_id}: width {width}"
            + (f" (height {height})" if height else "")
            + f" yields T={timesteps} timesteps but a {max_label_len}-character "
            f"label needs T >= {need} (= 2*maxlen+1); raise the image height, "
            f"lower the width downsampling factor, or split the group"
        )
    return timesteps


def greedy_decode(log_probs, charset: Sequence[str], blank: int = 0) -> List[Decoded]:
    """Collapse repeats, drop blanks, read off characters.

    Accepts a torch tensor or ndarray shaped ``(T, B, C)``. Confidence is the
    geometric mean of the emitted characters' probabilities; blank timesteps are
    excluded because they carry no character.
    """
    if isinstance(log_probs, torch.Tensor):
        lp = log_probs.detach().float().cpu().numpy()
    else:
        lp = np.asarray(log_probs, dtype=np.float64)
    if lp.ndim != 3:
        raise ValueError(f"expected (T, B, C) log-probs, got shape {lp.shape}")

    timesteps, batch, _ = lp.shape
    best = lp.argmax(axis=2)                      # (T, B)
    probs = np.exp(lp)

    out: List[Decoded] = []
    for b in range(batch):
        prev = -1
        chars: List[str] = []
        ps: List[float] = []
        for t in range(timesteps):
            k = int(best[t, b])
            if k != prev and k != blank:
                if not (1 <= k <= len(charset)):
                    raise ValueError(
                        f"model emitted class index {k} but the charset has "
                        f"{len(charset)} characters (1..{len(charset)} plus blank 0); "
                        f"checkpoint and charset disagree"
                    )
                chars.append(charset[k - 1])
                ps.append(float(probs[t, b, k]))
            prev = k
        text = "".join(chars)
        conf = float(np.exp(np.mean(np.log(ps)))) if ps else 0.0
        out.append(Decoded(text=text, confidence=conf,
                           min_char_prob=min(ps) if ps else 0.0, char_probs=ps))
    return out
