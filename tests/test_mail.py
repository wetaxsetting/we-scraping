"""Lấy hóa đơn từ mail (mail_reader + mail_ingest) — offline, store giả, không IMAP thật.

Sai ở đây thì: bỏ sót mail HĐ (forward mất người gửi), lưu nội dung mail cá nhân,
ghi đè XML chuẩn của Thuế bằng XML từ mail, ghép PDF nhầm HĐ, hoặc mở link lạ trong mail.

Run::

    pytest tests/test_mail.py -q
"""

from __future__ import annotations

import io
import sys
import zipfile
from collections import Counter
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mail_ingest  # noqa: E402
import mail_reader  # noqa: E402

OUR = "1201496252"


def _xml(nbmst="0100112233", khhdon="C26TBV", shdon=238805, nmmst=OUR) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<HDon><DLHDon>
  <TTChung><KHMSHDon>1</KHMSHDon><KHHDon>{khhdon}</KHHDon><SHDon>{shdon}</SHDon><NLap>2026-10-10</NLap>
    <MSTTCGP>0102331762</MSTTCGP></TTChung>
  <NDHDon>
    <NBan><Ten>NCC A</Ten><MST>{nbmst}</MST></NBan>
    <NMua><Ten>Cty B</Ten><MST>{nmmst}</MST></NMua>
    <DSHHDVu><HHDVu><STT>1</STT><THHDVu>Ve</THHDVu><ThTien>100</ThTien></HHDVu></DSHHDVu>
  </NDHDon>
</DLHDon></HDon>""".encode()


def _pdf(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((50, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _mail(subject="Hoa don dien tu", body="Kinh gui quy khach", attachments=(), html=None,
          mid="<m1@x>") -> bytes:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Message-ID"] = subject, "a@ncc.vn", "the.lai@webcashvietnam.com", mid
    m["Date"] = "Wed, 14 Oct 2026 09:00:00 +0700"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for name, data in attachments:
        maintype, subtype = ("application", name.rsplit(".", 1)[-1])
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


# ---------------------------------------------------------------------------
# mail_reader
# ---------------------------------------------------------------------------

def test_invoice_mail_detection_by_content_not_sender():
    assert mail_reader.is_invoice_mail("FW: Thông báo phát hành hóa đơn", "", [])
    assert mail_reader.is_invoice_mail("Fwd: abc", "", ["hd.xml"])
    assert mail_reader.is_invoice_mail("Fwd: abc", "Mã tra cứu: AB12CD34", [])
    assert mail_reader.is_invoice_mail("Fwd", "Gửi quý khách hóa đơn tháng 9", ["x.pdf"])
    assert not mail_reader.is_invoice_mail("Họp giao ban", "Lịch họp thứ 2", ["agenda.pdf"])
    assert not mail_reader.is_invoice_mail("Ảnh team building", "", ["a.jpg"])


def test_parse_message_extracts_xml_pdf_and_zip_contents():
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inv/hd.xml", _xml(shdon=5))
        zf.writestr("inv/hd.pdf", b"%PDF-1.4 zip")
        zf.writestr("readme.txt", b"x")
    raw = _mail(attachments=[("a.xml", _xml()), ("a.pdf", b"%PDF-1.4 a"), ("goi.zip", z.getvalue()),
                             ("logo.jpg", b"\xff\xd8")])
    pm = mail_reader.parse_message(raw)
    kinds = sorted((f.kind, f.filename, f.container) for f in pm.files)
    assert kinds == [("pdf", "a.pdf", ""), ("pdf", "hd.pdf", "goi.zip"),
                     ("xml", "a.xml", ""), ("xml", "hd.xml", "goi.zip")]
    assert pm.message_id == "<m1@x>" and pm.is_invoice


def _zip_with_locked_entry() -> bytes:
    """ZIP có 1 file đặt cờ mã hóa (như zip đặt mật khẩu) + 1 file XML bình thường."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("khoa.pdf", b"%PDF-1.4 secret")          # file đầu: local header ở offset 0
        zf.writestr("hd.xml", _xml(shdon=7))
    data = bytearray(buf.getvalue())
    data[6] |= 0x01                                            # cờ "encrypted" trong local header
    data[data.find(b"PK\x01\x02") + 8] |= 0x01                 # ... và trong central directory
    return bytes(data)


