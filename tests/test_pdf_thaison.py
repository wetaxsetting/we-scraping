"""Adapter Thái Sơn E-invoice (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 23/10/2026: einvoice.vn (Jollibee C26MXA-211773, Kiến An C24TKA-6/7/8/11/14) và
einvoice.mmvietnam.com (MM Mega Market C26MAA, 8 HĐ) — 14/14 PDF đúng số HĐ, chữ trùng bản PDF trong mail.
Cổng riêng thêm cùng ngày (qua fetch_pdf, 9/9 PDF đúng số HĐ, có MST người mua 1201496252):
vat.ggg.com.vn (Cổng Vàng C23TCV-176324, C25MCV-984129/2576211), invoice.kfcvietnam.com.vn
(KFC K23TTS-2901, C25MTS-3321), invoice.zaloplatforms.vn (Zalo C26TYA-12912/23351),
hddt.cellphones.com.vn (Diệu Phúc C23TDP-10327/1607).

Run::

    pytest tests/test_pdf_thaison.py -q
"""

from __future__ import annotations

import base64
import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import thaison          # noqa: E402

GEN = "https://einvoice.vn"
MM = "https://einvoice.mmvietnam.com"
T = "0d2709dd06794cf9a059868833b47f58"
GUID = "eb13a2f9-a3ee-4f7c-bfde-0de0ecc1293b"


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):                      # đủ dài để vượt ngưỡng is_pdf
        page.insert_text((40, 40 + i * 18), f"{text} dong {chr(97 + i % 26)}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class _Resp:
    def __init__(self, text="", status=200, content=None, url=""):
        self.status_code, self.text, self.url = status, text, url
        self.content = content if content is not None else text.encode("utf-8")


FORM = _Resp(f'<img id="CaptchaImage" src="/DefaultCaptcha/Generate?t={T}"/>'
             f'<input id="CaptchaDeText" name="CaptchaDeText" type="hidden" value="{T}" />')
IMG = _Resp(content=b"\x89PNG" + b"x" * 3000)
BAD_IMG = _Resp(content=b"GIF89a" + b"x" * 1200)

GEN_WRONG = _Resp('<div class="offset-md-5 text-danger fix-text-danger">Mã kiểm tra không đúng.</div>'
                  '<input type="hidden" id="IsCaptchaValid" value="" />')
GEN_NOT_FOUND = _Resp('<div class="offset-md-5 text-danger fix-text-danger">Mã nhận hóa đơn không đúng.</div>'
                      '<input type="hidden" id="IsCaptchaValid" value="" />')
GEN_OK = _Resp('<div class="offset-md-5 text-danger fix-text-danger"></div>'
               '<input type="hidden" id="IsCaptchaValid" value="true" />'
               '<a href="/tra-cuu/tai-hoa-don-dien-tu?format=pdf">PDF</a>')

MM_WRONG = _Resp('<div id="listresult"><button>&times;</button><strong>Error:</strong> '
                 'Mã bảo mật không đúng!</div>', url=f"{MM}/tra-cuu-hoa-don")
MM_NOT_FOUND = _Resp('<strong>Error:</strong> Không tìm thấy hóa đơn có thông tin vừa nhập!</div>',
                     url=f"{MM}/tra-cuu-hoa-don")
MM_OK = _Resp(f'<script>function downloadFile() {{ window.location.href = "/tai-ve-hoa-don/{GUID}"; }}</script>',
              url=f"{MM}/xem-hoa-don")


def _setup(monkeypatch, calls, posts, img=IMG, file=None, answers=("ABCD",)):
    """posts = [phản hồi POST tìm HĐ lần lượt]; file = phản hồi tải ZIP."""
    posts, answers = list(posts), list(answers)
    file = file if file is not None else _Resp(content=_zip({"1_X_768354/1_X_768354.pdf": _pdf_with("So 768354"),
                                                             "1_X_768354/1_X_768354.xml": b"<x/>"}))

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url in (f"{GEN}/tra-cuu", f"{MM}/"):
            return FORM
        if url.endswith("/DefaultCaptcha/Generate"):
            return img
        if method == "POST":
            return posts.pop(0)
        if "/tai-hoa-don-dien-tu" in url or "/tai-ve-hoa-don/" in url:
            return file
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(thaison, "request", fake)
    monkeypatch.setattr(thaison, "new_session", lambda: object())
    monkeypatch.setattr(thaison, "solve_captcha",
                        lambda img, group, min_conf=0.5: answers.pop(0) if len(answers) > 1 else answers[0])


INV_MM = {"shdon": 768354, "khhdon": "C26MAA", "khmshdon": "1", "nbmst": "0302249586",
          "lookup_url": "http://einvoice.mmvietnam.com",
          "tt_khac": {"DC TC": "http://einvoice.mmvietnam.com", "Mã TC": "VSOJKEFHUPMQSAHR"}}
INV_GEN = {"shdon": 768354, "khhdon": "C26MXA", "khmshdon": "1", "nbmst": "0303883266",
           "lookup_url": "https://einvoice.vn/tra-cuu",
           "tt_khac": {"DC TC": "https://einvoice.vn/tra-cuu", "Mã TC": "69AEIZEURC3"}}


def test_store_portal_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [MM_OK])
    assert thaison.ThaisonAdapter().fetch_pdf(INV_MM).startswith(b"%PDF")
    post = [kw for m, u, kw in calls if m == "POST"][0]
    assert post["data"] == {"MA_NHAN_HOA_DON": "VSOJKEFHUPMQSAHR", "CaptchaDeText": T, "CaptchaInputText": "ABCD"}
    assert [u for m, u, _ in calls if m == "POST"] == [f"{MM}/xem-hoa-don"]
    assert any(u == f"{MM}/tai-ve-hoa-don/{GUID}" for _, u, _ in calls)     # bản gốc, không ?printFlag
    cap = [kw for _, u, kw in calls if u.endswith("/DefaultCaptcha/Generate")][0]
    assert cap["params"] == {"t": T} and cap["headers"]["Referer"] == f"{MM}/"   # thiếu Referer = ảnh lỗi


