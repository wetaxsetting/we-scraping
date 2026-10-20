"""Per-group training loop.

The three things that actually matter here, all of them silent-failure guards:

1. **CTCLoss wiring order.** logits ``(T,B,C)`` -> ``log_softmax(dim=2)`` -> loss.
   The class dim is 2 *after* the permute, which the model does internally; doing
   it on the wrong dim before the permute is silent and wrong.
2. **AMP is CUDA-only and the loss stays fp32.** ``autocast("cpu", bfloat16)`` is
   slower on CPU and breaks CTCLoss numerics; on CUDA, CTCLoss in fp16 accumulates
   large negative log-probs and goes inf/NaN.
3. **zero-inf samples are counted, not hidden.** ``zero_infinity=True`` is a safety
   net -- it turns "label longer than T" into a silent zero-gradient sample. The
   ``T >= 2L+1`` rule is asserted before training starts, and any zero-inf that
   still appears is logged and warned about.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import torch
import torch.nn as nn

from .config import Config, GroupCfg, default_config_path, runs_dir
from .ctc import check_timesteps, input_lengths_for
from .dataset import GroupData, build_loaders, prepare_group, scan_index
from .evaluate import compute_metrics, predict_loader
from .model import build_model, parameter_count
from .utils import (append_history, configure_threads, get_device, rng_state,
                    save_checkpoint, set_rng_state, set_seed, setup_logging,
                    use_amp_for, worker_init_fn)

log = __import__("logging").getLogger("captcha")


@dataclass
class TrainResult:
    group_id: str
    best_exact_match: float = 0.0
    best_epoch: int = 0
    epochs_run: int = 0
    stopped_early: bool = False
    zero_inf_total: int = 0


def _make_criterion() -> nn.CTCLoss:
    return nn.CTCLoss(blank=0, zero_infinity=True, reduction="none")


def _improved(em: float, loss: float, best_em: float, best_loss: float,
              min_delta: float) -> bool:
    """Has this epoch beaten the best one -- EM first, loss as the tie-break.

    Exact-match is the metric that matters, but a CTC model spends its first
    epochs predicting nothing but blank, during which EM is identically 0.0 and
    every epoch ties. Selecting on EM alone therefore pins ``best.pt`` to the
    first epoch of the plateau, and -- worse -- makes both the LR schedule and
    early stopping read "still no text" as "no progress" and act exactly when the
    model needs the most room. Loss keeps falling throughout the plateau, so it
    is the signal that tells those epochs apart; once EM is moving it dominates
    and the loss tie-break is inert.
    """
    if em > best_em + min_delta:
        return True
    return (abs(em - best_em) <= min_delta and math.isfinite(loss)
            and best_loss - loss > min_delta)


def _ctc_loss(criterion: nn.CTCLoss, log_probs: torch.Tensor,
              targets: torch.Tensor, target_lengths: torch.Tensor
              ) -> tuple:
    """Per-sample CTC loss -> ``(mean_loss, n_zero_inf)``.

    ``reduction='none'`` plus ``zero_infinity=True`` means an unsupervised sample
    comes back as exactly 0.0 (a real CTC loss is never exactly zero), so counting
    zeros detects the samples the loss silently gave up on.
    """
    batch = log_probs.shape[1]
    input_lengths = input_lengths_for(batch, log_probs.shape[0], log_probs.device)
    flat = targets.to(log_probs.device)
    tlen = target_lengths.to(log_probs.device)
    valid = tlen > 0
    if not bool(valid.any()):
        return torch.zeros((), device=log_probs.device, requires_grad=True), batch

    per_sample = criterion(log_probs.float(), flat, input_lengths, tlen)
    n_zero = int((per_sample == 0).sum().item())
    good = per_sample[per_sample > 0]
    if good.numel() == 0:
        return torch.zeros((), device=log_probs.device, requires_grad=True), batch
    return good.mean(), n_zero


def _train_epoch(model: nn.Module, loader, optimizer, criterion, device,
                 use_amp: bool, grad_clip: float, scaler=None,
                 max_steps: int = 0) -> tuple:
    model.train()
    total_loss = 0.0
    n_batches = 0
    n_zero_inf = 0
    n_samples = 0

    for step, (images, targets, target_lengths, _labels, _names) in enumerate(loader):
        if max_steps and step >= max_steps:
            break
        images = images.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)

        if use_amp:
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                log_probs = model(images)
            loss, n_zero = _ctc_loss(criterion, log_probs, targets, target_lengths)
        else:
            log_probs = model(images)
            loss, n_zero = _ctc_loss(criterion, log_probs, targets, target_lengths)

        if not torch.isfinite(loss):
            log.warning("non-finite loss at step %d; skipping batch", step)
            continue

        if use_amp and scaler is not None:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            optimizer.step()

        total_loss += float(loss.item())
        n_batches += 1
        n_zero_inf += n_zero
        n_samples += images.shape[0]

    return (total_loss / max(1, n_batches), n_zero_inf, n_samples)


def train_group(cfg: Config, group: GroupCfg, data_dir: Path, run_dir: Path,
                device: torch.device, *, records=None, epochs: Optional[int] = None,
                batch_size: Optional[int] = None, lr: Optional[float] = None,
                limit: int = 0, max_steps: int = 0,
                num_workers: Optional[int] = None, resume: Optional[Path] = None,
                force: bool = False, make_plots: bool = True) -> TrainResult:
    """Train one group end to end. Safe to re-run: skips a finished group unless
    ``force``."""
    tcfg = cfg.train
    group.require_unmerged("train")
    epochs = int(epochs if epochs is not None else tcfg.epochs)
    batch_size = int(batch_size if batch_size is not None else tcfg.batch_size)
    lr = float(lr if lr is not None else tcfg.lr)
    workers = int(num_workers if num_workers is not None else tcfg.num_workers)

    run_dir = Path(run_dir)
    best_path = run_dir / "best.pt"
    last_path = run_dir / "last.pt"
    history_path = run_dir / "history.csv"

    if best_path.exists() and not force and resume is None:
        log.info("group %s already has %s; skipping (use --force to retrain)",
                 group.group_id, best_path)
        return TrainResult(group_id=group.group_id)

    if resume is None:
        # history.csv is appended so a resumed run keeps one continuous curve, but
        # a *fresh* run must not interleave its epochs with a previous run's --
        # that would produce a plot with two epoch 1s and a meaningless best.
        if history_path.exists():
            history_path.unlink()

    set_seed(tcfg.seed)
    configure_threads(workers)

    if records is None:
        records, _ = scan_index(data_dir)
    gd: GroupData = prepare_group(records, group, tcfg)
    if not gd.charset:
        raise ValueError(f"group {group.group_id} has an empty charset")

    model = build_model(group, tcfg).to(device)
    t = check_timesteps(group.width, max((len(r.label) for r in gd.all_records()), default=1),
                        model.conv_out_width, group.group_id, group.height)

    log.info("group %s: raw=%d dropped=%d train=%d val=%d test=%d | charset=%d "
             "T=%d classes=%d params=%s", group.group_id, gd.n_raw, gd.n_dropped,
             len(gd.train), len(gd.val), len(gd.test), len(gd.charset), t,
             group.num_classes, f"{parameter_count(model):,}")
    if gd.charset_mismatch:
        log.warning("group %s: derived charset %r differs from the configured %r; "
                    "the configured one is authoritative for routing",
                    group.group_id, "".join(gd.charset), group.charset)
    if gd.note:
        log.info("  charset policy: %s", gd.note)

    effective_limit = limit or tcfg.limit
    val_limit = min(len(gd.val), 128) if effective_limit else 0
    if effective_limit:
        gd.val = gd.val[:val_limit] or gd.val
    tr_loader, va_loader, te_loader, sampler = build_loaders(
        gd, cfg.preproc_for(group), cfg.aug, data_dir, batch_size, workers, device, tcfg,
        limit=effective_limit)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr,
                                  weight_decay=tcfg.weight_decay)
    # Monitored on val *loss*, not on exact-match: during the CTC blank plateau
    # EM is 0.0 for many epochs, so an EM-monitored schedule halves the LR exactly
    # when the model is trying to escape the plateau. Loss falls the whole time
    # and is what the scheduler should be reacting to.
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=tcfg.lr_factor, patience=tcfg.lr_patience,
        min_lr=tcfg.min_lr)
    criterion = _make_criterion()
    use_amp = use_amp_for(device)
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp) if use_amp else None

    start_epoch = 1
    best_em = -1.0
    best_loss = float("inf")
    best_epoch = 0
    zero_inf_total = 0
    result = TrainResult(group_id=group.group_id)

    if resume is not None and Path(resume).exists():
        ckpt = torch.load(Path(resume), map_location=device, weights_only=False)
        model.load_state_dict(ckpt["model"])
        if "optimizer" in ckpt:
            optimizer.load_state_dict(ckpt["optimizer"])
        start_epoch = int(ckpt.get("epoch", 0)) + 1
        best_em = float(ckpt.get("best_em", -1.0))
        best_loss = float(ckpt.get("best_loss", float("inf")))
        best_epoch = int(ckpt.get("best_epoch", 0))
        if "rng" in ckpt:
            set_rng_state(ckpt["rng"])
        log.info("resumed %s at epoch %d (best EM %.2f%%)", group.group_id,
                 start_epoch, best_em * 100)

    no_improve = 0
    for epoch in range(start_epoch, epochs + 1):
        if sampler is not None and hasattr(sampler, "set_epoch"):
            sampler.set_epoch(epoch)
        t0 = time.time()
        train_loss, n_zero, n_seen = _train_epoch(
            model, tr_loader, optimizer, criterion, device, use_amp, tcfg.grad_clip,
            scaler, max_steps or tcfg.max_steps)
        zero_inf_total += n_zero

        val_rows = predict_loader(model, va_loader, gd.charset, device, criterion)
        vm = compute_metrics(val_rows)
        scheduler.step(vm.loss if math.isfinite(vm.loss) else float("inf"))

        train_rows = predict_loader(model, tr_loader, gd.charset, device, criterion,
                                    max_batches=4)
        tm = compute_metrics(train_rows)
        elapsed = time.time() - t0

        append_history(history_path, {
            "epoch": epoch, "lr": optimizer.param_groups[0]["lr"],
            "train_loss": round(train_loss, 5), "train_em": round(tm.exact_match, 5),
            "val_loss": round(vm.loss, 5) if math.isfinite(vm.loss) else "",
            "val_em": round(vm.exact_match, 5), "val_char_acc": round(vm.char_acc, 5),
            "zero_inf_frac": round(n_zero / max(1, n_seen), 6), "sec": round(elapsed, 1)})

        log.info("epoch %3d/%d | train loss %.4f em %.1f%% | val loss %.4f em %.1f%% "
                 "char %.1f%% | lr %.2e | %.0fs", epoch, epochs, train_loss,
                 tm.exact_match * 100, vm.loss if math.isfinite(vm.loss) else -1,
                 vm.exact_match * 100, vm.char_acc * 100,
                 optimizer.param_groups[0]["lr"], elapsed)

        if n_zero / max(1, n_seen) > 0.001:
            log.warning("epoch %d: %.3f%% of samples produced zero-inf CTCLoss "
                        "(labels the loss gave up on)", epoch,
                        100 * n_zero / max(1, n_seen))

        result.epochs_run = epoch
        improved = _improved(vm.exact_match, vm.loss, best_em, best_loss,
                             tcfg.min_delta)
        if improved:
            best_em, best_epoch = vm.exact_match, epoch
            if math.isfinite(vm.loss):
                best_loss = vm.loss
            no_improve = 0
        else:
            no_improve += 1

        payload = dict(
            model=model.state_dict(), optimizer=optimizer.state_dict(), epoch=epoch,
            best_em=best_em, best_loss=best_loss, best_epoch=best_epoch,
            rng=rng_state(),
            meta=dict(group_id=group.group_id, charset="".join(gd.charset),
                      num_classes=group.num_classes, in_channels=group.in_channels,
                      hidden=tcfg.hidden, rnn_layers=tcfg.rnn_layers,
                      width=group.width, height=group.height,
                      config_hash=cfg.hash_for(group),
                      val_exact_match=vm.exact_match, val_char_acc=vm.char_acc,
                      note=gd.note))
        save_checkpoint(last_path, **payload)

        if improved:
            save_checkpoint(best_path, **payload)

        if no_improve >= tcfg.patience:
            log.info("early stop at epoch %d (no val improvement for %d epochs; "
                     "best EM %.2f%% loss %.4f @ %d)", epoch, no_improve,
                     best_em * 100, best_loss, best_epoch)
            result.stopped_early = True
            break

    result.best_exact_match = max(best_em, 0.0)
    result.best_epoch = best_epoch
    result.zero_inf_total = zero_inf_total

    if make_plots and history_path.exists():
        try:
            from .plotting import plot_history
            plot_history(history_path, run_dir / "plots", group.group_id)
        except Exception as exc:  # plotting is optional (needs matplotlib)
            log.info("skipped plots: %s", exc)

    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Train CRNN+CTC captcha models")
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--group", default=None, help="group id, e.g. g_80x30")
    parser.add_argument("--all", action="store_true", help="train every group in the config")
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--limit", type=int, default=0, help="cap training images (smoke tests)")
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--num-workers", type=int, default=None)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    root = Path(args.project_root).resolve()
    cfg = Config.load(Path(args.config) if args.config else default_config_path(root))
    data_dir = Path(args.data_dir) if args.data_dir else root / cfg.data_dir
    device = get_device(args.device)
    log.info("device=%s amp=%s", device, use_amp_for(device))

    if not cfg.groups:
        log.error("config has no groups; run: python -m captcha.inspect_data "
                  "--report --write-config configs/groups.yaml")
        return 2

    if args.all:
        targets: List[GroupCfg] = cfg.groups
    elif args.group:
        targets = [cfg.by_id(args.group)]
    else:
        log.error("pass --group <id> or --all")
        return 2

    records, _ = scan_index(data_dir)
    results = []
    for group in targets:
        try:
            results.append(train_group(
                cfg, group, data_dir, runs_dir(root, group.group_id), device,
                records=records, epochs=args.epochs, batch_size=args.batch_size,
                lr=args.lr, limit=args.limit, max_steps=args.max_steps,
                num_workers=args.num_workers,
                resume=Path(args.resume) if args.resume else None,
                force=args.force, make_plots=not args.no_plots))
        except Exception as exc:  # keep --all going across 14 independent groups
            log.exception("group %s failed: %s", group.group_id, exc)

    log.info("--- summary ---")
    for r in results:
        log.info("%-10s best EM %.2f%% @ epoch %d (%d epochs%s)%s",
                 r.group_id, r.best_exact_match * 100, r.best_epoch, r.epochs_run,
                 ", early stop" if r.stopped_early else "",
                 f", zero-inf {r.zero_inf_total}" if r.zero_inf_total else "")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