def test_zip_with_unreadable_entry_keeps_the_rest():
    """ZIP có mật khẩu / hỏng: chỉ bỏ file đó, không làm cả mail lỗi (mất cả file đính kèm khác)."""
    raw = _mail(attachments=[("goi.zip", _zip_with_locked_entry()), ("a.pdf", b"%PDF-1.4 a")])
    pm = mail_reader.parse_message(raw)
    assert sorted((f.kind, f.filename) for f in pm.files) == [("pdf", "a.pdf"), ("xml", "hd.xml")]


@pytest.mark.parametrize("how", ["rfc822", "eml_file"])
def test_forwarded_mail_as_attachment_is_walked(how):
    inner = _mail(attachments=[("goc.xml", _xml())], mid="<inner@x>",
                  html='<a href="https://tracuu.ehoadon.vn/Lookup?InvoiceGUID=abcdef12">x</a>')
    outer = EmailMessage()
    outer["Subject"], outer["Message-ID"] = "FW: hóa đơn", "<outer@x>"
    outer.set_content("xem mail dưới")
    if how == "rfc822":
        import email as _email
        outer.add_attachment(_email.message_from_bytes(inner))        # message/rfc822 chuẩn
    else:
        outer.add_attachment(inner, maintype="application", subtype="octet-stream", filename="goc.eml")
    pm = mail_reader.parse_message(outer.as_bytes())
    assert [f.filename for f in pm.files] == ["goc.xml"] and pm.message_id == "<outer@x>"
    assert [l.host for l in pm.links] == ["tracuu.ehoadon.vn"]


def test_links_and_codes_extraction_skips_tracking_and_images():
    html = """<p>Mã tra cứu: <b>7YZCACMAG*</b></p>
      <a href="https://tracuu.ehoadon.vn/Lookup?InvoiceGUID=70611f78-501c-4fce-a2dc-b31dd272f873">Xem</a>
      <a href="https://wetax.com.vn/tra-cuu?lookUpCode=AbC123xyz">wetax</a>
      <img src="https://live.staticflickr.com/a.png"><a href="https://x.awstrack.me/L0/abc">t</a>
      <a href="https://www.webcashvietnam.com/">cty</a>"""
    pm = mail_reader.parse_message(_mail(body="xem html", html=html))
    hosts = {l.host: l.code for l in pm.links}
    assert hosts == {"tracuu.ehoadon.vn": "70611f78-501c-4fce-a2dc-b31dd272f873",
                     "wetax.com.vn": "AbC123xyz"}
    assert "7YZCACMAG*" in pm.codes
    assert mail_reader.extract_codes("Mã tra cứu hóa đơn: HCM10013369202609") == ["HCM10013369202609"]  # VNPT


