"""Inference: size routing, torch and ONNX backends, greedy decode.

Routing is exact because image size is a perfect discriminator across this
dataset -- every one of the 25,976 files falls in exactly one of 14 sizes. An
unseen size is still handled, but never silently: the result carries a
``fallback`` flag, the log records the unmatched size so a new group can be added,
and ``--strict`` refuses to guess at all.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
from PIL import Image

from .config import Config, GroupCfg, default_config_path, runs_dir
from .ctc import greedy_decode
from .model import CRNN
from .transforms import load_image, preprocess
from .utils import get_device

log = logging.getLogger("captcha")

LOW_CONFIDENCE = 0.6


@dataclass
class Prediction:
    text: str
    confidence: float
    min_char_prob: float
    group_id: str
    size: Tuple[int, int]
    fallback: bool = False
    empty: bool = False
    low_confidence: bool = False
    error: str = ""

    def as_row(self, path: str = "", truth: str = "") -> Dict[str, object]:
        return {"path": path, "pred": self.text, "truth": truth,
                "confidence": round(self.confidence, 4), "group": self.group_id,
                "fallback": int(self.fallback), "error": self.error}


class Predictor:
    """Loads group models on demand from ``runs/<group>/``."""

    def __init__(self, cfg: Config, project_root: Path, backend: str = "torch",
                 device: Optional[torch.device] = None, strict: bool = False,
                 enforce_group: Optional[str] = None):
        if backend not in ("torch", "onnx"):
            raise ValueError("backend must be 'torch' or 'onnx'")
        for _group in cfg.groups:
            _group.require_unmerged("predict")
        self.cfg = cfg
        self.root = Path(project_root)
        self.backend = backend
        self.device = device or get_device("auto")
        self.strict = strict
        self.enforce_group = enforce_group
        self._cache: Dict[str, object] = {}
        self.unmatched_sizes: Dict[Tuple[int, int], int] = {}

    # ------------------------------------------------------------- loading
    def _run_dir(self, group: GroupCfg) -> Path:
        return runs_dir(self.root, group.group_id)

    def _load_torch(self, group: GroupCfg):
        path = self._run_dir(group) / "best.pt"
        if not path.exists():
            raise FileNotFoundError(
                f"no checkpoint for {group.group_id} at {path}; train it first "
                f"(python -m captcha.train --group {group.group_id})")
        ckpt = torch.load(path, map_location=self.device, weights_only=False)
        meta = ckpt["meta"]
        if meta.get("charset") != group.charset:
            raise ValueError(
                f"checkpoint {path} was trained with charset {meta.get('charset')!r} "
                f"but the config says {group.charset!r}; refusing to load")
        model = CRNN(num_classes=meta["num_classes"], in_channels=meta["in_channels"],
                     hidden=meta["hidden"], rnn_layers=meta["rnn_layers"],
                     input_height=meta["height"])
        model.load_state_dict(ckpt["model"])
        model.to(self.device).eval()
        return model

    def _load_onnx(self, group: GroupCfg):
        import onnxruntime as ort

        path = self._run_dir(group) / "model.onnx"
        if not path.exists():
            raise FileNotFoundError(
                f"no ONNX model for {group.group_id} at {path}; run "
                f"python -m captcha.export_onnx --group {group.group_id}")
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if self.device.type == "cuda" else ["CPUExecutionProvider"])
        session = ort.InferenceSession(str(path), providers=providers)
        return session

    def model_for(self, group: GroupCfg):
        if group.group_id not in self._cache:
            log.info("loading %s backend for %s", self.backend, group.group_id)
            self._cache[group.group_id] = (
                self._load_torch(group) if self.backend == "torch"
                else self._load_onnx(group))
        return self._cache[group.group_id]

    # ------------------------------------------------------------- routing
    def route(self, size: Tuple[int, int]) -> Tuple[GroupCfg, bool]:
        if self.enforce_group:
            return self.cfg.by_id(self.enforce_group), False
        router = self.cfg.size_router()
        if size in router:
            return router[size], False
        if self.strict:
            raise KeyError(
                f"image size {size} matches no configured group and --strict is set")
        group, score = self.cfg.nearest_group(size)
        self.unmatched_sizes[size] = self.unmatched_sizes.get(size, 0) + 1
        log.warning("unmatched size %s -> nearest group %s (score %.3f, aspect "
                    "distorted); add a group if this recurs", size, group.group_id,
                    score)
        return group, True

    # ----------------------------------------------------------- inference
    def _canonical(self, image: np.ndarray, group: GroupCfg) -> np.ndarray:
        """Force the routed group's canonical width.

        A no-op for exact size matches (width already matches); a deliberate
        aspect distortion for the nearest-group fallback. Static ONNX graphs
        require it.
        """
        if image.shape[2] == group.width:
            return image
        size = (group.width, group.height)
        if image.shape[0] == 1:
            # cv2 drops a singleton trailing axis, so grayscale is resized as a
            # genuine 2-D plane and the channel axis is restored afterwards.
            plane = cv2.resize(image[0], size, interpolation=cv2.INTER_LINEAR)
            return plane[None, :, :]
        hwc = cv2.resize(np.transpose(image, (1, 2, 0)), size,
                         interpolation=cv2.INTER_LINEAR)
        return np.transpose(hwc, (2, 0, 1))

    def predict_array(self, rgb: np.ndarray, size: Tuple[int, int]
                      ) -> Prediction:
        group, fallback = self.route(size)
        image, _info = preprocess(rgb, self.cfg.preproc_for(group), height=group.height)
        image = self._canonical(image, group)

        if self.backend == "torch":
            model = self.model_for(group)
            with torch.no_grad():
                tensor = torch.from_numpy(image[None]).to(self.device)
                log_probs = model(tensor).cpu().numpy()
        else:
            session = self.model_for(group)
            name = session.get_inputs()[0].name
            log_probs = session.run(None, {name: image[None].astype(np.float32)})[0]

        decoded = greedy_decode(log_probs, group.charset)[0]
        low = bool(fallback or decoded.confidence < LOW_CONFIDENCE)
        return Prediction(text=decoded.text, confidence=decoded.confidence,
                          min_char_prob=decoded.min_char_prob, group_id=group.group_id,
                          size=size, fallback=fallback, empty=decoded.empty,
                          low_confidence=low)

    def predict_path(self, path: Path) -> Prediction:
        path = Path(path)
        try:
            with Image.open(path) as im:
                size = (int(im.size[0]), int(im.size[1]))
            rgb = load_image(path)
            return self.predict_array(rgb, size)
        except Exception as exc:
            log.error("failed on %s: %s", path, exc)
            return Prediction(text="", confidence=0.0, min_char_prob=0.0,
                              group_id="", size=(0, 0), error=str(exc))

    def predict_dir(self, directory: Path, labels_from_filename: bool = False
                    ) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        paths = sorted(p for p in Path(directory).iterdir()
                       if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"))
        for path in paths:
            truth = ""
            if labels_from_filename and "_" in path.stem:
                truth = path.stem.rsplit("_", 1)[1]
            rows.append(self.predict_path(path).as_row(str(path), truth))
        return rows

    def summary(self) -> str:
        if not self.unmatched_sizes:
            return ""
        items = ", ".join(f"{k} x{v}" for k, v in self.unmatched_sizes.items())
        return f"unmatched sizes seen (no exact group): {items}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from .utils import setup_logging

    parser = argparse.ArgumentParser(description="Predict captcha text")
    parser.add_argument("--config", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--image", default=None)
    parser.add_argument("--dir", default=None)
    parser.add_argument("--group", default=None, help="force a group's model")
    parser.add_argument("--backend", choices=["torch", "onnx"], default="torch")
    parser.add_argument("--strict", action="store_true",
                        help="refuse the nearest-group fallback")
    parser.add_argument("--labels-from-filename", action="store_true")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--csv-out", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config) if args.config else default_config_path(root))
    predictor = Predictor(cfg, root, backend=args.backend,
                          device=get_device(args.device), strict=args.strict,
                          enforce_group=args.group)

    if args.image:
        pred = predictor.predict_path(Path(args.image))
        if pred.error:
            print(f"error: {pred.error}")
            return 1
        flag = " [FALLBACK]" if pred.fallback else ""
        low = " [LOW CONFIDENCE]" if pred.low_confidence else ""
        print(f"{pred.text}   (group={pred.group_id}, conf={pred.confidence:.3f}, "
              f"min_char={pred.min_char_prob:.3f}){flag}{low}")
        return 0

    if args.dir:
        rows = predictor.predict_dir(Path(args.dir), args.labels_from_filename)
        correct = sum(1 for r in rows if r["truth"] and r["pred"] == r["truth"])
        with_truth = sum(1 for r in rows if r["truth"])
        for row in rows[:20]:
            print(f"{row['path']}  pred={row['pred']!r} truth={row['truth']!r} "
                  f"conf={row['confidence']}")
        if len(rows) > 20:
            print(f"... {len(rows) - 20} more")
        if with_truth:
            print(f"exact match on labelled files: {correct}/{with_truth} "
                  f"= {correct / with_truth:.2%}")
        if args.csv_out:
            with Path(args.csv_out).open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["path", "pred", "truth",
                                                        "confidence", "group",
                                                        "fallback", "error"])
                writer.writeheader()
                writer.writerows(rows)
            print(f"wrote {args.csv_out}")
        note = predictor.summary()
        if note:
            print(note)
        return 0

    parser.error("pass --image or --dir")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
