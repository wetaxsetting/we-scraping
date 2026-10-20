# captcha_sources.py
"""
Registry 22 nguồn captcha (21 gốc + bitware). Mỗi source có 1 hàm fetch() riêng.

Dùng:
    from captcha_sources import fetch_captcha, list_sources, list_sources_by_status
    img_bytes, info = fetch_captcha("19_gdt")
"""
from __future__ import annotations

import base64
import html as _html
import re
import ssl
import time
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

# ============================================================
# SSL helper
# ============================================================
_SSL_CTX = None


def _get_ssl_ctx():
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX = ctx
    return _SSL_CTX


class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _get_ssl_ctx()
        return super().init_poolmanager(*args, **kwargs)


def make_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", LegacySSLAdapter())
    s.mount("http://", LegacySSLAdapter())
    return s


# ============================================================
# Helpers
# ============================================================
IMG_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")


def _is_image(data: bytes) -> bool:
    head = data[:16].lstrip()
    return any(data.startswith(m) or head.startswith(m) for m in IMG_MAGIC)


def _svg_to_png(svg_str: str, scale: float = 3.0) -> bytes:
    """Render SVG → PNG bytes."""
    import skia
    stream = skia.MemoryStream()
    stream.setMemory(svg_str.encode("utf-8"))
    skia_svg = skia.SVGDOM.MakeFromStream(stream)
    w, h = skia_svg.containerSize()
    if w <= 0 or h <= 0:
        raise ValueError(f"SVG invalid size {w}x{h}")
    surface = skia.Surface(int(w * scale), int(h * scale))
    with surface as canvas:
        canvas.clear(skia.ColorWHITE)
        canvas.scale(scale, scale)
        skia_svg.render(canvas)
    return bytes(surface.makeImageSnapshot().encodeToData())


def _decode_data_uri(uri: str) -> bytes:
    """data:image/png;base64,XXXX → bytes."""
    b64 = uri.split(",", 1)[1]
    return base64.b64decode(b64)


def _flatten_auto(img):
    """Dán ảnh RGBA lên nền tương phản với màu chữ.
    - Chữ sáng (avg > 180) → nền đen
    - Chữ tối             → nền trắng
    """
    from PIL import Image
    if img.mode not in ("RGBA", "LA", "P"):
        return img.convert("RGB")

    img = img.convert("RGBA")
    pixels = list(img.getdata())
    opaque = [p for p in pixels if p[3] > 200]

    if not opaque:
        return Image.new("RGB", img.size, (255, 255, 255))

    avg_brightness = sum(sum(p[:3]) / 3 for p in opaque) / len(opaque)

    if avg_brightness > 180:
        bg_color = (0, 0, 0, 255)      # nền đen cho chữ trắng
    else:
        bg_color = (255, 255, 255, 255)  # nền trắng cho chữ tối

    bg = Image.new("RGBA", img.size, bg_color)
    return Image.alpha_composite(bg, img).convert("RGB")


def _maybe_flatten_to_png(img_bytes: bytes) -> bytes:
    """Nếu ảnh có alpha → flatten về nền phù hợp, trả lại PNG bytes."""
    try:
        import io as _io
        from PIL import Image
        img = Image.open(_io.BytesIO(img_bytes))

        has_alpha = (
            img.mode in ("RGBA", "LA")
            or (img.mode == "P" and "transparency" in img.info)
        )
        if not has_alpha:
            return img_bytes

        img_rgb = _flatten_auto(img)
        buf = _io.BytesIO()
        img_rgb.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return img_bytes