# ---------------------------------------------------------------------------
# mail_ingest với store giả
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self, invoices=()):
        self.invoices = {i["id"]: dict(i) for i in invoices}
        self.emails, self.files, self.links = {}, [], []
        self.xml_saved, self.pdf_saved = [], []

    def managed_msts(self):
        return {OUR}

    def known_emails(self, mailbox, ids):
        return {m for (mb, m) in self.emails if mb == mailbox} & set(ids)

    def record_email(self, mailbox, mid, is_invoice, status, stats=None, subject=None,
                     from_addr=None, received_at=None):
        prev = self.emails.get((mailbox, mid), {})
        self.emails[(mailbox, mid)] = {"id": prev.get("id", len(self.emails) + 1), "status": status,
                                       "is_invoice": is_invoice,
                                       "subject": subject if is_invoice else None,
                                       "from": from_addr if is_invoice else None}
        if prev.get("subject"):
            self.emails[(mailbox, mid)]["subject"] = prev["subject"]
        return self.emails[(mailbox, mid)]["id"]

    def find_invoices(self, nbmst, khmshdon, khhdon, shdon):
        return [i for i in self.invoices.values()
                if (i["nbmst"], i["khmshdon"], i["khhdon"], i["shdon"]) == (nbmst, int(khmshdon), khhdon, int(shdon))]

    def find_invoices_in_text(self, msts, text):
        return [i for i in self.invoices.values()
                if i["nbmst"] in msts and i["khhdon"] in text and str(i["shdon"]) in text]

    def add_email_file(self, email_id, f):
        row = {"id": len(self.files) + 1, "email_id": email_id, "invoice_id": None, "nmmst": None,
               "nbmst": None, "khmshdon": None, "khhdon": None, "shdon": None, **f}
        self.files.append(row)
        return row["id"]

    def set_email_file(self, file_id, status, invoice_id=None, note=None):
        row = self.files[file_id - 1]
        row["status"] = status
        row["invoice_id"] = invoice_id or row["invoice_id"]

    def unmatched_email_files(self):
        return [f for f in self.files if f["status"] == "unmatched"]

    def add_email_links(self, email_id, links, invoice_id=None):
        self.links += [dict(l, invoice_id=invoice_id, id=len(self.links) + i + 1, email_id=email_id)
                       for i, l in enumerate(links)]
        return len(links)

    def apply_email_xml(self, invoice_id, run_id, path, sha, parsed, partner):
        inv = self.invoices[invoice_id]
        if inv["has_xml"]:
            return False
        inv["has_xml"] = True
        self.xml_saved.append((invoice_id, path))
        return True

    def apply_email_pdf(self, invoice_id, run_id, path, sha, source_id="email"):
        inv = self.invoices[invoice_id]
        if inv["has_pdf"]:
            return False
        inv["has_pdf"] = True
        self.pdf_saved.append((invoice_id, path))
        self.pdf_sources = getattr(self, "pdf_sources", []) + [source_id]
        return True

    # --- email_links ---
    def pending_email_links(self, max_attempts=3):
        return [dict(l, subject="Thông báo hóa đơn", mail_codes=[]) for l in self.links
                if l.get("status", "new") == "new" and l.get("attempts", 0) < max_attempts]

    def set_email_link(self, link_id, status, invoice_id=None, error=None, failed=False):
        l = next(x for x in self.links if x["id"] == link_id)
        l.update(status=status, invoice_id=invoice_id or l.get("invoice_id"), last_error=error,
                 attempts=l.get("attempts", 0) + (1 if failed else 0))

    def invoice_by_id(self, invoice_id):
        return self.invoices.get(invoice_id)


def _inv(id_=1, shdon=238805, has_xml=False, has_pdf=False, nbmst="0100112233", khhdon="C26TBV"):
    return {"id": id_, "company_mst": OUR, "nbmst": nbmst, "khmshdon": 1, "khhdon": khhdon,
            "shdon": shdon, "ngay_lap": date(2026, 10, 10), "has_xml": has_xml, "has_pdf": has_pdf}


def _process(store, tmp_path, raw):
    stats = Counter()
    status = mail_ingest.MailIngest(store, tmp_path).process("box", mail_reader.parse_message(raw), stats)
    return status, stats


REL = "1201496252/2026/10/0100112233/1_C26TBV_238805"