def test_generic_portal_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [GEN_OK])
    assert thaison.ThaisonAdapter().fetch_pdf(INV_GEN).startswith(b"%PDF")
    post = [(u, kw) for m, u, kw in calls if m == "POST"][0]
    assert post[0] == f"{GEN}/tra-cuu"
    assert post[1]["data"] == {"MaNhanHoaDon": "69AEIZEURC3", "CaptchaDeText": T, "CaptchaInputText": "ABCD"}
    dl = [kw for _, u, kw in calls if u == f"{GEN}/tra-cuu/tai-hoa-don-dien-tu"][0]
    assert dl["params"] == {"format": "pdf"}


def test_captcha_group_is_forced(monkeypatch):
    seen = []
    _setup(monkeypatch, [], [MM_OK])
    monkeypatch.setattr(thaison, "solve_captcha", lambda img, group, min_conf=0.5: seen.append(group) or "ABCD")
    thaison.ThaisonAdapter().fetch_pdf(INV_MM)
    assert seen == ["g_200x70"]


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV_MM, khmshdon=1, tong_tien=Decimal("1449298"), ngay_lap=date(2026, 5, 13), ma_tra_cuu=None)
    _setup(monkeypatch, [], [MM_OK])
    assert thaison.ThaisonAdapter().fetch_pdf(inv).startswith(b"%PDF")