# ============================================================
# Generic fetchers
# ============================================================
def _json_b64_fetcher(url: str, method: str = "GET",
                      json_body: dict = None,
                      headers: dict = None,
                      timeout: int = 10,
                      image_key: str = "image",
                      key_key: str = "key") -> Tuple[Optional[bytes], Dict]:
    """Generic fetcher cho JSON chứa base64 image."""
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        s = make_session()
        if method.upper() == "POST":
            r = s.post(url, headers=h, json=json_body or {}, timeout=timeout)
        else:
            r = s.get(url, headers=h, timeout=timeout)
        r.raise_for_status()
        data = r.json()

        # Xử lý nested result (ABP style)
        inner = data.get("result") if "result" in data else data
        uri = inner.get(image_key, "")
        key = inner.get(key_key, "")

        if not uri.startswith("data:image"):
            return None, {"error": f"no image data (keys={list(inner.keys())})"}
        return _decode_data_uri(uri), {"key": key, "source": "json_b64"}
    except Exception as e:
        return None, {"error": str(e)}


def _direct_image_fetcher(url: str, headers: dict = None,
                          params: dict = None,
                          timeout: int = 10) -> Tuple[Optional[bytes], Dict]:
    """Generic fetcher cho ảnh trực tiếp."""
    h = {"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.8"}
    if headers:
        h.update(headers)
    try:
        r = make_session().get(url, headers=h, params=params, timeout=timeout)
        r.raise_for_status()
        if not _is_image(r.content):
            return None, {"error": f"not an image (ctype={r.headers.get('Content-Type')})"}
        return r.content, {"source": "direct"}
    except Exception as e:
        return None, {"error": str(e)}


# ============================================================
# Fetchers (1-22)
# ============================================================
def fetch_1_einvoice_generate(timeout=10):
    """1. tracuutvan.einvoice.com.vn - CaptchaMVC (full browser headers)."""
    home = "https://tracuutvan.einvoice.com.vn/"
    base = "https://tracuutvan.einvoice.com.vn"

    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": home,
    }

    try:
        s = make_session()

        # 1. GET home
        r_home = s.get(
            home,
            headers={**browser_headers,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                               "image/avif,image/webp,image/apng,*/*;q=0.8",
                     "Upgrade-Insecure-Requests": "1",
                     "Sec-Fetch-Dest": "document",
                     "Sec-Fetch-Mode": "navigate",
                     "Sec-Fetch-Site": "none",
                     "Sec-Fetch-User": "?1"},
            timeout=timeout,
        )
        r_home.raise_for_status()

        m = re.search(
            r'id="CaptchaDeText"[^>]*value="([a-f0-9]{32})"',
            r_home.text, re.I
        )
        if not m:
            return None, {"error": "no token in home page"}
        t1 = m.group(1)

        # 2. POST /Refresh → lấy T2
        r_refresh = s.post(
            f"{base}/DefaultCaptcha/Refresh",
            data={"t": t1},
            headers={**browser_headers,
                     "Accept": "text/javascript, application/javascript, */*",
                     "X-Requested-With": "XMLHttpRequest",
                     "Sec-Fetch-Dest": "empty",
                     "Sec-Fetch-Mode": "cors",
                     "Sec-Fetch-Site": "same-origin"},
            timeout=timeout,
        )
        m2 = re.search(r'([a-f0-9]{32})', r_refresh.text)
        t2 = m2.group(1) if m2 else t1

        # 3. GET ảnh với T2
        img_url = f"{base}/DefaultCaptcha/Generate?t={t2}"
        r_img = s.get(
            img_url,
            headers={**browser_headers,
                     "Accept": "image/avif,image/webp,image/apng,image/svg+xml,"
                               "image/*,*/*;q=0.8",
                     "Sec-Fetch-Dest": "image",
                     "Sec-Fetch-Mode": "no-cors",
                     "Sec-Fetch-Site": "same-origin"},
            timeout=timeout,
        )
        r_img.raise_for_status()

        if not _is_image(r_img.content):
            return None, {
                "error": f"not an image (ctype={r_img.headers.get('Content-Type')}, "
                         f"len={len(r_img.content)})"
            }

        if len(r_img.content) < 1500:
            return None, {
                "error": f"image too small ({len(r_img.content)} bytes) — "
                         f"likely CaptchaMVC error placeholder",
                "token": t2,
                "preview": r_img.content,
            }

        return r_img.content, {"source": "einvoice_generate", "token": t2}

    except Exception as e:
        return None, {"error": str(e)}