def test_xml_from_mail_fills_invoice_without_xml_and_pairs_pdf(tmp_path):
    store = _Store([_inv()])
    pdf = _pdf("HOA DON C26TBV So 238805 MST 0100112233")
    status, stats = _process(store, tmp_path, _mail(attachments=[("hd.xml", _xml()), ("hd.pdf", pdf)]))
    assert status == "done"
    assert store.xml_saved == [(1, f"xml/{REL}.xml")] and store.pdf_saved == [(1, f"pdf/{REL}.pdf")]
    assert (tmp_path / "xml" / f"{REL}.xml").read_bytes() == _xml()
    assert (tmp_path / "pdf" / f"{REL}.pdf").read_bytes() == pdf


def test_gdt_xml_is_not_overwritten(tmp_path):
    store = _Store([_inv(has_xml=True)])
    status, stats = _process(store, tmp_path, _mail(attachments=[("hd.xml", _xml())]))
    assert stats["xml_duplicate"] == 1 and not store.xml_saved
    assert not (tmp_path / "xml").exists()
    assert status == "done"


def test_unmatched_xml_waits_then_rematches_when_invoice_appears(tmp_path):
    store = _Store([])
    status, stats = _process(store, tmp_path, _mail(attachments=[("hd.xml", _xml())]))
    assert status == "unmatched" and stats["xml_unmatched"] == 1
    tmp_files = list((tmp_path / "mail" / "unmatched").iterdir())
    assert len(tmp_files) == 1

    store.invoices[1] = _inv()                      # lượt sau: GDT đã liệt kê HĐ
    stats = Counter()
    mail_ingest.MailIngest(store, tmp_path).rematch(stats)
    assert stats["rematched"] == 1 and store.xml_saved == [(1, f"xml/{REL}.xml")]
    assert store.files[0]["status"] == "matched" and not tmp_files[0].exists()


def test_xml_of_other_company_is_ignored(tmp_path):
    store = _Store([])
    status, stats = _process(store, tmp_path, _mail(attachments=[("hd.xml", _xml(nmmst="0309999999"))]))
    assert stats["xml_not_ours"] == 1 and not (tmp_path / "mail").exists()


def test_lone_pdf_matched_by_text(tmp_path):
    store = _Store([_inv(id_=7, shdon=5983, nbmst="0317770671", khhdon="C26MMC")])
    pdf = _pdf("Ky hieu C26MMC So 5983 MST nguoi ban 0317770671")
    status, stats = _process(store, tmp_path, _mail(attachments=[("hoadon.pdf", pdf)]))
    assert status == "done" and store.pdf_saved and store.pdf_saved[0][0] == 7


def test_lone_pdf_ambiguous_stays_unmatched(tmp_path):
    store = _Store([_inv(id_=1, shdon=59), _inv(id_=2, shdon=598)])
    pdf = _pdf("C26TBV So 59 va 598 MST 0100112233")
    status, stats = _process(store, tmp_path, _mail(attachments=[("x.pdf", pdf)]))
    assert stats["pdf_unmatched"] == 1 and not store.pdf_saved


@pytest.mark.parametrize("phrase", ["Thay the cho hoa don", "Dieu chinh giam cho hoa don"])
def test_replacement_invoice_pdf_is_not_confused_with_the_original(tmp_path, phrase):
    """PDF HĐ thay thế/điều chỉnh in cả số HĐ gốc (MISA 0302159861 K26TAF-153 thay 148, 10/2026)."""
    store = _Store([_inv(id_=1, shdon=148, nbmst="0302159861", khhdon="K26TAF"),
                    _inv(id_=2, shdon=153, nbmst="0302159861", khhdon="K26TAF")])
    pdf = _pdf(f"HOA DON BAN HANG\nKy hieu: 2K26TAF So: 00000153\n{phrase} Mau so 2, ky hieu K26TAF,\n"
               "so 00000148, ngay 15 thang 09 nam 2026\nMST 0302159861")      # câu nhắc HĐ gốc bị xuống dòng
    status, stats = _process(store, tmp_path, _mail(attachments=[("hd.pdf", pdf)]))
    assert status == "done" and [i for i, _ in store.pdf_saved] == [2]


