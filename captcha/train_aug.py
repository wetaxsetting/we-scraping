"""Train giống hệt ``captcha.train``, chỉ khác: augmentation đổi theo từng epoch.

``CaptchaDataset`` gốc tạo RNG bằng ``default_rng((seed, index))``, không có số
epoch, nên mỗi ảnh bị biến đổi y hệt nhau ở mọi epoch. Nhóm ít ảnh mà khó đọc thì
model thuộc lòng đúng tập ảnh cố định đó: ``g_200x80`` 28/10 ra train_em 100%,
val_em 6%.

Ở đây thêm 1 số rút từ RNG của torch vào seed. Trong tiến trình chính, RNG đó
được ``set_seed`` khởi tạo; trong DataLoader worker, mỗi worker có seed riêng
(``base_seed + worker_id``) và RNG chạy liên tục qua các epoch (kể cả
``persistent_workers``). Nên mỗi lần lấy ảnh là 1 biến thể mới, mà vẫn tái lập
được khi giữ nguyên seed và ``num_workers``.

File gốc ``captcha/train.py`` / ``captcha/dataset.py`` không bị sửa. Lớp mới
nằm ở module này (không phải gán đè hàm), để worker trên Windows (spawn) unpickle
dataset ra đúng lớp.

    python -X utf8 -m captcha.train_aug --group g_200x80 --data-dir env/g_200x80 --epochs 100 --force
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import torch

from . import dataset as _dataset
from . import train as _train
from .transforms import preprocess_path, preprocess_train


class EpochAugDataset(_dataset.CaptchaDataset):
    def __getitem__(self, i: int) -> Dict[str, object]:
        rec = self.records[i]
        path = rec.path(self.data_dir)
        if self.aug is not None and self.aug.enabled:
            draw = int(torch.randint(0, 2**31 - 1, (1,)).item())
            rng = np.random.default_rng((self.seed, i, draw))
            image, _info = preprocess_train(path, self.preproc, self.aug, rng)
        else:
            image, _info = preprocess_path(path, self.preproc)
        encoded = [self.index[ch] for ch in rec.label]
        return {"image": torch.from_numpy(image), "label": rec.label,
                "encoded": encoded, "name": rec.name}


# build_loaders tra tên CaptchaDataset trong module dataset lúc chạy.
_dataset.CaptchaDataset = EpochAugDataset


def main(argv=None) -> int:
    print("train_aug: augmentation thay đổi theo epoch (EpochAugDataset)", flush=True)
    return _train.main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