def fetch_2_tax24(timeout=10):
    """2. tax24 - ảnh trực tiếp"""
    return _direct_image_fetcher(
        "https://tax24.com.vn/einvoice/servlet/ImageServlet",
        headers={"Referer": "https://tax24.com.vn/"},
        timeout=timeout)


def fetch_3_asiainvoice(timeout=10):
    """3. asiainvoice - ảnh trực tiếp"""
    return _direct_image_fetcher(
        "https://asiainvoice.vn/tra-cuu/GetCaptcha",
        headers={"Referer": "https://asiainvoice.vn/tra-cuu"},
        timeout=timeout)


def fetch_4_vin_hoadon(timeout=10):
    """4. vin-hoadon - JSON ABP"""
    return _json_b64_fetcher(
        "https://tracuu.vin-hoadon.com/api/services/hddt/TraCuuHoaDon/RefreshCaptcha",
        method="POST", json_body={},
        headers={"Content-Type": "application/json",
                 "Referer": "https://tracuu.vin-hoadon.com/"},
        timeout=timeout)


def fetch_5_vnpt(timeout=10):
    """5. VNPT - JSON + code (đáp án)"""
    url = "https://gateway-hst.vnpt-invoice.com.vn/admin/api/share/get-captcha"
    headers = {"User-Agent": "Mozilla/5.0",
               "Accept": "application/json",
               "Referer": "https://gateway-hst.vnpt-invoice.com.vn/"}
    try:
        r = make_session().get(url, headers=headers,
                               params={"width": "120", "height": "40"},
                               timeout=timeout)
        r.raise_for_status()
        data = r.json()
        inner = data.get("data") or {}
        img_b64 = inner.get("image") or ""
        code = inner.get("code") or ""
        if not img_b64:
            return None, {"error": "no image"}
        return base64.b64decode(img_b64), {"code": code, "source": "vnpt"}
    except Exception as e:
        return None, {"error": str(e)}


def fetch_6_vninvoice(timeout=10):
    """6. vninvoice - JSON data URI"""
    return _json_b64_fetcher(
        "https://gateway.v50.vninvoice.vn/api/portal/PortalApiService/captcha",
        timeout=timeout)


def fetch_7_smartsign(timeout=10):
    """7. smartsign - ảnh trực tiếp"""
    return _direct_image_fetcher(
        "https://tracuuhd.smartsign.com.vn/Pages/CImage.aspx",
        headers={"Referer": "https://tracuuhd.smartsign.com.vn/"},
        timeout=timeout)


def fetch_8_hoadon30s(timeout=10):
    """8. hoadon30s - ảnh trực tiếp"""
    return _direct_image_fetcher(
        "https://tracuu.hoadon30s.vn/load-capcha",
        headers={"Referer": "https://tracuu.hoadon30s.vn/"},
        timeout=timeout)


def fetch_9_xcyber(timeout=10):
    """9. xcyber - JSON ABP"""
    return _json_b64_fetcher(
        "https://bill2app.xcyber.vn/api/services/hddt/TraCuuHoaDon/RefreshCaptcha",
        method="POST", json_body={},
        headers={"Content-Type": "application/json",
                 "Referer": "https://bill2app.xcyber.vn/"},
        timeout=timeout)


def fetch_10_lienson(timeout=10):
    """10. lienson - JSON ABP"""
    return _json_b64_fetcher(
        "https://ls-invoiceapp.lienson.vn/api/services/hddt/TraCuuHoaDon/RefreshCaptcha",
        method="POST", json_body={},
        headers={"Content-Type": "application/json",
                 "Referer": "https://ls-invoiceapp.lienson.vn/"},
        timeout=timeout)