def test_non_invoice_mail_stores_nothing_but_id(tmp_path):
    store = _Store([])
    status, _ = _process(store, tmp_path, _mail(subject="Lịch họp", body="họp lúc 9h"))
    assert status == "ignored"
    rec = store.emails[("box", "<m1@x>")]
    assert rec["subject"] is None and rec["from"] is None and not store.files and not store.links


def test_links_only_mail_records_links_with_code(tmp_path):
    store = _Store([])
    html = '<p>Mã tra cứu: QX7PL2MN9</p><a href="https://tracuu.vin-hoadon.com/tra-cuu">Tra cứu</a>'
    status, _ = _process(store, tmp_path, _mail(subject="Thông báo hóa đơn", html=html))
    assert status == "links_only"
    assert [{k: l[k] for k in ("url", "host", "code", "invoice_id")} for l in store.links] == [
        {"url": "https://tracuu.vin-hoadon.com/tra-cuu", "host": "tracuu.vin-hoadon.com",
         "code": "QX7PL2MN9", "invoice_id": None}]


# ---------------------------------------------------------------------------
# Mail chỉ có link → adapter của cổng (process_links)
# ---------------------------------------------------------------------------

import pdf_providers  # noqa: E402


class _LinkAdapter(pdf_providers.PdfAdapter):
    source_id, hosts = "fake_link", ("tracuu.fake-hd.vn",)

    def __init__(self, pdf=b"", fail_kind=None, xml=None):
        self.pdf, self.fail_kind, self.xml, self.calls = pdf, fail_kind, xml, []

    def invoice_from_link(self, url, code, mail_text):
        p = pdf_providers.common.link_params(url)
        return {"nbmst": p["mst"], "ma_tra_cuu": p["sec"]} if "sec" in p else None

    def fetch_pdf(self, invoice):
        self.calls.append(invoice)
        if self.fail_kind:
            raise pdf_providers.PdfFetchError("lỗi", self.fail_kind)
        return self.pdf


@pytest.fixture
def link_adapter(monkeypatch):
    def make(**kw):
        a = _LinkAdapter(**kw)
        if kw.get("xml"):
            a.fetch_xml = lambda inv: kw["xml"]
        monkeypatch.setitem(pdf_providers._REGISTRY, a.source_id, a)
        return a
    return make


def _link_store(*urls, invoices=()):
    store = _Store(invoices)
    store.add_email_links(1, [{"url": u, "host": mail_reader.urlsplit(u).hostname, "code": None} for u in urls])
    return store


def test_links_fetch_pdf_through_adapter_and_match_invoice(tmp_path, link_adapter):
    a = link_adapter(pdf=_pdf("HOA DON C26TBV So 238805 MST 0100112233"))
    store = _link_store("https://tracuu.fake-hd.vn/?mst=0100112233&sec=ABC123",
                        "https://tracuu.fake-hd.vn/xem?mst=0100112233&sec=ABC123",     # link thứ 2 cùng mail
                        "https://tracuu.fake-hd.vn/huy-nhan-mail?email=x",            # link hủy nhận mail
                        "https://cong-chua-co-adapter.vn/?sec=1", invoices=[_inv()])
    stats = Counter()
    mail_ingest.MailIngest(store, tmp_path).process_links(stats)
    assert a.calls == [{"nbmst": "0100112233", "ma_tra_cuu": "ABC123"}]          # gọi 1 lần, không mở URL
    assert store.pdf_saved == [(1, f"pdf/{REL}.pdf")] and store.pdf_sources == ["email:fake_link"]
    assert [l.get("status", "new") for l in store.links] == ["done", "done", "ignored", "new"]
    assert store.links[0]["invoice_id"] == 1


