# debug_ngogiaphat_raw.py
from captcha_sources import make_session
import re

base = "https://tracuuonline.ngogiaphat.vn"
home = base + "/"
s = make_session()
h = {"User-Agent": "Mozilla/5.0", "Referer": home}

# 1. Home
r1 = s.get(home, headers=h, timeout=15)
print("=== HOME ===")
print("Status:", r1.status_code)
m = re.search(r'(/DXB\.axd\?DXCache=[a-f0-9-]+)', r1.text, re.I)
print("Token URL:", m.group(1) if m else "KHÔNG TÌM THẤY")
print("Cookies:", dict(s.cookies))
print()

if m:
    img_url = base + m.group(1)

    # Test A: GET với Accept: image/*
    print("=== GET với Accept: image/* ===")
    rA = s.get(img_url, headers={**h, "Accept": "image/*"}, timeout=15)
    print(f"Status: {rA.status_code}")
    print(f"Content-Type: {rA.headers.get('Content-Type')}")
    print(f"Body len: {len(rA.content)}")
    print(f"Body[:200]: {rA.content[:200]!r}")
    print()

    # Test B: GET với Accept: text/html
    print("=== GET với Accept: text/html ===")
    rB = s.get(img_url, headers={**h, "Accept": "text/html"}, timeout=15)
    print(f"Status: {rB.status_code}")
    print(f"Content-Type: {rB.headers.get('Content-Type')}")
    print(f"Body len: {len(rB.content)}")
    print(f"Body[:300]: {rB.text[:300]!r}")