@pytest.mark.parametrize("inv,wrong,ok", [(INV_MM, MM_WRONG, MM_OK), (INV_GEN, GEN_WRONG, GEN_OK)])
def test_retries_wrong_captcha_with_new_captcha(monkeypatch, inv, wrong, ok):
    calls = []
    _setup(monkeypatch, calls, [wrong, ok])
    assert thaison.ThaisonAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert sum(u.endswith("/DefaultCaptcha/Generate") for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [], [GEN_WRONG] * thaison.CAPTCHA_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_GEN)
    assert ei.value.kind == "captcha"


def test_does_not_send_bad_answer(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [], answers=("ABC",))
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_MM)
    assert ei.value.kind == "captcha"
    assert not any(m == "POST" for m, _, _ in calls)


def test_low_confidence_takes_new_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [MM_OK])
    seq = [PdfFetchError("không chắc", "captcha"), "ABCD"]

    def solve(img, group, min_conf=0.5):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(thaison, "solve_captcha", solve)
    assert thaison.ThaisonAdapter().fetch_pdf(INV_MM).startswith(b"%PDF")
    assert sum(m == "POST" for m, _, _ in calls) == 1


def test_error_image_is_not_solved(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [], img=BAD_IMG)
    monkeypatch.setattr(thaison, "solve_captcha", lambda *a, **k: pytest.fail("không được giải ảnh lỗi"))
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_MM)
    assert ei.value.kind == "captcha"


@pytest.mark.parametrize("inv,resp", [(INV_MM, MM_NOT_FOUND), (INV_GEN, GEN_NOT_FOUND)])
def test_not_found_on_known_portal_is_no_pdf(monkeypatch, inv, resp):
    _setup(monkeypatch, [], [resp])
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_pdf"


def test_unknown_seller_tries_generic_then_no_portal(monkeypatch):
    calls = []
    inv = {"shdon": 3906, "nbmst": "0109999999", "tt_khac": {"Mã TC": "GHS5Y04VS6QG", "Loại hiển thị": "HD"}}
    _setup(monkeypatch, calls, [GEN_NOT_FOUND])
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_portal"
    assert [u for m, u, _ in calls if m == "POST"] == [f"{GEN}/tra-cuu"]


def test_seller_map_without_dc_tc(monkeypatch):
    calls = []
    inv = dict(INV_MM, lookup_url=None, tt_khac={"Mã TC": "VSOJKEFHUPMQSAHR"})
    _setup(monkeypatch, calls, [MM_OK])
    assert thaison.ThaisonAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert [u for m, u, _ in calls if m == "POST"] == [f"{MM}/xem-hoa-don"]


def test_unsupported_or_unknown_portal_host(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [])
    for inv in ({"shdon": 1, "nbmst": "0318836678", "lookup_url": "https://hoadon.example.vn",
                 "tt_khac": {"ĐCTC": "https://hoadon.example.vn", "Mã TC": "3LCZR3CZR"}},
                # Grab: HĐ Thái Sơn cũ không tra được ở đâu → no_portal ngay, không gọi mạng
                {"shdon": 52668, "nbmst": "0312650437", "tt_khac": {"Mã TC": "N7N5IZDAE", "TTMR": ""}}):
        with pytest.raises(PdfFetchError) as ei:
            thaison.ThaisonAdapter().fetch_pdf(inv)
        assert ei.value.kind == "no_portal"
    assert calls == []


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf({"shdon": 1, "tt_khac": {"DC TC": "https://einvoice.vn/tra-cuu"}})
    assert ei.value.kind == "no_code"


def test_zip_without_pdf_is_bad_content(monkeypatch):
    _setup(monkeypatch, [], [GEN_OK], file=_Resp(content=_zip({"a/a.xml": b"<x/>"})))
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_GEN)
    assert ei.value.kind == "bad_content"


def test_wrong_invoice_pdf_rejected(monkeypatch):
    file = _Resp(content=_zip({"x.pdf": _pdf_with("So 999999")}))
    _setup(monkeypatch, [], [MM_OK], file=file)
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_MM)
    assert ei.value.kind == "bad_content"


