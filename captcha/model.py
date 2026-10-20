"""CRNN: convolutional feature extractor + BiLSTM + CTC head.

The width-downsampling factor is **4** (2x2, 2x2, then 2x1). This is not a
stylistic choice: at /8 the narrow groups starve CTC of timesteps and the loss
silently goes to inf (masked by ``zero_infinity``). Measured at /8, three groups
violate the ``T >= 2L+1`` design rule -- ``120x40`` (T=12 < 13), ``80x30``
(T=10 < 11) and ``160x75`` (T=8 < 13). At /4 every group satisfies it (T = 17..50).
"""

from __future__ import annotations

from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import GroupCfg


def _conv_block(cin: int, cout: int, pool: tuple) -> list:
    return [
        nn.Conv2d(cin, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(kernel_size=pool, stride=pool),
    ]


def conv_out_width(width: int) -> int:
    """Number of CTC timesteps the CNN emits for a given input width.

    Mirrors the pooling arithmetic exactly: (2,2) -> (2,2) -> (2,1), so the total
    width downsampling factor is 4. Exposed at module level so tooling can check
    the ``T >= 2L+1`` rule without instantiating a model.
    """
    w = int(width)
    w //= 2
    w //= 2
    return max(1, w)


class CRNN(nn.Module):
    """Input (B, C, 32, W) -> log-probabilities (T, B, num_classes).

    ``LogSoftmax`` is part of the module so the exported ONNX graph emits
    log-probs directly and there is exactly one definition of the decode contract.
    """

    def __init__(self, num_classes: int, in_channels: int = 1, hidden: int = 256,
                 rnn_layers: int = 2, dropout: float = 0.1, input_height: int = 32,
                 assert_shape: bool = True):
        super().__init__()
        if num_classes < 2:
            raise ValueError("num_classes must include the CTC blank")
        self.num_classes = int(num_classes)
        self.in_channels = int(in_channels)
        self.hidden = int(hidden)
        self.rnn_layers = int(rnn_layers)
        self.input_height = int(input_height)
        self.assert_shape = bool(assert_shape)

        self.cnn = nn.Sequential(
            *_conv_block(self.in_channels, 64, (2, 2)),
            *_conv_block(64, 128, (2, 2)),
            *_conv_block(128, 256, (2, 1)),      # height halves, width kept -> /4
        )

        # Derive the flattened feature size from a dummy pass so the model stays
        # correct if the canonical height ever changes.
        with torch.no_grad():
            probe = torch.zeros(1, self.in_channels, self.input_height, 8)
            feat = self.cnn(probe)
            self.feat_h = int(feat.shape[2])
            feat_dim = int(feat.shape[1] * feat.shape[2])

        self.proj = nn.Linear(feat_dim, self.hidden)
        self.rnn = nn.LSTM(self.hidden, self.hidden // 2, num_layers=self.rnn_layers,
                           bidirectional=True, batch_first=False,
                           dropout=dropout if self.rnn_layers > 1 else 0.0)
        self.fc = nn.Linear(self.hidden, self.num_classes)
        self.log_softmax = nn.LogSoftmax(dim=2)

    # ------------------------------------------------------------------ shape
    def conv_out_width(self, width: int) -> int:
        """Number of CTC timesteps for a given input width (see the module-level
        :func:`conv_out_width`)."""
        return conv_out_width(width)

    # ---------------------------------------------------------------- forward
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        feat = self.cnn(x)                                # (B, 256, H', W')
        b, c, h, w = feat.shape
        if self.assert_shape:
            expected = self.conv_out_width(x.shape[3])
            if w != expected:
                raise AssertionError(
                    f"CNN emitted {w} timesteps for input width {x.shape[3]} but "
                    f"conv_out_width() predicted {expected}; the CTC input_lengths "
                    f"would be wrong"
                )
        feat = feat.permute(3, 0, 1, 2).flatten(2)        # (T, B, C*H')
        feat = self.proj(feat)                            # (T, B, hidden)
        feat, _ = self.rnn(feat)                          # (T, B, hidden)
        logits = self.fc(feat)                            # (T, B, num_classes)
        return self.log_softmax(logits)                   # (T, B, num_classes)

    # ------------------------------------------------------------- factory
    @classmethod
    def from_group(cls, group: GroupCfg, hidden: int = 256, rnn_layers: int = 2,
                   dropout: float = 0.1, assert_shape: bool = True) -> "CRNN":
        return cls(num_classes=group.num_classes, in_channels=group.in_channels,
                   hidden=hidden, rnn_layers=rnn_layers, dropout=dropout,
                   input_height=group.height, assert_shape=assert_shape)

    def config(self) -> dict:
        return {"num_classes": self.num_classes, "in_channels": self.in_channels,
                "hidden": self.hidden, "rnn_layers": self.rnn_layers,
                "input_height": self.input_height}


def build_model(group: GroupCfg, train_cfg: Optional[object] = None,
                assert_shape: bool = True) -> CRNN:
    """Build a CRNN for a group, picking hyper-parameters off TrainCfg when given."""
    kw = {"hidden": 256, "rnn_layers": 2, "dropout": 0.1}
    if train_cfg is not None:
        kw = {"hidden": getattr(train_cfg, "hidden", 256),
              "rnn_layers": getattr(train_cfg, "rnn_layers", 2),
              "dropout": getattr(train_cfg, "dropout", 0.1)}
    return CRNN.from_group(group, assert_shape=assert_shape, **kw)


def parameter_count(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
