"""ONNX export of the CRNN head, with a hard torch<->onnxruntime parity check.

Only the network is exported (CNN + BiLSTM + Linear + LogSoftmax), ending at
log-probs ``(T, B, C)``; the CTC loss is excluded (poorly supported in ONNX and
unnecessary) and greedy decoding runs in numpy outside the graph.

Static shapes are the default. Every image in a group has identical dimensions, so
production never needs a variable width, and a dynamic time axis combined with an
LSTM is a known-broken combination -- torch 2.x's dynamo exporter is stricter and
fails on LSTMs more often than the legacy one, hence ``dynamo=False``.

Contract: the ONNX model consumes the **already-normalized float tensor in
[-1, 1]**, not a uint8 image. All preprocessing stays in Python so the Flask app
and any external consumer behave identically.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import torch

from .config import Config, GroupCfg, default_config_path, runs_dir
from .model import CRNN
from .predict import _check_charset
from .utils import setup_logging

log = logging.getLogger("captcha")

PARITY_ATOL = 1e-4


def load_model_for_group(cfg: Config, group: GroupCfg, run_dir: Path,
                         device: torch.device = torch.device("cpu")):
    ckpt_path = Path(run_dir) / "best.pt"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"no checkpoint at {ckpt_path}; train the group first")
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    meta = ckpt["meta"]
    _check_charset(meta.get("charset"), group, str(ckpt_path))
    model = CRNN(num_classes=meta["num_classes"], in_channels=meta["in_channels"],
                 hidden=meta["hidden"], rnn_layers=meta["rnn_layers"],
                 input_height=meta["height"])
    model.load_state_dict(ckpt["model"])
    model.eval().to(device)
    return model, meta


def export_group(cfg: Config, group: GroupCfg, run_dir: Path,
                 out_path: Optional[Path] = None, dynamic: bool = False,
                 opset: int = 17, verify: bool = True) -> Path:
    """Export one group to ONNX and (by default) prove it matches PyTorch."""
    import onnxruntime as ort

    run_dir = Path(run_dir)
    out_path = Path(out_path) if out_path else run_dir / "model.onnx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    model, meta = load_model_for_group(cfg, group, run_dir)
    height = int(meta["height"])
    width = int(meta["width"])
    dummy = torch.zeros(1, int(meta["in_channels"]), height, width)

    dynamic_axes = None
    if dynamic:
        dynamic_axes = {"input": {0: "batch", 3: "width"},
                        "log_probs": {0: "time", 1: "batch"}}

    try:
        torch.onnx.export(
            model, dummy, str(out_path), opset_version=opset,
            do_constant_folding=True, training=torch.onnx.TrainingMode.EVAL,
            input_names=["input"], output_names=["log_probs"],
            dynamic_axes=dynamic_axes, dynamo=False,
        )
    except (ModuleNotFoundError, ImportError) as exc:
        # A missing exporter dependency is not something a different exporter can
        # paper over -- say what to install instead of silently switching paths.
        raise RuntimeError(
            f"ONNX export needs the 'onnx' package ({exc}); "
            f"pip install -r requirements.txt"
        ) from exc
    except Exception as exc:  # the dynamo flag is unsupported on older torch
        log.warning("export with dynamo=False failed (%s); retrying without the flag",
                    exc)
        torch.onnx.export(
            model, dummy, str(out_path), opset_version=opset,
            do_constant_folding=True, training=torch.onnx.TrainingMode.EVAL,
            input_names=["input"], output_names=["log_probs"],
            dynamic_axes=dynamic_axes,
        )

    if verify:
        with torch.no_grad():
            torch_out = model(dummy).numpy()
        session = ort.InferenceSession(str(out_path),
                                       providers=["CPUExecutionProvider"])
        ort_out = session.run(None, {"input": dummy.numpy()})[0]
        if torch_out.shape != ort_out.shape:
            raise AssertionError(
                f"ONNX output shape {ort_out.shape} != torch {torch_out.shape}; the "
                f"graph is not equivalent")
        diff = float(np.abs(torch_out - ort_out).max())
        log.info("parity %s: outputs %s, max|torch-onnx| = %.3e", group.group_id,
                 tuple(ort_out.shape), diff)
        if diff > PARITY_ATOL:
            raise AssertionError(
                f"ONNX export for {group.group_id} disagrees with PyTorch by "
                f"{diff:.3e} (> {PARITY_ATOL:.0e}); the exported graph produces "
                f"different numbers than the model it came from")

    payload = {"group_id": group.group_id, "charset": meta["charset"],
               "num_classes": int(meta["num_classes"]), "height": height, "width": width,
               "in_channels": int(meta["in_channels"]), "hidden": meta["hidden"],
               "rnn_layers": meta["rnn_layers"], "opset": opset, "dynamic": dynamic,
               "config_hash": cfg.hash_for(group),
               "input_contract": "float32 (1, C, H, W) normalized to [-1, 1]",
               "output": "log-probs (T, B, C); greedy decode outside the graph"}
    sidecar = out_path.with_suffix(out_path.suffix + ".meta.json")
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    try:  # embed the metadata in the graph too, when the onnx package is present
        import onnx

        graph = onnx.load(str(out_path))
        for key, value in payload.items():
            entry = graph.metadata_props.add()
            entry.key = str(key)
            entry.value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        onnx.save(graph, str(out_path))
    except ImportError:
        log.debug("onnx package not installed; metadata only in %s", sidecar.name)

    log.info("wrote %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)
    return out_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export a group's CRNN to ONNX")
    parser.add_argument("--config", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--group", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dynamic", action="store_true",
                        help="dynamic batch/width axes (LSTM + dynamic time is fragile)")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config) if args.config else default_config_path(root))

    if args.all:
        targets = cfg.groups
    elif args.group:
        targets = [cfg.by_id(args.group)]
    else:
        parser.error("pass --group <id> or --all")
        return 2

    failures = 0
    for group in targets:
        try:
            export_group(cfg, group, runs_dir(root, group.group_id),
                         dynamic=args.dynamic, opset=args.opset,
                         verify=not args.no_verify)
        except Exception as exc:
            failures += 1
            log.error("export failed for %s: %s", group.group_id, exc)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
