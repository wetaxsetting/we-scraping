"""CRNN + CTC captcha recognition, one model per captcha source (size group)."""

__version__ = "0.1.0"

# Canonical preprocessed image height. Every group is resized so that H == HEIGHT
# and W is scaled proportionally; the resulting W is constant *within* a group
# because every image in a group has identical original dimensions.
HEIGHT = 32

# CTC blank label. Charset characters are encoded as 1..len(charset), so the
# classifier has len(charset) + 1 outputs.
BLANK = 0