def test_link_with_xml_fills_summary_only_invoice(tmp_path, link_adapter):
    link_adapter(pdf=_pdf("C26TBV So 238805 MST 0100112233"), xml=_xml())
    store = _link_store("https://tracuu.fake-hd.vn/?mst=0100112233&sec=K1", invoices=[_inv()])
    mail_ingest.MailIngest(store, tmp_path).process_links(Counter())
    assert store.xml_saved == [(1, f"xml/{REL}.xml")] and store.pdf_saved == [(1, f"pdf/{REL}.pdf")]


@pytest.mark.parametrize("kind,status", [("no_pdf", "new"), ("network", "new"), ("no_code", "failed")])
def test_link_failures_retry_or_stop(tmp_path, link_adapter, kind, status):
    link_adapter(fail_kind=kind)
    store = _link_store("https://tracuu.fake-hd.vn/?mst=0100112233&sec=K1")
    stats = Counter()
    mail_ingest.MailIngest(store, tmp_path).process_links(stats)
    assert store.links[0]["status"] == status and store.links[0]["attempts"] == 1
    assert stats["link_failed"] == 1


def test_link_skipped_when_invoice_already_has_pdf(tmp_path, link_adapter):
    a = link_adapter(pdf=b"%PDF-x")
    store = _link_store("https://tracuu.fake-hd.vn/?mst=0100112233&sec=K1", invoices=[_inv(has_pdf=True)])
    store.links[0]["invoice_id"] = 1
    mail_ingest.MailIngest(store, tmp_path).process_links(Counter())
    assert a.calls == [] and store.links[0]["status"] == "done"


@pytest.mark.parametrize("url,expected", [
    ("https://www.meinvoice.vn/tra-cuu/?sc=_PF4TPQ2RKRG&m=x@y.vn", {"TransactionID": "_PF4TPQ2RKRG"}),
    ("https://api.meinvoice.vn/api/v3/WebHook/EmailTrackingHandler.ashx?TransactionID=Q_FGTPQ_6M0E",
     {"TransactionID": "Q_FGTPQ_6M0E"}),
    ("https://www.meinvoice.vn/tra-cuu", None),
])
def test_misa_invoice_from_link(url, expected):
    inv = pdf_providers.get_adapter("misa_meinvoice").invoice_from_link(url, None, "")
    assert (inv and inv["tt_khac"]) == expected


def test_visnam_viettel_easyinvoice_invoice_from_link():
    vis = pdf_providers.get_adapter("visnam")
    assert vis.invoice_from_link("https://tracuu.vin-hoadon.com/File/TaiPdfLinkTraCuu?mstdoanhnghiep=0304892753"
                                 "&masobimat=29W2XF3XMCY0", None, "")["tt_khac"] == {"QuanLy_SoBaoMat": "29W2XF3XMCY0"}
    assert vis.invoice_from_link("https://tracuu.vin-hoadon.com//tuchoinhanemail?diachi=a@b.vn", None, "") is None
    vt = pdf_providers.get_adapter("viettel").invoice_from_link(
        "https://vinvoice.viettel.vn/tracuu?tax-code=0313592114&secret-code=ABCDEF123", None, "")
    assert vt["nbmst"] == "0313592114" and vt["tt_khac"] == {"Mã số bí mật": "ABCDEF123"}
    import base64
    tok = base64.b64encode(b"1C26TPS__4538|X2KI57TA8").decode().rstrip("=")
    ez = pdf_providers.get_adapter("easyinvoice").invoice_from_link(
        f"http://0104947250001hd.easyinvoice.com.vn/Invoice/DownloadInvPdf?token={tok}", None, "")
    assert ez == {"nbmst": "0104947250-001", "khmshdon": 1, "khhdon": "C26TPS", "shdon": 4538,
                  "ma_tra_cuu": "X2KI57TA8", "tt_khac": {"Fkey": "X2KI57TA8"},
                  "lookup_url": "http://0104947250001hd.easyinvoice.com.vn"}
    assert pdf_providers.get_adapter("easyinvoice").invoice_from_link("https://easyinvoice.vn", None, "") is None
    assert pdf_providers.adapter_for_host("easy-pit.easyinvoice.com.vn").source_id == "easyinvoice"
    assert pdf_providers.adapter_for_host("evil-easyinvoice.vn") is None