def test_invoice_from_link():
    a = thaison.ThaisonAdapter()
    inv = a.invoice_from_link("http://einvoice.mmvietnam.com", None,
                              "Hóa đơn điện tử\nMã nhận hóa đơn: VSOJKEFHUPMQSAHR")
    assert inv["ma_tra_cuu"] == "VSOJKEFHUPMQSAHR" and inv["nbmst"] == "0302249586"
    assert inv["tt_khac"]["DC TC"] == MM
    inv = a.invoice_from_link("https://einvoice.vn/tra-cuu", "69AEIZEURC3", "")
    assert inv["tt_khac"]["Mã TC"] == "69AEIZEURC3" and "nbmst" not in inv
    # tài liệu / không có mã / cổng khác → None
    assert a.invoice_from_link("http://einvoice.vn/Content/Clients/Thong_tu.pdf", None, "tiêu đề") is None
    assert a.invoice_from_link("https://www.meinvoice.vn/tra-cuu", "ABCDEFGH", "") is None


# --- cổng riêng bản mới: /Captcha (KFC, Zalo), CaptchaMvc + token (Cổng Vàng), captcha JS (CellphoneS) --

KFC = "https://invoice.kfcvietnam.com.vn"
ZALO = "https://invoice.zaloplatforms.vn"
GGG = "https://vat.ggg.com.vn"
CPS = "https://hddt.cellphones.com.vn"
TOK = "tok-form_123"
TOK_DL = "tok-dl_456"
ID_ENC = "1ACIz919MxQ="

FORM_IMG = _Resp('<form action="/xem-hoa-don" id="formgr" method="post">'
                 f'<input name="__RequestVerificationToken" type="hidden" value="{TOK}" />'
                 '<img id="capcha-img" src="/Captcha" /><input id="capcha" name="captchaCode" /></form>')
FORM_JS = _Resp(f'<input name="__RequestVerificationToken" type="hidden" value="{TOK}" />'
                '<div class="CAPTCHA form-control" id="CAPTCHA">Grrv2N</div>'
                '<input class="form-control captcha" maxlength="5" name="MA_XAC_THUC" required>')
FORM_MVC_TOK = _Resp(f'<input name="__RequestVerificationToken" type="hidden" value="{TOK}" />'
                     f'<img id="CaptchaImage" src="/DefaultCaptcha/Generate?t={T}"/>'
                     f'<input id="CaptchaDeText" name="CaptchaDeText" type="hidden" value="{T}" />')
NEW_OK = _Resp('<form action="/Search/Search" id="formgr" method="post">'
               '<input name="__RequestVerificationToken" type="hidden" value="tok-search" />'
               f'<input type="hidden" name="id" id="idHoaDonEncrypt" value="{ID_ENC}" /></form>'
               '<form action="/Download/DownloadFile" id="download" method="post">'
               f'<input name="__RequestVerificationToken" type="hidden" value="{TOK_DL}" />'
               f'    <input type="hidden" name="idHoaDon" value="{ID_ENC}" />'
               '<input type="submit" id="btnSubmitDownload" hidden /></form>', url="/xem-hoa-don")
NEW_WRONG = _Resp('<div id="listresult"><strong>Error:</strong> M&#227; kiểm tra kh&#244;ng đ&#250;ng!</div>')
NEW_NOT_FOUND = _Resp('<strong>Error:</strong> Không tìm thấy hóa đơn có thông tin vừa nhập!</div>')
NEW_ZIP = _Resp(content=_zip({"2901_1_K23TTS/2901_1_K23TTS_hdcd.pdf": _pdf_with("CHUYEN DOI 2901"),
                              "2901_1_K23TTS/2901_1_K23TTS.xml": b"<x/>",
                              "2901_1_K23TTS/2901_1_K23TTS.pdf": _pdf_with("BAN GOC 2901")}))

