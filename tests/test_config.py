"""Config-level invariants: size routing and the merged-group guard.

Routing is how an image finds its model, and the whole per-group design rests on
size being a perfect discriminator for this dataset. A routing table that
silently maps a size to the wrong group produces confident, wrong text, so it is
checked rather than assumed.

Run::

    pytest tests/ -q
    python tests/test_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from captcha.config import Config, GroupCfg, default_config_path   # noqa: E402

# Measured sizes, from inspect_data. Hard-coded on purpose: if the dataset is
# regenerated and a size disappears, this test should be what notices.
EXPECTED_SIZES = {
    (180, 64), (120, 40), (80, 30), (300, 75), (160, 75), (600, 120), (120, 38),
    (150, 34), (194, 35), (150, 30), (1500, 350), (250, 40), (150, 39), (180, 50),
}


def _cfg() -> Config:
    return Config.load(default_config_path(ROOT))


def test_every_measured_size_has_a_group():
    cfg = _cfg()
    assert len(cfg.groups) == 18, f"expected 18 groups, got {len(cfg.groups)}"
    routed = set(cfg.size_router())
    assert routed == EXPECTED_SIZES, f"missing {EXPECTED_SIZES - routed}, " \
                                     f"unexpected {routed - EXPECTED_SIZES}"


def test_routing_is_a_bijection():
    """Each size maps back to the group that declares it -- no shadowing."""
    cfg = _cfg()
    router = cfg.size_router()
    for group in cfg.groups:
        for size in group.sizes:
            assert router[(size[0], size[1])].group_id == group.group_id


def test_charset_and_width_are_present_for_every_group():
    """Both are embedded in every checkpoint; an empty one is an untrainable
    group that would fail much later and less clearly."""
    for group in _cfg().groups:
        assert group.charset, f"{group.group_id} has no charset"
        assert len(set(group.charset)) == len(group.charset), \
            f"{group.group_id} has a duplicate charset character"
        assert group.width > 0, f"{group.group_id} has no canonical width"
        assert group.num_classes == len(group.charset) + 1


def test_fallback_picks_the_obviously_right_group():
    """Sanity on the unseen-size path: a near-miss size must land on its source."""
    cfg = _cfg()
    for size, want in [((83, 31), "g_80x30"), ((121, 41), "g_120x40"),
                       ((179, 63), "g_180x64")]:
        got, score = cfg.nearest_group(size)
        assert got.group_id == want, f"{size} -> {got.group_id}, expected {want}"
        assert score < 0.15, f"{size} -> {want} scored {score:.3f}, unexpectedly far"


def test_merged_group_is_refused_by_the_fixed_width_paths():
    """A merge is fine for batch-building but not for anything assuming one
    canonical width -- it must say so rather than squash every size silently."""
    merged = GroupCfg(group_id="g_digits", sizes=[(80, 30), (300, 75)],
                      charset="0123456789", width=85)
    assert merged.is_merged
    try:
        merged.require_unmerged("train")
    except NotImplementedError as exc:
        assert "g_digits" in str(exc)
    else:
        raise AssertionError("a merged group must not pass a fixed-width path")


def test_single_size_group_passes_the_guard():
    GroupCfg(group_id="g_80x30", sizes=[(80, 30)], charset="0123456789",
             width=85).require_unmerged("train")


def test_no_group_in_the_shipped_config_is_merged():
    """Guards are wired into train/predict/evaluate, so a merged group in
    ``groups.yaml`` would make every entry point refuse to start."""
    for group in _cfg().groups:
        group.require_unmerged("boot")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("config gate: OK")