def fetch_12_vdsg(timeout=10):
    """12. vdsg-invoice - BotDetect (dùng cùng session, decode entity)."""
    home = "https://vdsg-invoice.vn/"  # bỏ www, không cần /Account/Login
    base = "https://vdsg-invoice.vn"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": home,
    }

    botdetect_re = re.compile(
        r'(BotDetectCaptcha\.ashx\?get=image[^"\s<>]*)',
        re.IGNORECASE,
    )

    try:
        s = make_session()
        r = s.get(home, headers=headers, timeout=timeout)
        r.raise_for_status()

        m = botdetect_re.search(r.text)
        if not m:
            return None, {"error": "no BotDetect URL in page"}

        img_path = m.group(1)
        # Decode HTML entity 2 lần: &amp;amp; → &amp; → &
        img_path = _html.unescape(_html.unescape(img_path))
        if not img_path.startswith("/"):
            img_path = "/" + img_path

        img_url = base + img_path
        r_img = s.get(
            img_url,
            headers={**headers,
                     "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            timeout=timeout,
        )
        r_img.raise_for_status()

        if not _is_image(r_img.content):
            return None, {"error": f"not an image (ctype={r_img.headers.get('Content-Type')!r})"}

        return r_img.content, {"source": "vdsg", "url": img_url}
    except Exception as e:
        return None, {"error": str(e)}


def fetch_13_lacviet(timeout=10):
    """13. lacviet - TẠM dùng key hard-code (sẽ hết hạn)."""
    KEY = "a249fdd5-04b7-4aa9-3536-3d6d5712ce0e"  # ← update thủ công
    url = (f"https://lacviet.hdtt.vn/api/Invoice/get-captcha-image"
           f"?key={KEY}&v={int(time.time()*1000)}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/154.0.0.0 Safari/537.36",
        "Referer": "https://lacviet.hdtt.vn/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    return _direct_image_fetcher(url, headers=headers, timeout=timeout)


def fetch_14_wetax(timeout=10):
    """14. wetax - JSON đơn giản với field 'image' (data URI base64)."""
    url = "https://api.wetax.com.vn/api/wtx/ca/v1/lookup/captcha"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wetax.com.vn/",
        "Origin": "https://wetax.com.vn",
    }
    try:
        r = make_session().get(url, headers=headers, timeout=timeout)
        r.raise_for_status()

        ctype = r.headers.get("Content-Type", "").lower()
        if "json" not in ctype:
            return None, {"error": f"not JSON (ctype={ctype!r})"}

        data = r.json()
        uri = data.get("image", "")
        if not uri.startswith("data:image"):
            return None, {"error": f"no image data (keys={list(data.keys())})"}

        img_bytes = _decode_data_uri(uri)
        return img_bytes, {"source": "wetax"}

    except Exception as e:
        return None, {"error": str(e)}


def fetch_15_cloudteam(timeout=10):
    """15. cloudteam - JSON trả URL ảnh"""
    home = "https://chanel-eportal.cloudteam.vn/"
    try:
        s = make_session()
        s.get(home, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        r = s.get("https://chanel-eportal.cloudteam.vn/refreshcaptcha",
                  headers={"User-Agent": "Mozilla/5.0",
                           "Accept": "application/json",
                           "Referer": home},
                  timeout=timeout)
        r.raise_for_status()
        data = r.json()
        html = data.get("captcha", "")
        m = re.search(r'src="([^"]+)"', html)
        if not m:
            return None, {"error": "no src in captcha html"}
        from urllib.parse import urljoin
        url = urljoin("https://chanel-eportal.cloudteam.vn",
                      m.group(1).replace("\\/", "/"))
        return _direct_image_fetcher(url, headers={"Referer": home},
                                     timeout=timeout)
    except Exception as e:
        return None, {"error": str(e)}


def fetch_16_easyinvoice_0109045760(timeout=10):
    """16. 0109045760hd.easyinvoice.vn"""
    return _direct_image_fetcher(
        "https://0109045760hd.easyinvoice.vn/Captcha/Show",
        headers={"Referer": "https://0109045760hd.easyinvoice.vn/"},
        timeout=timeout)


def fetch_17_pvoil(timeout=10):
    """17. pvoil"""
    return _direct_image_fetcher(
        "https://hoadon.pvoil.vn/Captcha/Show",
        headers={"Referer": "https://hoadon.pvoil.vn/"},
        timeout=timeout)


def fetch_19_gdt(timeout=10):
    """19. GDT - JSON + SVG"""
    url = "https://hoadondientu.gdt.gov.vn/api/captcha"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://hoadondientu.gdt.gov.vn/",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    try:
        r = make_session().get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        svg = data.get("content") or ""
        if not svg.startswith("<svg"):
            return None, {"error": "no svg content"}
        png = _svg_to_png(svg)
        return png, {"key": data.get("key", ""), "source": "gdt"}
    except Exception as e:
        return None, {"error": str(e)}


def fetch_20_ngogiaphat(timeout=10):
    """20. ngogiaphat - DevExpress DXB.axd (dùng cùng session)."""
    home = "https://tracuuonline.ngogiaphat.vn/"
    base = "https://tracuuonline.ngogiaphat.vn"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        "Referer": home,
    }

    try:
        s = make_session()

        r_home = s.get(
            home,
            headers={**headers,
                     "Accept": "text/html,application/xhtml+xml,"
                               "application/xml;q=0.9,*/*;q=0.8"},
            timeout=timeout,
        )
        r_home.raise_for_status()

        m = re.search(r'(/DXB\.axd\?DXCache=[a-f0-9-]+)', r_home.text, re.I)
        if not m:
            return None, {"error": "no DXB token in home"}
        img_url = base + m.group(1)

        r_img = s.get(
            img_url,
            headers={**headers,
                     "Accept": "image/avif,image/webp,image/apng,"
                               "image/*,*/*;q=0.8"},
            timeout=timeout,
        )
        r_img.raise_for_status()

        if _is_image(r_img.content):
            return r_img.content, {"source": "ngogiaphat", "url": img_url}

        # Fallback: parse JSONP
        text = r_img.text
        m2 = re.search(r"'result'\s*:\s*'([^']+)'", text, re.I)
        if not m2:
            return None, {
                "error": f"not image and no result in JSONP "
                         f"(ctype={r_img.headers.get('Content-Type')!r}, "
                         f"len={len(r_img.content)})"
            }

        inner_url = m2.group(1).replace("\\/", "/")
        if inner_url.startswith("/"):
            inner_url = base + inner_url

        r_img2 = s.get(
            inner_url,
            headers={**headers, "Accept": "image/*"},
            timeout=timeout,
        )
        r_img2.raise_for_status()

        if not _is_image(r_img2.content):
            return None, {"error": "inner URL not image"}

        return r_img2.content, {"source": "ngogiaphat", "url": inner_url}

    except Exception as e:
        return None, {"error": str(e)}


def fetch_21_easyinvoice_tracuu(timeout=10):
    """21. tracuu.easyinvoice.vn"""
    return _direct_image_fetcher(
        "https://tracuu.easyinvoice.vn/Captcha/Show",
        headers={"Referer": "https://tracuu.easyinvoice.vn/"},
        timeout=timeout)


def fetch_22_bitware(timeout=10):
    """22. bitware.vn - vào home lấy token rồi tải ảnh."""
    home = "https://www.bitware.vn/tracuuhoadon/"
    base = "https://www.bitware.vn"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": base + "/",
    }

    try:
        s = make_session()
        r = s.get(home, headers=headers, timeout=timeout)
        r.raise_for_status()

        m = re.search(
            r'(/tracuuhoadon/captcha/image/[a-f0-9]{32,}/)',
            r.text, re.I
        )
        if not m:
            return None, {"error": "no token in home"}
        img_url = base + m.group(1)

        return _direct_image_fetcher(
            img_url,
            headers={**headers, "Accept": "image/*"},
            timeout=timeout,
        )
    except Exception as e:
        return None, {"error": str(e)}


