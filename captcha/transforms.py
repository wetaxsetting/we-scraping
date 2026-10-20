"""Image preprocessing and CTC-safe augmentation.

Order matters and is fixed: composite alpha -> grayscale -> polarity
canonicalization -> optional CLAHE -> percentile contrast stretch -> resize to
H=32 -> normalize to [-1,1] -> augment.

*Train, eval, predict and the Flask app all call ``preprocess_path``*, so there
is exactly one definition of what the model sees and no train/serve skew.
torchvision is deliberately not used for this reason.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from . import HEIGHT
from .config import AugCfg, PreprocCfg

# Augmentations that silently corrupt CTC supervision. An AugCfg carrying any of
# these is rejected outright rather than merely discouraged.
CTC_FORBIDDEN = ("hflip", "horizontal_flip", "vflip", "vertical_flip",
                 "random_resized_crop", "mixup", "cutmix", "color_jitter",
                 "random_invert", "polarity_flip")


class CtcUnsafeAugmentation(ValueError):
    """Raised when an augmentation that breaks CTC alignment is configured."""


def assert_ctc_safe(aug: AugCfg) -> None:
    """Fail loudly if a forbidden augmentation was added to AugCfg.

    Horizontal flip is the important one: it mirrors the glyphs *and* reverses the
    left-to-right ordering that CTC assumes, corrupting both the label and the
    alignment the loss is computed against.
    """
    bad = [name for name in CTC_FORBIDDEN if hasattr(aug, name)]
    if bad:
        raise CtcUnsafeAugmentation(
            "augmentation(s) " + ", ".join(bad) + " break CTC alignment/labels "
            "and are not allowed"
        )


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_image(src) -> np.ndarray:
    """Load a PNG from a path, raw bytes, or a binary file object as opaque RGB.

    Accepting bytes as well as paths keeps the Flask upload path on *exactly* the
    same code as training and the CLI -- no second decode implementation to drift.

    Never ``convert("RGB")`` an RGBA image directly: for a transparent-background
    captcha that yields black fringes around every glyph, because the transparent
    pixels are black once the alpha is thrown away.
    """
    if isinstance(src, (bytes, bytearray, memoryview)):
        src = io.BytesIO(bytes(src))
    with Image.open(src) as im:
        has_alpha = im.mode in ("RGBA", "LA") or (
            im.mode == "P" and "transparency" in im.info
        )
        if has_alpha:
            rgba = im.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(white, rgba)
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def border_ring_median(gray: np.ndarray, px: int = 2) -> float:
    """Median luminance of the ``px``-wide border ring.

    The ring is almost certainly background, and its median separates the 14
    groups cleanly (dark 0-87 vs light 149-255) where a global Otsu threshold does
    not.
    """
    mask = np.zeros(gray.shape, dtype=bool)
    mask[:px, :] = mask[-px:, :] = True
    mask[:, :px] = mask[:, -px:] = True
    return float(np.median(gray[mask]))


def resized_width(w0: int, h0: int, height: int = HEIGHT) -> int:
    """Aspect-preserving width after scaling to ``height``."""
    return max(8, int(round(w0 * height / h0)))


def _interp_for(h0: int, height: int) -> int:
    # INTER_AREA for real downscales: 1500x350 -> 137x32 is a 10.9x reduction,
    # where bilinear aliases thin strokes into gray mush.
    return cv2.INTER_AREA if (height / h0) < 0.5 else cv2.INTER_LINEAR


# --------------------------------------------------------------------------- #
# preprocessing
# --------------------------------------------------------------------------- #
def preprocess(rgb: np.ndarray, cfg: PreprocCfg, height: Optional[int] = None
               ) -> Tuple[np.ndarray, dict]:
    """RGB uint8 -> float32 (C, H, W) in [-1, 1]. Returns ``(image, info)``."""
    height = int(height or cfg.height)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h0, w0 = gray.shape

    ring = border_ring_median(gray, cfg.border_px)

    # Noise gate, before anything else touches the levels: pixels brighter than
    # the cutoff are background clutter, not ink, so push them back to the ring
    # level. Applied pre-inversion because the cutoff is stated in the source
    # image's own scale. See PreprocCfg.clip_above.
    clipped = None
    if cfg.clip_above is not None:
        clipped = gray > float(cfg.clip_above)
        gray = np.where(clipped, ring, gray).astype(np.uint8)

    inverted = bool(cfg.polarity_normalize and ring < 128.0)
    if inverted:
        gray = 255 - gray

    if cfg.clahe:
        clahe = cv2.createCLAHE(clipLimit=cfg.clahe_clip,
                                tileGridSize=(cfg.clahe_grid, cfg.clahe_grid))
        gray = clahe.apply(gray)

    lo, hi = np.percentile(gray.astype(np.float32), [cfg.stretch_lo, cfg.stretch_hi])
    span = float(hi - lo)
    if span < 1e-6:                      # flat image: avoid dividing by ~0
        span = 1.0
    w = resized_width(w0, h0, height)
    interp = _interp_for(h0, height)

    if cfg.in_channels == 3:
        src = rgb.astype(np.float32)
        if clipped is not None:
            src = np.where(clipped[..., None], ring, src)
        if inverted:
            src = 255.0 - src
        src = np.clip((src - lo) / span, 0.0, 1.0)
        ch = cv2.resize(src, (w, height), interpolation=interp)
        image = np.transpose(ch * 2.0 - 1.0, (2, 0, 1))
    else:
        g = np.clip((gray.astype(np.float32) - lo) / span, 0.0, 1.0)
        g = cv2.resize(g, (w, height), interpolation=interp)
        image = (g * 2.0 - 1.0)[None, :, :]

    info = {"orig_size": (w0, h0), "width": w, "height": height,
            "ring_before": ring, "inverted": inverted}
    return image.astype(np.float32), info


def preprocess_path(path: Path, cfg: PreprocCfg, height: Optional[int] = None
                    ) -> Tuple[np.ndarray, dict]:
    return preprocess(load_image(path), cfg, height)


# --------------------------------------------------------------------------- #
# augmentation (CTC-safe only)
# --------------------------------------------------------------------------- #
def _ring_median_float(img2d: np.ndarray, px: int = 2) -> float:
    mask = np.zeros(img2d.shape, dtype=bool)
    mask[:px, :] = mask[-px:, :] = True
    mask[:, :px] = mask[:, -px:] = True
    return float(np.median(img2d[mask]))


def apply_augment(image: np.ndarray, aug: AugCfg, rng: np.random.Generator
                  ) -> np.ndarray:
    """Augment a normalized (C, H, W) float32 image in place-ish.

    Runs *after* normalization, so the geometric/photometric transforms are the
    only source of variation and the eval path is the same code with ``aug`` off.
    """
    assert_ctc_safe(aug)
    if not aug.enabled:
        return image

    c, h, w = image.shape
    # OpenCV silently drops a trailing axis of size 1, so passing (H, W, 1) makes
    # warpAffine hand back a 2-D array and every later index/cv2.line call fails.
    # Grayscale is therefore carried as a genuine 2-D plane throughout.
    gray = c == 1
    plane = image[0] if gray else np.transpose(image, (1, 2, 0))

    def ink(value: float):
        """A colour cv2 accepts for this image's channel count."""
        return float(value) if gray else (float(value),) * c

    # --- affine: small rotation, shear, scale, translate ---------------------
    angle = rng.uniform(-aug.rotate_deg, aug.rotate_deg)
    shear = rng.uniform(-aug.shear, aug.shear)
    scale = rng.uniform(aug.scale_lo, aug.scale_hi)
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, scale)
    m[0, 1] += shear
    m[0, 2] += rng.uniform(-aug.translate, aug.translate) * w
    m[1, 2] += rng.uniform(-aug.translate, aug.translate) * h
    plane = cv2.warpAffine(plane, m, (w, h), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)

    # --- width stretch (font/kerning variation), shape preserved -------------
    if aug.width_stretch > 0:
        f = rng.uniform(1.0 - aug.width_stretch, 1.0 + aug.width_stretch)
        tmp = cv2.resize(plane, (max(8, int(round(w * f))), h),
                         interpolation=cv2.INTER_LINEAR)
        plane = cv2.resize(tmp, (w, h), interpolation=cv2.INTER_LINEAR)

    # --- photometric ---------------------------------------------------------
    gain = rng.uniform(aug.bright_lo, aug.bright_hi)
    bias = rng.uniform(-0.1, 0.1)
    plane = plane * gain + bias

    background = _ring_median_float(plane if gray else plane[:, :, 0])

    if rng.random() < aug.blur_p:
        sigma = rng.uniform(0.3, 1.0)
        plane = cv2.GaussianBlur(plane, (3, 3), sigma)

    if aug.noise_sigma > 0:
        plane = plane + rng.normal(0.0, aug.noise_sigma,
                                   size=plane.shape).astype(np.float32)

    # --- crossing lines: the highest-value augmentation for this dataset -----
    if rng.random() < aug.line_p:
        for _ in range(int(rng.integers(1, 3))):
            y0, y1 = rng.integers(0, h, size=2)
            x0, x1 = rng.integers(0, w, size=2)
            value = float(rng.uniform(0.4, 1.0) * (1 if rng.random() < 0.5 else -1))
            cv2.line(plane, (int(x0), int(y0)), (int(x1), int(y1)), ink(value),
                     thickness=1, lineType=cv2.LINE_8)

    # --- small cutout, filled with this image's own background --------------
    if rng.random() < aug.cutout_p:
        for _ in range(int(rng.integers(1, 3))):
            bw = int(rng.integers(3, aug.cutout_max_w + 1))
            bh = int(rng.integers(3, aug.cutout_max_h + 1))
            x0 = int(rng.integers(0, max(1, w - bw)))
            y0 = int(rng.integers(0, max(1, h - bh)))
            if gray:
                plane[y0:y0 + bh, x0:x0 + bw] = background
            else:
                plane[y0:y0 + bh, x0:x0 + bw, :] = background

    out = plane[None, :, :] if gray else np.transpose(plane, (2, 0, 1))
    return np.clip(out.astype(np.float32), -1.0, 1.0)


def preprocess_train(path: Path, cfg: PreprocCfg, aug: AugCfg,
                     rng: np.random.Generator) -> Tuple[np.ndarray, dict]:
    image, info = preprocess_path(path, cfg)
    return apply_augment(image, aug, rng), info
