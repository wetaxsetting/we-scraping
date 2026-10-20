"""Flask demo: upload a captcha, get the text back with a confidence score.

Run it with the ONNX backend for a realistic deployment shape::

    python app.py --backend onnx
    python app.py --backend torch --port 5001

The app reports *how* it produced an answer, not just the answer. A size that
matches no configured group is served by the nearest-group fallback, which
distorts the aspect ratio -- such results are labelled, and low-confidence ones
are labelled too, because a demo that hides its own uncertainty is worse than
useless.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
from pathlib import Path
from typing import Optional, Sequence

from flask import Flask, jsonify, render_template_string, request
from PIL import Image

from captcha.config import Config, default_config_path, runs_dir
from captcha.predict import Predictor
from captcha.transforms import load_image
from captcha.utils import get_device, setup_logging

log = logging.getLogger("captcha")

MAX_UPLOAD_BYTES = 4 * 1024 * 1024

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CRNN captcha reader</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#f6f6f7; --card:#fff;
          --line:#dcdce0; --muted:#6b6b73; --accent:#2f6feb; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e8e8ea; --bg:#141416; --card:#1e1e21; --line:#33333a;
            --muted:#9a9aa3; --accent:#6ea8ff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:32px 16px; background:var(--bg); color:var(--fg);
         font:15px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
  .wrap { max-width:720px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:20px; margin-bottom:16px; }
  input[type=file] { width:100%; padding:10px; border:1px dashed var(--line);
                     border-radius:8px; background:transparent; color:var(--fg); }
  button { margin-top:14px; padding:10px 18px; border:0; border-radius:8px;
           background:var(--accent); color:#fff; font-size:15px; cursor:pointer; }
  .text { font:600 34px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
          letter-spacing:.06em; word-break:break-all; margin:0 0 12px; }
  .meta { color:var(--muted); font-size:13px; }
  .badges { margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; }
  .badge { font-size:12px; padding:3px 9px; border-radius:999px;
           border:1px solid var(--line); }
  .warn { border-color:#d9822b; color:#b3661a; }
  .bad  { border-color:#d64545; color:#b32d2d; }
  .ok   { border-color:#2f9e44; color:#227a33; }
  .err  { border-left:3px solid #d64545; padding-left:12px; }
  img.prev { max-width:100%; border:1px solid var(--line); border-radius:8px;
             background:#fff; image-rendering:pixelated; }
  table { border-collapse:collapse; font-size:13px; width:100%; }
  td, th { text-align:left; padding:5px 10px 5px 0; border-bottom:1px solid var(--line); }
  code { background:rgba(127,127,127,.15); padding:1px 5px; border-radius:4px; }
</style>
</head>
<body><div class="wrap">
  <h1>CRNN captcha reader</h1>
  <p class="sub">Backend <code>{{ backend }}</code> &middot; {{ n_ready }}/{{ n_groups }}
     group models loaded &middot; routed by image size</p>

  <div class="card">
    <form method="post" enctype="multipart/form-data">
      <input type="file" name="image" accept="image/*" required>
      <button type="submit">Read captcha</button>
    </form>
  </div>

  {% if error %}
  <div class="card err"><strong>Error:</strong> {{ error }}</div>
  {% endif %}

  {% if result %}
  <div class="card">
    {% if preview %}<img class="prev" src="{{ preview }}" alt="uploaded captcha">{% endif %}
    <p class="text">{{ result.text or "(nothing decoded)" }}</p>
    <p class="meta">
      group <code>{{ result.group_id }}</code> &middot;
      size {{ result.size[0] }}&times;{{ result.size[1] }} &middot;
      confidence {{ "%.3f"|format(result.confidence) }} &middot;
      weakest char {{ "%.3f"|format(result.min_char_prob) }}
    </p>
    <div class="badges">
      {% if result.error %}<span class="badge bad">failed</span>{% endif %}
      {% if result.empty %}<span class="badge warn">empty prediction</span>{% endif %}
      {% if result.fallback %}<span class="badge warn">nearest-group fallback
        &mdash; size not configured</span>{% endif %}
      {% if result.low_confidence and not result.fallback %}
        <span class="badge warn">low confidence</span>{% endif %}
      {% if not result.error and not result.fallback
            and not result.low_confidence and not result.empty %}
        <span class="badge ok">confident</span>{% endif %}
    </div>
  </div>
  {% endif %}

  <div class="card">
    <table>
      <tr><th>group</th><th>size</th><th>charset</th><th>model</th></tr>
      {% for g in groups %}
      <tr>
        <td><code>{{ g.group_id }}</code></td>
        <td>{{ g.sizes }}</td>
        <td>{{ g.charset }}</td>
        <td>{{ "ready" if g.ready else "not trained" }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div></body></html>
"""


