"""
Generate captcha images with known labels for training data augmentation.
Styles mimic BotDetect-like captchas: wave distortion, noise, lines, gradients.
"""

from __future__ import annotations

import argparse
import math
import random
import string
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------- Group presets from groups.yaml ----------
GROUP_PRESETS: dict[str, dict] = {
    "g_70x23":    {"size": (70, 23),   "charset": "ABCDEFGHJKLMNOPQRSTUVWXYZ",              "label_len": (4, 4)},
    "g_80x30":    {"size": (80, 30),   "charset": "0123456789",                              "label_len": (4, 5)},
    "g_120x38":   {"size": (120, 38),  "charset": "0123456789",                              "label_len": (4, 5)},
    "g_120x40":   {"size": (120, 40),  "charset": "23456789Zabcdefghjkmnpqrtuwxyz",          "label_len": (5, 6)},
    "g_150x30":   {"size": (150, 30),  "charset": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",              "label_len": (5, 6)},
    "g_150x34":   {"size": (150, 34),  "charset": "0123456789",                              "label_len": (4, 6)},
    "g_150x39":   {"size": (150, 39),  "charset": "0123456789abcdefghijklmnopqrstuvwxyz",    "label_len": (3, 4)},
    "g_160x60":   {"size": (160, 60),  "charset": "2345678abcdefgmnpwxy",                    "label_len": (4, 4)},
    "g_160x75":   {"size": (160, 75),  "charset": "0123456789abcdefghijklmnopqrstuvwxyz",    "label_len": (5, 6)},
    "g_180x50":   {"size": (180, 50),  "charset": "0123456789abcdefghijklmnopqrstuvwxyz",    "label_len": (3, 6)},
    "g_180x64":   {"size": (180, 64),  "charset": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",    "label_len": (5, 5)},
    "g_194x35":   {"size": (194, 35),  "charset": "0123456789",                              "label_len": (4, 7)},
    "g_200x70":   {"size": (200, 70),  "charset": "ABCDEFGHIJKLMNOPQRSTUVWYZ",               "label_len": (1, 5)},
    "g_200x80":   {"size": (200, 80),  "charset": "23456789abcdefghijklmnopqrstuvxyz",       "label_len": (5, 5)},
    "g_250x40":   {"size": (250, 40),  "charset": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",    "label_len": (5, 10)},
    "g_300x75":   {"size": (300, 75),  "charset": "0123456789",                              "label_len": (5, 5)},
    "g_600x120":  {"size": (600, 120), "charset": "23456789ABCDEFGHJKMNPQRSTVWXYZ",          "label_len": (6, 7)},
    "g_1500x350": {"size": (1500, 350),"charset": "0123456789",                              "label_len": (4, 4)},
}

# Windows fonts that look captcha-like
_FONT_CANDIDATES = [
    "arial.ttf", "arialbd.ttf", "times.ttf", "timesbd.ttf",
    "cour.ttf", "courbd.ttf", "verdana.ttf", "verdanab.ttf",
    "georgia.ttf", "georgiab.ttf", "impact.ttf", "comic.ttf",
    "trebuc.ttf", "trebucbd.ttf", "tahoma.ttf", "tahomabd.ttf",
]


def _load_fonts(size: int) -> list[ImageFont.FreeTypeFont]:
    fonts = []
    for name in _FONT_CANDIDATES:
        try:
            fonts.append(ImageFont.truetype(name, size))
        except OSError:
            pass
    if not fonts:
        fonts.append(ImageFont.load_default())
    return fonts


def _random_color(lo: int = 0, hi: int = 180) -> tuple[int, int, int]:
    return tuple(random.randint(lo, hi) for _ in range(3))


def _wave_distort(img: Image.Image, amplitude: float = 3.0, period: float = 30.0) -> Image.Image:
    w, h = img.size
    arr = np.array(img)
    out = np.zeros_like(arr)
    phase_x = random.uniform(0, 2 * math.pi)
    phase_y = random.uniform(0, 2 * math.pi)
    for y in range(h):
        shift = int(amplitude * math.sin(2 * math.pi * y / period + phase_x))
        row = np.roll(arr[y], shift * (3 if arr.ndim == 3 else 1))
        out[y] = row
    arr2 = out.copy()
    for x in range(w):
        shift = int(amplitude * 0.6 * math.sin(2 * math.pi * x / (period * 0.8) + phase_y))
        col = np.roll(arr2[:, x], shift, axis=0)
        out[:, x] = col
    return Image.fromarray(out)


def generate_captcha(
    text: str,
    width: int,
    height: int,
    style: str = "random",
) -> Image.Image:
    """Generate a single captcha image for the given text and size."""
    if style == "random":
        style = random.choice(["gradient", "solid", "noise_bg"])

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    # --- Background ---
    if style == "gradient":
        c1 = _random_color(180, 255)
        c2 = _random_color(180, 255)
        for x in range(width):
            r = int(c1[0] + (c2[0] - c1[0]) * x / width)
            g = int(c1[1] + (c2[1] - c1[1]) * x / width)
            b = int(c1[2] + (c2[2] - c1[2]) * x / width)
            draw.line([(x, 0), (x, height)], fill=(r, g, b))
    elif style == "noise_bg":
        bg = np.random.randint(180, 255, (height, width, 3), dtype=np.uint8)
        img = Image.fromarray(bg)
        draw = ImageDraw.Draw(img)
    else:
        draw.rectangle([(0, 0), (width, height)], fill=_random_color(200, 255))

    # --- Noise lines behind text ---
    n_lines = random.randint(2, max(3, height // 10))
    for _ in range(n_lines):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=_random_color(120, 200), width=1)

    # --- Draw text char by char ---
    font_size = int(height * random.uniform(0.5, 0.75))
    font_size = max(10, font_size)
    fonts = _load_fonts(font_size)

    n = len(text)
    char_w = width / (n + 1.5)
    x_start = char_w * 0.75

    for i, ch in enumerate(text):
        font = random.choice(fonts)
        color = _random_color(0, 100)

        char_img = Image.new("RGBA", (int(char_w * 1.5), int(height * 1.2)), (0, 0, 0, 0))
        cd = ImageDraw.Draw(char_img)
        bbox = cd.textbbox((0, 0), ch, font=font)
        cw, ch_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cd.text((-bbox[0], -bbox[1]), ch, font=font, fill=color + (255,))

        angle = random.uniform(-25, 25)
        char_img = char_img.rotate(angle, expand=True, resample=Image.BICUBIC)

        cx = int(x_start + i * char_w)
        cy = int((height - ch_h) / 2 + random.randint(-height // 8, height // 8))
        img.paste(char_img, (cx, cy), char_img)

    # --- Noise lines over text ---
    for _ in range(random.randint(1, 3)):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw = ImageDraw.Draw(img)
        draw.line([(x1, y1), (x2, y2)], fill=_random_color(60, 160), width=random.randint(1, 2))

    # --- Dot noise ---
    n_dots = random.randint(width * height // 200, width * height // 80)
    draw = ImageDraw.Draw(img)
    for _ in range(n_dots):
        x, y = random.randint(0, width - 1), random.randint(0, height - 1)
        draw.point((x, y), fill=_random_color(0, 200))

    # --- Wave distortion ---
    amp = random.uniform(1.5, min(4.0, height * 0.08))
    period = random.uniform(height * 0.6, height * 1.5)
    img = _wave_distort(img, amplitude=amp, period=period)

    # --- Slight blur ---
    if random.random() < 0.4:
        img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.3, 0.8)))

    return img


def generate_for_group(
    group_id: str,
    count: int,
    output_dir: str | Path | None = None,
    label_len: tuple[int, int] | None = None,
) -> list[tuple[str, Path]]:
    """
    Generate `count` captcha images for a group preset.
    Saves as <label>_<index>.png in output_dir.
    Returns list of (label, path).
    """
    if group_id not in GROUP_PRESETS:
        raise ValueError(f"Unknown group: {group_id}. Available: {list(GROUP_PRESETS)}")

    preset = GROUP_PRESETS[group_id]
    w, h = preset["size"]
    charset = preset["charset"]
    lo, hi = label_len or preset["label_len"]

    if output_dir is None:
        output_dir = Path("generated") / group_id
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i in range(count):
        length = random.randint(lo, hi)
        label = "".join(random.choices(charset, k=length))
        img = generate_captcha(label, w, h)
        fname = f"{label}_{i:05d}.png"
        path = output_dir / fname
        img.save(path)
        results.append((label, path))
        if (i + 1) % 500 == 0:
            print(f"  [{group_id}] {i + 1}/{count}")

    print(f"[{group_id}] Generated {count} images → {output_dir}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Generate captcha training images")
    parser.add_argument("group", help="Group ID (e.g. g_150x30) or 'all'")
    parser.add_argument("-n", "--count", type=int, default=1000, help="Number of images")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output directory")
    parser.add_argument("--list", action="store_true", help="List available groups")
    args = parser.parse_args()

    if args.list or args.group == "list":
        for gid, p in sorted(GROUP_PRESETS.items()):
            print(f"  {gid:15s}  {p['size'][0]:4d}x{p['size'][1]:<4d}  "
                  f"charset={len(p['charset']):2d}  label={p['label_len']}")
        return

    if args.group == "all":
        for gid in sorted(GROUP_PRESETS):
            out = Path(args.output or "generated") / gid
            generate_for_group(gid, args.count, out)
    else:
        generate_for_group(args.group, args.count, args.output)


if __name__ == "__main__":
    main()