INV_KFC = {"shdon": 2901, "khhdon": "K23TTS", "khmshdon": 1, "nbmst": "0100773885-001",
           "lookup_url": None, "tt_khac": {"Mã TC": "K2Z4PBF184", "ChiNhanh": "", "Loại hiển thị": "HD"}}
INV_ZALO = {"shdon": 2901, "khhdon": "C26TYA", "khmshdon": 1, "nbmst": "0318836678",
            "lookup_url": "https://invoice.zaloplatforms.vn/",
            "tt_khac": {"ĐCTC": "https://invoice.zaloplatforms.vn/", "Mã TC": "3LCZR3CZR"}}
INV_GGG = {"shdon": 2901, "khhdon": "C23TCV", "khmshdon": 1, "nbmst": "0102721191-001",
           "tt_khac": {"MR": "", "Mã TC": "U0BXLFBAX"}}
INV_CPS = {"shdon": 2901, "khhdon": "C23TDP", "khmshdon": 1, "nbmst": "0316172372",
           "tt_khac": {"Mã TC": "5SBBMV7YQ"}}


def _setup_new(monkeypatch, calls, base, form, posts, answers=("AIZMo7",)):
    posts, answers = list(posts), list(answers)

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == f"{base}/":
            return form
        if url == f"{base}/Captcha":
            return _Resp(content=b"\x89PNG" + b"x" * 1500)
        if url.endswith("/DefaultCaptcha/Generate"):
            return IMG
        if method == "POST" and url == f"{base}/xem-hoa-don":
            return posts.pop(0)
        if method == "POST" and url == f"{base}/Download/DownloadFile":
            return NEW_ZIP
        raise AssertionError(f"không có route cho {method} {url}")

    def solve_img(img):
        v = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(thaison, "request", fake)
    monkeypatch.setattr(thaison, "new_session", lambda: object())
    monkeypatch.setattr(thaison, "solve_img_captcha", solve_img)
    monkeypatch.setattr(thaison, "solve_captcha", lambda img, group, min_conf=0.5: "ABCD")


def _text(pdf: bytes) -> str:
    import fitz
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return "".join(p.get_text() for p in doc)


@pytest.mark.parametrize("inv,base", [(INV_KFC, KFC), (INV_ZALO, ZALO)])
def test_img_captcha_portal_ok(monkeypatch, inv, base):
    calls = []
    _setup_new(monkeypatch, calls, base, FORM_IMG, [NEW_OK])
    pdf = thaison.ThaisonAdapter().fetch_pdf(inv)
    assert "BAN GOC" in _text(pdf)                       # bản gốc, không phải *_hdcd.pdf (chuyển đổi)
    posts = [(u, kw) for m, u, kw in calls if m == "POST"]
    assert posts[0][0] == f"{base}/xem-hoa-don"
    assert posts[0][1]["data"] == {"MA_NHAN_HOA_DON": inv["tt_khac"]["Mã TC"], "captchaCode": "AIZMo7",
                                   "__RequestVerificationToken": TOK}
    assert posts[1][0] == f"{base}/Download/DownloadFile"
    assert posts[1][1]["data"] == {"__RequestVerificationToken": TOK_DL, "idHoaDon": ID_ENC}
    assert [u for m, u, _ in calls if u.endswith("/Captcha")] == [f"{base}/Captcha"]


def test_img_captcha_wrong_then_ok(monkeypatch):
    calls = []
    _setup_new(monkeypatch, calls, KFC, FORM_IMG, [NEW_WRONG, NEW_OK], answers=("aIZmo7", "AIZMo7"))
    assert thaison.ThaisonAdapter().fetch_pdf(INV_KFC).startswith(b"%PDF")
    sent = [kw["data"]["captchaCode"] for m, u, kw in calls if m == "POST" and u.endswith("/xem-hoa-don")]
    assert sent == ["aIZmo7", "AIZMo7"]


