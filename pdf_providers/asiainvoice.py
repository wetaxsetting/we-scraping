"""
A-Invoice / ASIA (Công ty CP Công nghệ Châu Á — asiainvoice.vn) — MSTTCGP 0101162173.
Tra cứu: https://asiainvoice.vn/tra-cuu/  (tracuu.asiainvoice.vn và tt78.asiainvoice.vn chỉ là
trang đăng nhập cho người bán; link EinvoiceView?token= của mail mang đúng mã tra cứu).

Mã tra cứu = FKey — CHỈ có trong mail ASIA gửi ("Mã tra cứu: OTIzd1NP...", cũng là tham số `token`
của link tt78.asiainvoice.vn/EinvoiceView?token=...). Nó là base64 của "<...>;<...>"; trang nhận cả
chuỗi base64 lẫn phần trước dấu ";" sau khi giải mã. XML Thuế KHÔNG có mã này (TTKhac chỉ có
TKHNbo) → HĐ chỉ có XML từ Thuế ra `no_code`, chỉ lấy được qua link mail.

Quy trình (khảo sát + kiểm trên HĐ thật 26/10/2026, Ricoh HCM C26TRC-2258 / 2377):
  1. GET  /tra-cuu/                → cookie phiên
  2. GET  /tra-cuu/GetCaptcha      → JPEG 150x30, 5 chữ HOA. Cổng phát lại từ 1 KHO ẢNH CỐ ĐỊNH
                                     (7.561 lần tải chỉ ra 823 ảnh; 19/20 ảnh tải mới 29/10 trùng
                                     từng điểm ảnh) → tra bảng `data/asia_captcha.npz` (818 ảnh, dựng
                                     bằng scripts/build_asia_captcha_table.py). Ảnh ngoài bảng mới dùng
                                     ddddocr + .upper() (server nhận 11/15, đo 26/10/2026).
                                     TỰ HỌC: đáp án ddddocr được server nhận → lưu vào
                                     <output>/captcha_learned/asiainvoice/, lần sau tra thẳng; đáp án
                                     bảng bị từ chối → bỏ dòng đó, để ddddocr đọc lại
  3. POST /tra-cuu {FKey, Captcha} → {"status":true, "pdf":<base64>, "data":{ma_so_thue, ...}}
                                     sai captcha: {"status":false,"error":3}; không có HĐ: error 1
  4. GET  /tra-cuu/downloadxml?fkey=&captcha=  (cùng phiên, ngay sau bước 3) → XML có chữ ký
Bước tải /tra-cuu/downloadpdf trả 500 nên lấy PDF từ JSON bước 3.
Tab "Từ file" (POST /tra-cuu/SearchCT, tải XML lên) chỉ nhận XML bọc TDiep có MTDTChieu (bản NCC
gửi qua mail); XML lấy từ Thuế (chỉ HDon) bị trả 500 → không dùng.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import ROOT, check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .ocr_ddddocr import ocr_text

BASE = "https://asiainvoice.vn"
MAX_TRIES = 6
_AJAX = {"X-Requested-With": "XMLHttpRequest"}
_FKEY_RE = re.compile(r"^[A-Za-z0-9+/=;_-]{10,80}$")
TABLE_PATH = Path(__file__).with_name("data") / "asia_captcha.npz"
# Sai khác trung bình mỗi điểm ảnh (0–255). Ảnh trùng đo được 0,0; ảnh khác gần nhất ≥ 33.
# Để dư cho trường hợp bộ giải mã JPEG trên máy khác lệch vài mức xám.
MATCH_MAX_DIFF = 4.0
_LABEL_RE = re.compile(r"^[A-Z0-9]{5}$")
_STATE: dict = {}


def learned_dir() -> Path:
    """Ảnh học thêm lúc chạy: <INVOICE_OUTPUT_DIR>/captcha_learned/asiainvoice/<sha1>_<NHÃN>.png.

    Nằm ngoài pdf_providers/ để chép code mới sang server không ghi đè mất phần đã học.
    """
    return Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / "captcha_learned" / "asiainvoice"


def _decode(img: bytes):
    try:
        import numpy as np
        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(img)).convert("L"), dtype=np.int16)
    except Exception:  # noqa: BLE001 — ảnh hỏng/lạ: để ddddocr xử lý như cũ
        return None


def _key(a) -> str:
    return hashlib.sha1(a.astype("uint8").tobytes()).hexdigest()


def _state() -> dict:
    """{"base": (ảnh N×30×150, nhãn), "learned": ([ảnh], [nhãn], {key}), "rejected": {key}}."""
    if not _STATE:
        base = None
        try:
            import numpy as np
            with np.load(TABLE_PATH) as z:
                base = (z["images"].astype(np.int16), [str(x) for x in z["labels"]])
        except (OSError, ImportError, KeyError):
            pass
        imgs, labels, keys = [], [], set()
        d = learned_dir()
        for f in sorted(d.glob("*.png")) if d.is_dir() else []:
            k, _, lab = f.stem.partition("_")
            a = _decode(f.read_bytes())
            if a is not None and _LABEL_RE.match(lab):
                imgs.append(a); labels.append(lab); keys.add(k)
        _STATE.update(base=base, learned=(imgs, labels, keys), rejected=set())
    return _STATE


def _nearest(images, labels, a):
    import numpy as np
    if not len(labels) or a.shape != images.shape[1:]:
        return None
    diff = np.abs(images - a).mean(axis=(1, 2))
    i = int(diff.argmin())
    return labels[i] if diff[i] <= MATCH_MAX_DIFF else None


def table_answer(img: bytes):
    """Đáp án nếu ảnh nằm trong kho captcha đã biết, ngược lại None.

    Phần học thêm được xét TRƯỚC bảng gốc: nó chỉ có đáp án server đã nhận, nên sửa được
    cả nhãn sai của bảng gốc.
    """
    a = _decode(img)
    if a is None:
        return None
    st = _state()
    imgs, labels, _ = st["learned"]
    if imgs:
        import numpy as np
        ans = _nearest(np.stack(imgs), labels, a)
        if ans:
            return ans
    if st["base"] is None or _key(a) in st["rejected"]:
        return None
    return _nearest(*st["base"], a)


def remember(img: bytes, label: str) -> bool:
    """Lưu ảnh + đáp án server vừa NHẬN. True nếu là ảnh mới được ghi."""
    a = _decode(img)
    if a is None or not _LABEL_RE.match(label) or a.shape != (30, 150):
        return False
    st = _state()
    imgs, labels, keys = st["learned"]
    k = _key(a)
    if k in keys:
        return False
    d = learned_dir()
    try:
        from PIL import Image
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{k}.tmp"
        Image.fromarray(a.astype("uint8")).save(tmp, format="PNG")
        os.replace(tmp, d / f"{k}_{label}.png")     # nhiều worker ghi cùng lúc vẫn không ra file dở
    except OSError:
        return False
    imgs.append(a); labels.append(label); keys.add(k)
    return True


def reject(img: bytes) -> None:
    """Server từ chối đáp án lấy từ bảng gốc → bỏ dòng đó cho tới hết tiến trình (để ddddocr thử,
    đọc đúng thì remember() ghi đè vĩnh viễn)."""
    a = _decode(img)
    if a is not None:
        _state()["rejected"].add(_key(a))


class AsiaInvoiceAdapter(PdfAdapter):
    source_id = "asiainvoice"
    name = "A-Invoice (asiainvoice.vn)"
    mst_tcgp = ("0101162173",)
    hosts = ("asiainvoice.vn",)            # gồm tracuu. / tt78.asiainvoice.vn

    def __init__(self):
        self._last: tuple = ("", b"", b"")    # (fkey, pdf, xml) lần tra gần nhất — fetch_xml + fetch_pdf 1 lần captcha

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("FKey") or invoice.get("ma_tra_cuu") or "").strip()

    @staticmethod
    def _solve(img: bytes) -> tuple:
        """(đáp án, lấy từ bảng?)"""
        ans = table_answer(img)
        if ans:
            return ans, True
        return re.sub(r"[^A-Za-z0-9]", "", ocr_text(img)).upper(), False

    @classmethod
    def _captcha(cls, img: bytes) -> str:
        return cls._solve(img)[0]

    def _lookup(self, invoice: dict) -> tuple:
        """(pdf, xml) — xml rỗng nếu bước tải XML lỗi (XML chỉ là phần thêm)."""
        fkey = self.lookup_code(invoice)
        if not fkey:
            raise PdfFetchError("Không có FKey (mã tra cứu ASIA chỉ có trong mail NCC gửi)", "no_code")
        if not _FKEY_RE.match(fkey):
            raise PdfFetchError(f"FKey ASIA không đúng dạng: {fkey[:20]!r}", "no_code")
        if self._last[0] == fkey:
            return self._last[1], self._last[2]
        for _ in range(MAX_TRIES):
            s = new_session()
            request(s, "GET", f"{BASE}/tra-cuu/")
            img = request(s, "GET", f"{BASE}/tra-cuu/GetCaptcha").content
            cap, from_table = self._solve(img)
            if len(cap) != 5:                       # đọc thiếu/thừa ký tự: xin ảnh mới, khỏi gửi bừa
                continue
            r = request(s, "POST", f"{BASE}/tra-cuu", data={"FKey": fkey, "Captcha": cap},
                        headers={**_AJAX, "Referer": f"{BASE}/tra-cuu/"})
            try:
                j = r.json()
            except ValueError as e:
                raise PdfFetchError(f"ASIA trả HTTP {r.status_code}, không phải JSON", "bad_content") from e
            if j.get("status") or j.get("error") == 1:     # cả 2 đều nghĩa là captcha đúng
                if not from_table:
                    remember(img, cap)
            elif j.get("error") == 3 and from_table:
                reject(img)
            if j.get("status"):
                try:
                    pdf = base64.b64decode(j.get("pdf") or "")
                except (binascii.Error, ValueError) as e:
                    raise PdfFetchError("Trường pdf của ASIA không phải base64", "bad_content") from e
                if not is_pdf(pdf):
                    raise PdfFetchError("ASIA báo thấy HĐ nhưng không trả PDF", "bad_content")
                xml = b""
                try:
                    x = request(s, "GET", f"{BASE}/tra-cuu/downloadxml", params={"fkey": fkey, "captcha": cap})
                    if x.status_code == 200 and b"<SHDon>" in x.content:
                        xml = x.content
                except PdfFetchError:
                    pass
                self._last = (fkey, pdf, xml)
                return pdf, xml
            if j.get("error") == 1:
                raise PdfFetchError(f"ASIA: hóa đơn không tồn tại (FKey {fkey[:12]}…)", "no_pdf")
            if j.get("error") != 3:
                raise PdfFetchError(f"ASIA: {j.get('message') or j.get('error')}", "other")
        raise PdfFetchError(f"ASIA: đọc sai captcha {MAX_TRIES} lần liền", "captcha")

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf, _ = self._lookup(invoice)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        _, xml = self._lookup(invoice)
        if not xml:
            raise PdfFetchError("ASIA không trả XML", "no_pdf")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML ASIA tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Mail ASIA: tt78.asiainvoice.vn/EinvoiceView?token=<FKey>, kèm "Mã tra cứu: <FKey>" trong thân
        (các link khác của mail — asiainvoice.vn/tra-cuu/, tracuu.asiainvoice.vn — chỉ có mã trong mail).
        Chỉ đọc tham số / mã, không mở link.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        fkey = (link_params(url).get("token") or code or "").strip()
        if not _FKEY_RE.match(fkey):
            return None
        return {"ma_tra_cuu": fkey, "mst_tcgp": self.mst_tcgp[0], "tt_khac": {"FKey": fkey}}


register(AsiaInvoiceAdapter())