# ============================================================
# Registry
# ============================================================
# ============================================================
# Registry — 4 nhóm theo EM thực tế
#   pass          : >= 90% EM
#   needs_more    : 50-89% EM, cần train thêm epochs
#   not_converged : 10-49% EM, thiếu data
#   plateau       : 0-9% EM, kẹt ở đáy
#   unknown       : chưa có số liệu
# ============================================================
SOURCES = {
    # ===== ✅ PASS (>= 90% EM) — 7 site =====
    "4_vin_hoadon": {
        "name": "Visnam (vin-hoadon)",
        "fn": fetch_4_vin_hoadon,
        "status": "pass", "accuracy": 96,
        "group": "g_1500x350", "images": 1000, "charset": "10 số",
    },
    "6_vninvoice": {
        "name": "Việt Nam (vninvoice)",
        "fn": fetch_6_vninvoice,
        "status": "pass", "accuracy": 93.1,
        "group": "g_180x50", "images": 2960, "charset": "36 ch",
    },
    "9_xcyber": {
        "name": "My-Invoice (xcyber)",
        "fn": fetch_9_xcyber,
        "status": "pass", "accuracy": 94.47,
        "group": "g_180x45", "images": 0, "charset": "36 ch",
    },
    "10_lienson": {
        "name": "Liên Sơn (lienson)",
        "fn": fetch_10_lienson,
        "status": "pass", "accuracy": 97.7,
        "group": "g_300x75", "images": 6001, "charset": "10 số",
    },
    "16_easyinvoice_0109045760": {
        "name": "Netacom (0109045760)",
        "fn": fetch_16_easyinvoice_0109045760,
        "status": "pass", "accuracy": 96,
        "group": "g_150x34", "images": 1000, "charset": "10 số",
    },
    "17_pvoil": {
        "name": "PVOIL (pvoil)",
        "fn": fetch_17_pvoil,
        "status": "pass", "accuracy": 91.8,
        "group": "g_194x35", "images": 1000, "charset": "10 số",
    },
    "21_easyinvoice_tracuu": {
        "name": "Softdreams (easyinvoice)",
        "fn": fetch_21_easyinvoice_tracuu,
        "status": "pass", "accuracy": 96,
        "group": "g_150x34", "images": 1000, "charset": "10 số",
    },

    # ===== ⚠️ CẦN TRAIN THÊM (50-89% EM) — 3 site =====
    "5_vnpt": {
        "name": "VNPT (vnpt-invoice)",
        "fn": fetch_5_vnpt,
        "status": "needs_more", "accuracy": 84,
        "group": "g_120x40", "images": 6000, "charset": "27 ch",
        "note": "2 style gộp, cần --epochs 120",
    },
    "14_wetax": {
        "name": "WEBCASH (wetax)",
        "fn": fetch_14_wetax,
        "status": "needs_more", "accuracy": 52,
        "group": "g_160x60", "images": 0, "charset": "18 ch",
        "note": "Đang học, chưa hội tụ, cần --epochs 120",
    },
    "15_cloudteam": {
        "name": "Nhóm Mây (cloudteam)",
        "fn": fetch_15_cloudteam,
        "status": "needs_more", "accuracy": 84,
        "group": "g_120x40", "images": 6000, "charset": "27 ch",
        "note": "Cần --epochs 120",
    },

    # ===== 🔄 CHƯA HỘI TỤ (10-49% EM) — 2 site =====
    "12_vdsg": {
        "name": "Đông Sài Gòn (vdsg)",
        "fn": fetch_12_vdsg,
        "status": "not_converged", "accuracy": 16,
        "group": "g_250x40", "images": 166, "charset": "36 ch",
        "note": "Thiếu data + charset lớn (36) + label dài 5-10",
    },
    "22_bitware": {
        "name": "Bitware",
        "fn": fetch_22_bitware,
        "status": "not_converged", "accuracy": 24,
        "group": "g_70x23", "images": 198, "charset": "23 ch",
        "note": "Thiếu data (dưới 250)",
    },

    # ===== ⛔ PLATEAU (0-9% EM) — 7 site =====
    "1_einvoice_generate": {
        "name": "Thái Sơn (einvoice)",
        "fn": fetch_1_einvoice_generate,
        "status": "plateau", "accuracy": 0,
        "group": "g_200x70", "images": 95, "charset": "24 ch",
        "note": "Thiếu data nghiêm trọng (len 1-5 lộn xộn)",
    },
    "2_tax24": {
        "name": "Đông Nam Á (tax24)",
        "fn": fetch_2_tax24,
        "status": "plateau", "accuracy": 0,
        "group": "g_600x120", "images": 437, "charset": "26 ch",
        "note": "Inst/cls đủ nhưng clip_above hoặc config có vấn đề",
    },
    "3_asiainvoice": {
        "name": "ASIA (asiainvoice)",
        "fn": fetch_3_asiainvoice,
        "status": "plateau", "accuracy": 0,
        "group": "g_150x30", "images": 164, "charset": "26 ch",
        "note": "Thiếu data nghiêm trọng",
    },
    "7_smartsign": {
        "name": "VI NA (smartsign)",
        "fn": fetch_7_smartsign,
        "status": "plateau", "accuracy": 2,
        "group": "g_150x39", "images": 265, "charset": "36 ch",
        "note": "Gần ngưỡng, có thể nhãn bẩn (2 style gộp)",
    },
    "8_hoadon30s": {
        "name": "PA Việt Nam (hoadon30s)",
        "fn": fetch_8_hoadon30s,
        "status": "plateau", "accuracy": 0,
        "group": "g_160x75", "images": 243, "charset": "36 ch",
        "note": "Sát ngưỡng, charset mới đổi sang 36",
    },
    "19_gdt": {
        "name": "Tổng cục Thuế (GDT)",
        "fn": fetch_19_gdt,
        "status": "plateau", "accuracy": 0,
        "group": "g_600x120", "images": 437, "charset": "26 ch",
        "note": "clip_above hoặc config có vấn đề",
    },
    "20_ngogiaphat": {
        "name": "Ngô Gia Phát",
        "fn": fetch_20_ngogiaphat,
        "status": "plateau", "accuracy": 6,
        "group": "g_200x80", "images": 168, "charset": "33 ch",
        "note": "Thiếu data",
    },

    # ===== ❓ UNKNOWN — 1 site =====
    "13_lacviet": {
        "name": "Lạc Việt (accnet)",
        "fn": fetch_13_lacviet,
        "status": "unknown", "accuracy": 0,
        "group": "g_150x32", "images": 0, "charset": "?",
        "note": "Chưa có số liệu + key hard-code có thể hết hạn",
    },
}