def test_img_captcha_unreadable_not_sent_and_gives_up(monkeypatch):
    calls = []
    _setup_new(monkeypatch, calls, ZALO, FORM_IMG, [],
               answers=(thaison._WrongCaptcha("ddddocr đọc lệch số ký tự"),))
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_ZALO)
    assert ei.value.kind == "captcha"
    assert not any(m == "POST" for m, _, _ in calls)
    assert sum(u.endswith("/Captcha") for _, u, _ in calls) == thaison.IMG_CAPTCHA_TRIES


def test_img_captcha_not_found_is_no_pdf(monkeypatch):
    _setup_new(monkeypatch, [], KFC, FORM_IMG, [NEW_NOT_FOUND])
    with pytest.raises(PdfFetchError) as ei:
        thaison.ThaisonAdapter().fetch_pdf(INV_KFC)
    assert ei.value.kind == "no_pdf"


def test_js_captcha_portal_sends_any_code(monkeypatch):
    calls = []
    _setup_new(monkeypatch, calls, CPS, FORM_JS, [NEW_OK])
    monkeypatch.setattr(thaison, "solve_img_captcha", lambda img: pytest.fail("CellphoneS không có captcha ảnh"))
    assert "BAN GOC" in _text(thaison.ThaisonAdapter().fetch_pdf(INV_CPS))
    data = [kw["data"] for m, u, kw in calls if m == "POST" and u.endswith("/xem-hoa-don")][0]
    assert data["MA_NHAN_HOA_DON"] == "5SBBMV7YQ" and data["__RequestVerificationToken"] == TOK
    assert len(data["MA_XAC_THUC"]) == 5 and data["MA_XAC_THUC"].isalnum()
    assert not any("Captcha" in u for _, u, _ in calls)


def test_mvc_store_with_token_and_download_form(monkeypatch):
    calls = []
    _setup_new(monkeypatch, calls, GGG, FORM_MVC_TOK, [NEW_OK])
    assert thaison.ThaisonAdapter().fetch_pdf(INV_GGG).startswith(b"%PDF")
    data = [kw["data"] for m, u, kw in calls if m == "POST" and u.endswith("/xem-hoa-don")][0]
    assert data == {"MA_NHAN_HOA_DON": "U0BXLFBAX", "CaptchaDeText": T, "CaptchaInputText": "ABCD",
                    "__RequestVerificationToken": TOK}
    assert sum(m == "GET" and u == f"{GGG}/" for m, u, _ in calls) == 1     # form chỉ mở 1 lần
    cap = [kw for _, u, kw in calls if u.endswith("/DefaultCaptcha/Generate")][0]
    assert cap["headers"]["Referer"] == f"{GGG}/"


@pytest.mark.parametrize("mst,host", [("0100773885-001", "invoice.kfcvietnam.com.vn"),
                                      ("0102721191-001", "vat.ggg.com.vn"),
                                      ("0316172372", "hddt.cellphones.com.vn"),
                                      ("0318836678", "invoice.zaloplatforms.vn")])
def test_seller_routing_new_portals(mst, host):
    assert thaison.ThaisonAdapter.portal_for({"nbmst": mst, "tt_khac": {"Mã TC": "X"}}) == (host, False)


def test_portal_from_printed_lookup_host():
    # PDF KFC in "www.invoice.kfcvietnam.com.vn" (www. trả 404) → bỏ www.
    inv = {"nbmst": "0100773885-001", "lookup_url": "www.invoice.kfcvietnam.com.vn"}
    assert thaison.ThaisonAdapter.portal_for(inv) == ("invoice.kfcvietnam.com.vn", False)


def test_invoice_from_link_new_portal():
    inv = thaison.ThaisonAdapter().invoice_from_link("https://invoice.zaloplatforms.vn/", None,
                                                     "Hóa đơn\nMã nhận hóa đơn: 3LCZR3CZR")
    assert inv["ma_tra_cuu"] == "3LCZR3CZR" and inv["nbmst"] == "0318836678"


