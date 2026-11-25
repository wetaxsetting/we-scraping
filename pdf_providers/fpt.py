"""
fpt_v3.py
=====================
Bắt JSON từ /api/search, trích xuất PDF, lưu + rename.

Cách dùng:
    python fpt.py --mst 2300680991 --sec goxyk0k9wg `
        --filename "HD_2300680991" --out-dir "C:\\HoaDon"
"""

import argparse
import asyncio
import base64
import json
import random
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ================== CẤU HÌNH ==================
USER_DATA_DIR = Path("./fpt_profile").resolve()
DEFAULT_DOWNLOAD_DIR = Path("./downloads").resolve()
DEBUG_DIR = Path("./debug").resolve()

BASE_URL = "https://tracuuhoadon.fpt.com.vn/"
LOGIN_WAIT_SEC = 180
API_WAIT_SEC = 30
# =============================================


async def human_delay(a=0.5, b=1.2):
    await asyncio.sleep(random.uniform(a, b))


async def wait_for_manual_login(page: Page):
    print(f"\n⚠️  Đăng nhập tay nếu cần. Chờ tối đa {LOGIN_WAIT_SEC}s...\n")
    try:
        await page.wait_for_selector(
            'input[placeholder="MST bên bán"]',
            timeout=LOGIN_WAIT_SEC * 1000,
        )
        print("✅ Đã vào trang tra cứu.")
        return True
    except PWTimeout:
        print("⚠️  Không thấy form tra cứu, tiếp tục.")
        return False