# ---------------------------------------------------------------------------
# Công tắc đăng nhập (INVOICE_MAIL_AUTH) + chi tiết IMAP
# ---------------------------------------------------------------------------

_MAIL_ENV = ("INVOICE_MAIL_USER", "INVOICE_MAIL_HOST", "INVOICE_MAIL_PASSWORD", "INVOICE_MAIL_AUTH",
             "INVOICE_MAIL_PORT", "INVOICE_MAIL_FOLDER", "INVOICE_MAIL_DAYS",
             "INVOICE_MAIL_MS_TENANT_ID", "INVOICE_MAIL_MS_CLIENT_ID", "INVOICE_MAIL_MS_CLIENT_SECRET",
             "INVOICE_MAIL_GOOGLE_CLIENT_ID", "INVOICE_MAIL_GOOGLE_CLIENT_SECRET",
             "INVOICE_MAIL_GOOGLE_REFRESH_TOKEN")


@pytest.fixture
def mail_env(monkeypatch):
    for k in _MAIL_ENV:
        monkeypatch.delenv(k, raising=False)

    def set_(**kv):
        for k, v in kv.items():
            monkeypatch.setenv(k, v)
    return set_


def test_mail_disabled_without_user(mail_env):
    assert mail_ingest.mail_config() is None


def test_password_mode_is_default_and_needs_host_and_password(mail_env):
    mail_env(INVOICE_MAIL_USER="a@cty.vn", INVOICE_MAIL_HOST="mail.cty.vn")
    with pytest.raises(mail_ingest.MailConfigError, match="INVOICE_MAIL_PASSWORD"):
        mail_ingest.mail_config()
    mail_env(INVOICE_MAIL_PASSWORD="pw")
    cfg = mail_ingest.mail_config()
    assert cfg["auth"] == "password" and cfg["host"] == "mail.cty.vn" and cfg["port"] == 993
    assert cfg["folders"] == ["INBOX"] and cfg["days"] == 7 and cfg["token_provider"] is None
    mail_env(INVOICE_MAIL_FOLDER=" INBOX , HDDT ,")                # bộ lọc chuyển mail sang HDDT
    assert mail_ingest.mail_config()["folders"] == ["INBOX", "HDDT"]


def test_microsoft_mode_defaults_host_and_never_leaks_secret(mail_env):
    mail_env(INVOICE_MAIL_USER="hd@cty.onmicrosoft.com", INVOICE_MAIL_AUTH="oauth2_microsoft",
             INVOICE_MAIL_MS_TENANT_ID="t", INVOICE_MAIL_MS_CLIENT_SECRET="SIEU-BI-MAT")
    with pytest.raises(mail_ingest.MailConfigError) as ei:
        mail_ingest.mail_config()
    assert "INVOICE_MAIL_MS_CLIENT_ID" in str(ei.value) and "SIEU-BI-MAT" not in str(ei.value)
    mail_env(INVOICE_MAIL_MS_CLIENT_ID="c")
    cfg = mail_ingest.mail_config()
    assert cfg["host"] == "outlook.office365.com" and callable(cfg["token_provider"])


def test_google_mode_and_invalid_mode(mail_env):
    mail_env(INVOICE_MAIL_USER="hd@cty.vn", INVOICE_MAIL_AUTH="oauth2_google",
             INVOICE_MAIL_GOOGLE_CLIENT_ID="c", INVOICE_MAIL_GOOGLE_CLIENT_SECRET="s",
             INVOICE_MAIL_GOOGLE_REFRESH_TOKEN="r")
    assert mail_ingest.mail_config()["host"] == "imap.gmail.com"
    mail_env(INVOICE_MAIL_AUTH="pop3")
    with pytest.raises(mail_ingest.MailConfigError, match="không hợp lệ"):
        mail_ingest.mail_config()


