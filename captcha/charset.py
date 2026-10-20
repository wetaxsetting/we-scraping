"""Per-group charset derivation and label-noise policy.

The dataset has a real, per-source alphabet plus a long tail of very rare
characters that are label errors (mostly case confusion and digit/letter
confusion -- e.g. ``Z:81 C:66 X:41`` inside a group whose real alphabet is
lowercase+digits, or ``l:32`` inside a group whose real alphabet is the ten
digits). A class with one training example is untrainable and actively harms the
softmax, so the tail must not silently become classes.

At the same time the tail must not be silently *deleted* either: three groups
lose >10% of their samples at the default threshold, and their tails are too
large and too structured to be pure noise -- those groups probably have a
genuinely larger alphabet. That is what ``mode='full'`` is for, and
``GroupCfg.review`` forces a human decision instead of a quiet 17% deletion.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Fraction-of-characters threshold above which a character is considered part of
# the real alphabet. 0.005 matches the analysis in the plan (drops 4.1% overall).
DEFAULT_MIN_FREQ = 0.005


@dataclass
class PolicyResult:
    """Outcome of applying a charset policy to one group's labels."""

    charset: List[str]
    kept: List[str]              # labels kept as-is
    dropped: List[str] = field(default_factory=list)
    remapped: List[Tuple[str, str]] = field(default_factory=list)  # (before, after)
    note: str = ""

    @property
    def n_total(self) -> int:
        return len(self.kept) + len(self.dropped)

    @property
    def drop_frac(self) -> float:
        return len(self.dropped) / self.n_total if self.n_total else 0.0

    def summary(self) -> str:
        return (f"charset={len(self.charset)} kept={len(self.kept)} "
                f"dropped={len(self.dropped)} ({self.drop_frac:.1%}) "
                f"remapped={len(self.remapped)}")


def char_counts(labels: Iterable[str]) -> Counter:
    counts: Counter = Counter()
    for lab in labels:
        counts.update(lab)
    return counts


def derive_core(labels: Sequence[str], min_freq: float = DEFAULT_MIN_FREQ
                ) -> Tuple[List[str], Counter]:
    """Split the observed alphabet into a core alphabet and a rare tail.

    Returns ``(core, tail)`` where ``core`` is sorted (deterministic index order)
    and ``tail`` keeps the counts so a human can judge them.
    """
    counts = char_counts(labels)
    total = sum(counts.values())
    if total == 0:
        return [], Counter()
    core = sorted(ch for ch, n in counts.items() if n / total >= min_freq)
    core_set = set(core)
    tail = Counter({ch: n for ch, n in counts.items() if ch not in core_set})
    return core, tail


def _has_both_cases(charset: Sequence[str]) -> bool:
    return any(c.isupper() for c in charset) and any(c.islower() for c in charset)


def resolve_labels(labels: Sequence[str], mode: str = "core",
                   min_freq: float = DEFAULT_MIN_FREQ
                   ) -> Tuple[List[str], List[Optional[str]], Counter, str]:
    """Decide, per label, whether it survives and under what text.

    Returns ``(charset, resolved, tail, note)`` where ``resolved[i]`` is the final
    label for ``labels[i]``, or ``None`` when that sample must be dropped.

    ``core``  -- alphabet is the frequent core; any label containing a tail
                 character is dropped. Best default: no untrainable classes.
    ``full``  -- alphabet is every observed character; nothing is dropped. Use for
                 the groups whose tail looks like a real alphabet, and compare.
    ``map``   -- alphabet is the core; a tail character is folded into its
                 case-variant core character when that exists (``Z`` -> ``z``).
                 Only valid when the core is single-case -- for a genuinely
                 mixed-case group, folding would corrupt valid labels, so it
                 degrades to ``core`` with a note.

    Training calls this too (not just the report), so the labels a model is
    trained on are exactly the ones the report accounted for.
    """
    if mode not in ("core", "full", "map"):
        # "fixed" never reaches here: select_records handles it before calling
        # this, since it needs the group's configured charset rather than labels.
        raise ValueError(
            f"unknown charset mode {mode!r} (expected core|full|map|fixed)")
    core, tail = derive_core(labels, min_freq)

    if mode == "full":
        charset = sorted(char_counts(labels))
        note = "full alphabet: tail characters kept as classes"
        if tail:
            note += f" ({len(tail)} rare chars, min count {min(tail.values())})"
        return charset, list(labels), tail, note

    note = f"core alphabet ({len(core)} chars), tail of {len(tail)} rare chars"
    fold: Dict[str, str] = {}
    if mode == "map":
        if _has_both_cases(core):
            note += ("; map disabled because the core is mixed-case -- folding "
                     "would corrupt valid labels, so this behaves as core")
        else:
            core_set_tmp = set(core)
            for ch in tail:
                swap = ch.swapcase()
                if swap in core_set_tmp:
                    fold[ch] = swap
            note += f"; folded {len(fold)} tail chars into core case-variants"

    core_set = set(core)
    resolved: List[Optional[str]] = []
    for lab in labels:
        if set(lab) <= core_set:
            resolved.append(lab)
            continue
        if not fold:
            resolved.append(None)
            continue
        fixed = "".join(fold.get(ch, ch) for ch in lab)
        resolved.append(fixed if set(fixed) <= core_set else None)
    return core, resolved, tail, note


def apply_policy(labels: Sequence[str], mode: str = "core",
                 min_freq: float = DEFAULT_MIN_FREQ) -> PolicyResult:
    """Reporting wrapper around :func:`resolve_labels`."""
    charset, resolved, _tail, note = resolve_labels(labels, mode, min_freq)
    kept: List[str] = []
    dropped: List[str] = []
    remapped: List[Tuple[str, str]] = []
    for original, final in zip(labels, resolved):
        if final is None:
            dropped.append(original)
        else:
            kept.append(final)
            if final != original:
                remapped.append((original, final))
    return PolicyResult(charset=charset, kept=kept, dropped=dropped,
                        remapped=remapped, note=note)


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #
def build_index(charset: Sequence[str]) -> Dict[str, int]:
    """Character -> CTC index. Characters are 1-based; 0 is the blank."""
    return {ch: i + 1 for i, ch in enumerate(charset)}


def encode(label: str, charset: Sequence[str]) -> List[int]:
    """Encode a label to 1-based indices, raising on any unknown character."""
    index = build_index(charset)
    try:
        return [index[ch] for ch in label]
    except KeyError as exc:
        raise ValueError(
            f"label {label!r} contains {exc.args[0]!r}, which is not in the group "
            f"charset {''.join(charset)!r}"
        ) from None


def decode(indices: Sequence[int], charset: Sequence[str]) -> str:
    """Decode 1-based indices back to text (blank index 0 is skipped)."""
    return "".join(charset[i - 1] for i in indices if i > 0)
