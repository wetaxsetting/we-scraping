"""Web UI: upload ảnh / paste URL / chọn source → model predict captcha."""

from __future__ import annotations
from api_client import authenticate_auto

import io
import sys
import tempfile
import uuid
from pathlib import Path

import requests as http_requests
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image

# ---------- Paths ----------
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(WEB_DIR))

from captcha.config import Config, default_config_path
from captcha.predict import Predictor
from captcha.transforms import load_image
from captcha.utils import get_device
from captcha_sources import (
    fetch_captcha,
    list_sources,
    detect_source_from_url,
)
from ocr_fallback import ocr_predict

# ---------- Config ----------
CONF_THRESHOLD = 0.8   # dưới ngưỡng này → chạy OCR fallback

app = Flask(__name__)

_predictor: Predictor | None = None
UPLOAD_DIR = Path(tempfile.gettempdir()) / "captcha_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def get_predictor() -> Predictor:
    global _predictor
    if _predictor is None:
        cfg = Config.load(default_config_path(ROOT))
        _predictor = Predictor(cfg, ROOT, backend="torch",
                               device=get_device("cpu"))
    return _predictor


def predict_with_ocr_fallback(saved_path: Path) -> dict:
    """
    Chạy model predict. Nếu confidence < CONF_THRESHOLD → chạy OCR fallback.
    Trả về dict chuẩn cho response.
    """
    predictor = get_predictor()
    pred = predictor.predict_path(saved_path)

    result = {
        "pred": pred.text,
        "confidence": round(pred.confidence, 4),
        "min_char_prob": round(pred.min_char_prob, 4),
        "group": pred.group_id,
        "fallback": pred.fallback,
        "low_confidence": pred.low_confidence,
        "error": pred.error,
        # Fields cho OCR
        "used_ocr": False,
        "ocr_text": None,
        "ocr_agree": None,
    }

    # Nếu confidence thấp → chạy OCR
    if pred.confidence < CONF_THRESHOLD and not pred.error:
        ocr_text = ocr_predict(saved_path)
        result["used_ocr"] = True
        result["ocr_text"] = ocr_text
        if ocr_text:
            result["ocr_agree"] = (ocr_text == pred.text)

    return result


from captcha_sources import list_sources_by_status   # thêm import

@app.route("/")
def index():
    return render_template(
        "index.html",
        sources=list_sources(),                  # giữ cho tab Paste URL
        sources_by_status=list_sources_by_status(),  # cho UI phân nhóm
    )

# ============================================================
# API: upload file / paste URL
# ============================================================
@app.route("/api/login", methods=["POST"])
def do_login():
    body = request.get_json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    if not all([username, password]):
        return jsonify({"ok": False, "message": "Thiếu tài khoản/mật khẩu."}), 400

    whitelist = _load_whitelist()
    if whitelist and username not in whitelist:
        return jsonify({"ok": False, "message": "Tài khoản không có quyền truy cập."}), 403

    # ✅ Gọi login tự động (không cần cvalue)
    result = authenticate_auto(username, password, max_retries=5)

    if result["ok"]:
        session["token"]    = result["token"]
        session["username"] = username
        return jsonify({
            "ok": True,
            "attempts": result.get("attempts", 1),
        })
    return jsonify({"ok": False, "message": result["message"]}), 401

@app.route("/api/predict", methods=["POST"])
def api_predict():
    img_bytes = None
    filename = "captcha.png"
    extra_info = None
    detected = None

    # 1. Upload file
    if "file" in request.files:
        f = request.files["file"]
        if f.filename:
            filename = f.filename
            img_bytes = f.read()

    # 2. Paste URL
    if img_bytes is None:
        url = (request.form.get("url") or "").strip()
        if not url:
            return jsonify(error="Upload a file or paste a URL"), 400

        # 2a. URL match 1 trong 21 source → dùng fetcher tương ứng
        detected = detect_source_from_url(url)
        if detected:
            img_bytes, extra_info = fetch_captcha(detected, timeout=10)
            if img_bytes is None:
                return jsonify(
                    error=f"[{detected}] {extra_info.get('error', 'fetch failed')}",
                    source=detected,
                    url=url,
                ), 400
            filename = f"{detected}.png"
        else:
            # 2b. URL ngoài 21 source → requests.get với headers
            try:
                resp = http_requests.get(
                    url, timeout=10,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/120.0.0.0 Safari/537.36",
                        "Referer": url.rsplit("/", 1)[0] + "/",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                )
                resp.raise_for_status()
                img_bytes = resp.content
                filename = url.rsplit("/", 1)[-1].split("?")[0] or "captcha.png"
            except Exception as e:
                return jsonify(error=f"Failed to fetch URL: {e}"), 400

    # 3. Validate ảnh
    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            size = (im.size[0], im.size[1])
    except Exception as e:
        return jsonify(error=f"Invalid image: {e}"), 400

    # 4. Lưu + predict (dùng OCR fallback)
    saved = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
    saved.write_bytes(img_bytes)

    response = predict_with_ocr_fallback(saved)
    response["filename"] = filename
    response["saved_path"] = str(saved)
    response["size"] = list(size)
    if detected:
        response["source"] = detected
        response["extra"] = extra_info

    return jsonify(response)


# ============================================================
# API: source registry
# ============================================================
@app.route("/api/sources", methods=["GET"])
def api_sources():
    return jsonify(sources=list_sources())


@app.route("/api/fetch/<source_id>", methods=["POST"])
def api_fetch(source_id):
    img_bytes, info = fetch_captcha(source_id, timeout=10)

    if img_bytes is None:
        return jsonify(error=info.get("error", "fetch failed"),
                       source=source_id), 400

    filename = f"fetch_{source_id}_{uuid.uuid4().hex[:8]}.png"
    saved = UPLOAD_DIR / filename
    saved.write_bytes(img_bytes)

    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            size = (im.size[0], im.size[1])
    except Exception as e:
        return jsonify(error=f"Invalid image: {e}"), 400

    response = predict_with_ocr_fallback(saved)
    response["source"] = source_id
    response["filename"] = filename
    response["saved_path"] = str(saved)
    response["size"] = list(size)
    response["extra"] = info

    return jsonify(response)


@app.route("/api/image")
def api_image():
    path = request.args.get("path", "")
    p = Path(path)
    if not p.is_file():
        return "not found", 404
    return send_file(p, mimetype="image/png")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host="0.0.0.0", port=port, debug=True)