def test_zip_prefers_original_over_converted():
    data = _zip({"a/a_hdcd.pdf": _pdf_with("CHUYEN DOI"), "a/a.pdf": _pdf_with("BAN GOC")})
    assert "BAN GOC" in _text(thaison._pdf_from_zip(data, "x"))
    only_conv = _zip({"a/a_hdcd.pdf": _pdf_with("CHUYEN DOI")})
    assert "CHUYEN DOI" in _text(thaison._pdf_from_zip(only_conv, "x"))


# --- giải captcha /Captcha: ảnh thật của cổng, ddddocr giả lập (đọc sai hoa/thường như thật) ------

KFC_08_PNG = base64.b64decode(
    "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"
    "YII="
)
ZALO_00_PNG = base64.b64decode(
    "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"
)

@pytest.mark.parametrize("png,ocr,expected", [
    (KFC_08_PNG, "RoEoqp", "RoEOqP"),     # O cao → hoa; q thò xuống → thường; P không thò → hoa
    (KFC_08_PNG, "roeoqp", "RoEOqP"),
    (ZALO_00_PNG, "g4ivkg", "G4ivKg"),    # i có chấm; K rộng → hoa; g thò xuống → thường
])
def test_solve_img_captcha_fixes_case(monkeypatch, png, ocr, expected):
    pytest.importorskip("cv2")
    monkeypatch.setattr(thaison, "ocr_text", lambda img: ocr)
    assert thaison.solve_img_captcha(png) == expected


def test_solve_img_captcha_rejects_length_mismatch(monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setattr(thaison, "ocr_text", lambda img: "Roeqp")         # đọc sót 1 ký tự
    with pytest.raises(thaison._WrongCaptcha):
        thaison.solve_img_captcha(KFC_08_PNG)
    with pytest.raises(thaison._WrongCaptcha):
        thaison.solve_img_captcha(b"not an image")


@pytest.mark.parametrize("c,glyph,expected", [
    ("w", {"y": 18, "h": 28, "w": 38}, "W"), ("W", {"y": 25, "h": 21, "w": 29}, "w"),
    ("Y", {"y": 25, "h": 29, "w": 27}, "y"), ("p", {"y": 18, "h": 28, "w": 28}, "P"),
    ("k", {"y": 18, "h": 28, "w": 32}, "K"), ("K", {"y": 18, "h": 28, "w": 23}, "k"),
    ("T", {"y": 20, "h": 26, "w": 13}, "t"), ("f", {"y": 18, "h": 28, "w": 28}, "F"),
    ("B", {"y": 18, "h": 28, "w": 22}, "b"), ("d", {"y": 17, "h": 29, "w": 29}, "D"),
    ("O", {"y": 18, "h": 28, "w": 21}, "0"), ("0", {"y": 25, "h": 22, "w": 21}, "o"),
    ("i", {"y": 18, "h": 28, "w": 13}, "l"), ("I", {"y": 25, "h": 21, "w": 11, "dot": True}, "i"),
    ("l", {"y": 18, "h": 29, "w": 18}, "L"), ("I", {"y": 18, "h": 28, "w": 13}, "I"),
    ("7", {"y": 18, "h": 28, "w": 22}, "7"),
])
def test_fix_case_rules(c, glyph, expected):
    assert thaison._fix_case(c, glyph) == expected


def test_registered_and_routed():
    import pdf_providers
    adapter = pdf_providers.get_adapter("thaison")
    if adapter is None:                                   # coordinator chưa thêm import trong __init__
        pytest.skip("thaison chưa đăng ký trong pdf_providers/__init__.py")
    assert pdf_providers.adapter_for_mst_tcgp("0101300842") is adapter
    assert pdf_providers.adapter_for_host("einvoice.mmvietnam.com") is adapter
    assert pdf_providers.adapter_for_host("invoice.zaloplatforms.vn") is adapter