# ---------- TRÍCH XUẤT PDF TỪ JSON ----------
def find_pdf_in_json(obj, path=""):
    """
    Đệ quy tìm mọi khả năng chứa PDF trong JSON.
    Trả về list [(path, kind, value)] với kind ∈ {'url', 'base64', 'data_url'}.
    """
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(find_pdf_in_json(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            results.extend(find_pdf_in_json(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        s = obj.strip()
        if not s:
            return results
        low = s.lower()
        # 1) URL .pdf
        if low.endswith(".pdf") or ".pdf?" in low:
            results.append((path, "url", s))
        # 2) data URL
        elif low.startswith("data:application/pdf"):
            if "," in s:
                b64 = s.split(",", 1)[1]
                results.append((path, "base64", b64))
        # 3) base64 PDF (%PDF = JVBERi0)
        elif s.startswith("JVBERi0"):
            results.append((path, "base64", s))
        # 4) URL có "pdf" trong path và bắt đầu bằng http
        elif low.startswith(("http://", "https://")) and "pdf" in low and "viewer" not in low:
            # thận trọng: chỉ nhận nếu path kết thúc bằng pdf hoặc chứa /pdf/
            results.append((path, "url", s))
    return results


def extract_pdf_bytes_from_json(data: dict) -> tuple[bytes | None, str]:
    """Trả về (pdf_bytes, source_info)."""
    hits = find_pdf_in_json(data)
    if not hits:
        return None, "no_match"
    for path, kind, value in hits:
        if kind == "base64":
            try:
                return base64.b64decode(value), f"base64@{path}"
            except Exception as e:
                print(f"   ⚠️ base64 decode lỗi tại {path}: {e}")
        elif kind == "url":
            # trả URL cho caller tải
            return None, f"url:{value}"
    return None, "no_valid"


# ---------- LUỒNG CHÍNH ----------
async def run(args):
    out_dir = Path(args.out_dir).resolve() if args.out_dir else DEFAULT_DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)

    # Biến ghi lại kết quả từ handler
    captured = {"json": None, "pdf_url": None, "pdf_bytes": None, "http_pdf": None}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            accept_downloads=True,
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = context.pages[0] if context.pages else await context.new_page()

        # === ĐĂNG KÝ HANDLER CHO /api/search VÀ MỌI RESPONSE PDF ===
        async def on_response(response):
            url = response.url
            ct = (response.headers.get("content-type", "") or "").lower()
            try:
                # A) Bắt JSON từ /api/search
                if "/api/search" in url and response.status == 200:
                    try:
                        data = await response.json()
                    except Exception:
                        text = await response.text()
                        try:
                            data = json.loads(text)
                        except Exception:
                            print(f"   ⚠️ /api/search trả về không phải JSON, bỏ qua.")
                            return
                    captured["json"] = data
                    print(f"\n📥 Bắt được JSON /api/search ({len(json.dumps(data))} chars)")
                    # dump ra file debug
                    dbg = DEBUG_DIR / "api_response.json"
                    dbg.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"   ↳ Lưu: {dbg}")
                    # tìm PDF
                    hits = find_pdf_in_json(data)
                    if hits:
                        print(f"   🔍 Tìm thấy {len(hits)} ứng viên PDF trong JSON:")
                        for hp, kind, val in hits[:5]:
                            preview = val[:80] + ("..." if len(val) > 80 else "")
                            print(f"     - {kind}@{hp}: {preview}")
                    else:
                        print("   ℹ️  Không thấy PDF trong JSON, cần inspect cấu trúc.")
                    return

                # B) Bắt response PDF thực tế (nếu trình duyệt tải riêng)
                if "application/pdf" in ct or url.lower().endswith(".pdf"):
                    print(f"\n📥 Bắt được response PDF: {url[:120]}")
                    body = await response.body()
                    captured["http_pdf"] = body
                    print(f"   📦 {len(body)} bytes")
                    return
            except Exception as e:
                print(f"   ⚠️ Handler lỗi: {e}")

        page.on("response", on_response)

        # 1. Mở trang chủ
        print(f"→ Mở {BASE_URL}")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        await human_delay(1.5, 2.5)

        # 2. Chờ đăng nhập
        await wait_for_manual_login(page)

        # 3. Điều hướng đến URL tra cứu
        search_url = f"{BASE_URL}?mst={args.mst}&sec={args.sec}"
        print(f"→ Điều hướng: {search_url}")
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=60_000)
        except PWTimeout:
            pass

        # 4. Chờ handler kịp chạy
        print("→ Chờ bắt API response...")
        for _ in range(API_WAIT_SEC):
            if captured["json"] or captured["http_pdf"]:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(1)  # đợi thêm cho handler xử lý xong

        # 5. Xác định nguồn PDF
        pdf_bytes = None
        source = ""

        if captured["http_pdf"]:
            pdf_bytes = captured["http_pdf"]
            source = "direct_http_response"
        elif captured["json"]:
            data = captured["json"]
            hits = find_pdf_in_json(data)
            for path, kind, value in hits:
                if kind == "base64":
                    try:
                        pdf_bytes = base64.b64decode(value)
                        source = f"json.base64@{path}"
                        break
                    except Exception:
                        continue
                elif kind == "url":
                    # Tải URL đó bằng context.request (giữ cookie)
                    print(f"→ Tải PDF từ URL trong JSON: {value[:100]}")
                    try:
                        resp = await context.request.get(value, timeout=60_000)
                        if resp.ok:
                            body = await resp.body()
                            if body[:4] == b"%PDF":
                                pdf_bytes = body
                                source = f"json.url@{path}"
                                break
                            else:
                                print(f"   ⚠️ URL không trả về PDF thực ({len(body)} bytes)")
                    except Exception as e:
                        print(f"   ⚠️ Tải URL lỗi: {e}")

        if not pdf_bytes:
            print("\n❌ Không trích xuất được PDF.")
            if captured["json"]:
                print(f"   → Đã lưu JSON tại {DEBUG_DIR / 'api_response.json'}")
                print("   → Gửi file này để tôi phân tích cấu trúc.")
            else:
                print("   → Không bắt được response /api/search.")
                print("   → Có thể trang dùng token reCAPTCHA và cần chờ lâu hơn.")
            return

        # 6. Rename & lưu
        if args.filename:
            fname = args.filename if "." in Path(args.filename).name else f"{args.filename}.pdf"
        else:
            fname = f"HD_{args.mst}_{args.sec}.pdf"
        out_path = out_dir / fname
        if out_path.exists():
            stem, suf = out_path.stem, out_path.suffix
            i = 1
            while out_path.exists():
                out_path = out_dir / f"{stem}_{i}{suf}"
                i += 1
        out_path.write_bytes(pdf_bytes)
        print(f"\n✅ THÀNH CÔNG!")
        print(f"   Nguồn: {source}")
        print(f"   Kích thước: {len(pdf_bytes)} bytes")
        print(f"   Lưu tại: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Tự động tra cứu hóa đơn FPT")
    parser.add_argument("--mst", required=True, help="Mã số thuế")
    parser.add_argument("--sec", required=True, help="Mã tra cứu")
    parser.add_argument("--out-dir", default=None, help="Thư mục lưu (mặc định ./downloads)")
    parser.add_argument("--filename", default=None, help="Tên file (không cần .pdf)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n⏹️  Dừng.")


if __name__ == "__main__":
    sys.exit(main())