def create_app(cfg: Config, project_root: Path, backend: str = "onnx",
               device=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    predictor = Predictor(cfg, project_root, backend=backend, device=device)

    def group_rows():
        run = runs_dir(project_root, "")  # parent dir of all runs
        rows = []
        for g in cfg.groups:
            ready = (run / g.group_id / ("best.pt" if backend == "torch"
                                         else "model.onnx")).exists()
            rows.append({"group_id": g.group_id, "sizes": " ".join(
                f"{w}x{h}" for w, h in g.sizes), "charset": g.charset,
                "ready": ready})
        return rows

    @app.route("/", methods=["GET", "POST"])
    def index():
        result = preview = None
        error = None
        if request.method == "POST":
            upload = request.files.get("image")
            if upload is None or not upload.filename:
                error = "no file selected"
            else:
                raw = upload.read()
                try:
                    with Image.open(io.BytesIO(raw)) as im:
                        size = (int(im.size[0]), int(im.size[1]))
                        im.seek(0)
                        im.thumbnail((320, 320))
                        buf = io.BytesIO()
                        im.convert("RGB").save(buf, format="PNG")
                    preview = "data:image/png;base64," + base64.b64encode(
                        buf.getvalue()).decode("ascii")
                    rgb = load_image(raw)
                    result = predictor.predict_array(rgb, size)
                except Exception as exc:              # surface, never swallow
                    log.exception("prediction failed")
                    error = f"{type(exc).__name__}: {exc}"
        rows = group_rows()
        return render_template_string(
            PAGE, result=result, preview=preview, error=error, groups=rows,
            backend=backend, n_groups=len(rows),
            n_ready=sum(1 for r in rows if r["ready"]))

    @app.route("/predict", methods=["POST"])
    def predict_json():
        upload = request.files.get("image")
        if upload is None or not upload.filename:
            return jsonify({"error": "no file selected"}), 400
        raw = upload.read()
        try:
            with Image.open(io.BytesIO(raw)) as im:
                size = (int(im.size[0]), int(im.size[1]))
            result = predictor.predict_array(load_image(raw), size)
        except Exception as exc:
            log.exception("prediction failed")
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify(result.as_row(path=upload.filename))

    @app.route("/healthz")
    def healthz():
        rows = group_rows()
        ready = [r["group_id"] for r in rows if r["ready"]]
        return jsonify({"status": "ok" if ready else "no models",
                        "backend": backend, "groups": len(rows),
                        "ready": ready})

    return app


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Flask demo for the CRNN reader")
    parser.add_argument("--config", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--backend", choices=["torch", "onnx"], default="onnx")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config) if args.config else default_config_path(root))
    if not cfg.groups:
        parser.error(f"no groups in the config; run "
                     f"'python -m captcha.inspect_data --write-config "
                     f"configs/groups.yaml' first")

    app = create_app(cfg, root, backend=args.backend, device=get_device(args.device))
    print(f"serving on http://{args.host}:{args.port}  (backend={args.backend})")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