def test_token_request_posts_form_and_hides_secret_on_error(monkeypatch):
    calls = []

    class _R:
        def __init__(self, code, js):
            self.status_code, self._js = code, js

        def json(self):
            return self._js

    def fake_post(url, data, timeout):
        calls.append((url, data))
        return _R(200, {"access_token": "AT"}) if data["client_secret"] == "ok" else \
            _R(401, {"error": "invalid_client", "error_description": "bad secret"})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert mail_reader.microsoft_token("tid", "cid", "ok") == "AT"
    assert calls[0][0] == "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
    assert calls[0][1]["scope"] == "https://outlook.office365.com/.default"
    assert mail_reader.google_token("cid", "ok", "rt") == "AT"
    assert calls[1][1]["grant_type"] == "refresh_token"
    with pytest.raises(mail_reader.MailError) as ei:
        mail_reader.google_token("cid", "SAI-BI-MAT", "rt")
    assert "invalid_client" in str(ei.value) and "SAI-BI-MAT" not in str(ei.value)


def test_encode_folder_modified_utf7():
    assert mail_reader.encode_folder("INBOX") == "INBOX"
    assert mail_reader.encode_folder("Hóa đơn") == "H&APM-a &AREBoQ-n"
    assert mail_reader.encode_folder("~peter/mail/台北/日本語") == "~peter/mail/&U,BTFw-/&ZeVnLIqe-"  # RFC 3501
    assert mail_reader.encode_folder("A&B") == "A&-B"


def test_imap_date_is_locale_independent():
    assert mail_reader.imap_date(date(2026, 10, 7)) == "07-Oct-2026"


class _FakeImap:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.calls = host, []
        _FakeImap.instances.append(self)

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def authenticate(self, mech, cb):
        self.calls.append(("authenticate", mech, cb(b"")))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def logout(self):
        pass


@pytest.mark.parametrize("auth", ["password", "oauth2_microsoft"])
def test_imap_login_switch(monkeypatch, auth):
    monkeypatch.setattr(mail_reader.imaplib, "IMAP4_SSL", _FakeImap)
    _FakeImap.instances.clear()
    mb = mail_reader.ImapMailbox("h", 993, "u@x", "pw", "Hóa đơn", auth=auth,
                                 token_provider=lambda: "TOKEN")
    with mb:
        pass
    calls = _FakeImap.instances[0].calls
    if auth == "password":
        assert calls[0] == ("login", "u@x", "pw")
    else:
        assert calls[0] == ("authenticate", "XOAUTH2", b"user=u@x\x01auth=Bearer TOKEN\x01\x01")
    assert calls[1] == ("select", '"H&APM-a &AREBoQ-n"', True)        # chỉ đọc + tên thư mục đã mã hóa


class _Box:
    name = "box"

    def __init__(self, mails):
        self.mails = mails            # {uid: raw}
        self.fetched = []

    def message_ids_since(self, days):
        return [(uid, mail_reader.parse_message(raw).message_id) for uid, raw in self.mails.items()]

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.mails[uid]


def test_run_skips_already_seen_mails(tmp_path):
    store = _Store([_inv()])
    box = _Box({"1": _mail(attachments=[("hd.xml", _xml())], mid="<a@x>"),
                "2": _mail(subject="Lịch họp", body="x", mid="<b@x>")})
    stats = Counter()
    mail_ingest.run(store, box, tmp_path, None, 7, stats)
    assert stats["mail_new"] == 2 and stats["xml_saved"] == 1 and stats["mail_ignored"] == 1
    box.fetched.clear()
    stats = Counter()
    mail_ingest.run(store, box, tmp_path, None, 7, stats)
    assert stats["mail_new"] == 0 and box.fetched == []