# ============================================================
# Public API
# ============================================================

def list_sources_by_status() -> Dict[str, List[Dict]]:
    """Group sources theo 5 status: pass / needs_more / not_converged / plateau / unknown."""
    out = {
        "pass": [],
        "needs_more": [],
        "not_converged": [],
        "plateau": [],
        "unknown": [],
    }
    for s in list_sources():
        status = s["status"] if s["status"] in out else "unknown"
        out[status].append(s)
    # Sort mỗi nhóm theo accuracy giảm dần
    for status in out:
        out[status].sort(key=lambda x: -x["accuracy"])
    return out


def list_sources() -> List[Dict]:
    """Trả về list sources, có status + accuracy + metadata."""
    return [
        {
            "id": k,
            "name": v["name"],
            "status": v.get("status", "unknown"),
            "accuracy": v.get("accuracy", 0),
            "group": v.get("group", ""),
            "images": v.get("images", 0),
            "charset": v.get("charset", ""),
            "note": v.get("note", ""),
        }
        for k, v in SOURCES.items()
    ]

def fetch_captcha(source_id: str, timeout: int = 10
                  ) -> Tuple[Optional[bytes], Dict]:
    src = SOURCES.get(source_id)
    if not src:
        return None, {"error": f"unknown source: {source_id}"}
    try:
        result = src["fn"](timeout=timeout)
        if result is None:
            return None, {"error": f"{source_id}: fetcher returned None"}
        if not isinstance(result, tuple) or len(result) != 2:
            return None, {"error": f"{source_id}: invalid return type"}
        img_bytes, info = result

        # ✅ Tự động flatten ảnh trong suốt → nền tương phản
        if img_bytes:
            img_bytes = _maybe_flatten_to_png(img_bytes)

        return img_bytes, info
    except Exception as e:
        return None, {"error": f"{source_id}: {type(e).__name__}: {e}"}


# ============================================================
# URL → source_id detection
# ============================================================
URL_PATTERNS = [
    (re.compile(r'hoadondientu\.gdt\.gov\.vn/api/captcha', re.I), "19_gdt"),
    (re.compile(r'tax24\.com\.vn.*ImageServlet', re.I), "2_tax24"),
    (re.compile(r'asiainvoice\.vn.*GetCaptcha', re.I), "3_asiainvoice"),
    (re.compile(r'vin-hoadon\.com.*RefreshCaptcha', re.I), "4_vin_hoadon"),
    (re.compile(r'vnpt-invoice\.com\.vn.*get-captcha', re.I), "5_vnpt"),
    (re.compile(r'vninvoice\.vn.*PortalApiService/captcha', re.I), "6_vninvoice"),
    (re.compile(r'smartsign\.com\.vn.*CImage', re.I), "7_smartsign"),
    (re.compile(r'hoadon30s\.vn.*load-capcha', re.I), "8_hoadon30s"),
    (re.compile(r'xcyber\.vn.*RefreshCaptcha', re.I), "9_xcyber"),
    (re.compile(r'lienson\.vn.*RefreshCaptcha', re.I), "10_lienson"),
    (re.compile(r'vdsg-invoice\.vn.*BotDetectCaptcha', re.I), "12_vdsg"),
    (re.compile(r'lacviet\.hdtt\.vn.*get-captcha', re.I), "13_lacviet"),
    (re.compile(r'wetax\.com\.vn.*captcha', re.I), "14_wetax"),
    (re.compile(r'cloudteam\.vn', re.I), "15_cloudteam"),
    (re.compile(r'0109045760hd\.easyinvoice\.vn', re.I), "16_easyinvoice_0109045760"),
    (re.compile(r'pvoil\.vn', re.I), "17_pvoil"),
    (re.compile(r'tracuuonline\.ngogiaphat\.vn', re.I), "20_ngogiaphat"),
    (re.compile(r'tracuu\.easyinvoice\.vn', re.I), "21_easyinvoice_tracuu"),
    (re.compile(r'tracuutvan\.einvoice\.com\.vn', re.I), "1_einvoice_generate"),
    (re.compile(r'bitware\.vn', re.I), "22_bitware"),
]


def detect_source_from_url(url: str) -> Optional[str]:
    """Từ URL, đoán xem thuộc source nào. Trả về source_id hoặc None."""
    if not url:
        return None
    for pattern, sid in URL_PATTERNS:
        if pattern.search(url):
            return sid
    return None


# ============================================================
# Main — test tất cả sources
# ============================================================
if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"Testing {len(SOURCES)} sources...\n")
    for sid in SOURCES:
        img, info = fetch_captcha(sid)
        if img:
            status = f"✅ OK ({len(img)} bytes)"
        else:
            status = f"❌ FAIL: {info.get('error', '?')[:80]}"
        print(f"{sid:30s} → {status}")