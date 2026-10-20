# CRNN captcha reader

Train **CRNN + CTC** để đọc bộ captcha trong `pass/`. Mỗi nguồn captcha có một model
riêng, route theo kích thước ảnh lúc inference.

> Phạm vi: dùng cho captcha của chính bạn / được phép xử lý (kiểm thử hệ thống của
> mình, nghiên cứu, học tập).

## Dữ liệu: 14 nguồn, không phải 1

`pass/` có **25,976 PNG**, tên `{index}_{label}.png`, **nhãn nằm trong tên file**
(đã verify trên nhiều mẫu). `pass/labels.csv` chỉ có 7,200/25,976 dòng nên **không
bao giờ được dùng làm nguồn nhãn** — chỉ cross-check (kết quả: 0 mismatch).

Kích thước ảnh phân biệt hoàn toàn 14 nguồn, nên routing lúc inference là **khớp
chính xác**, không cần đoán:

| group | size | n | charset lõi | drop | W | T | cần |
|---|---|---|---|---|---|---|---|
| `g_180x64` | 180×64 | 7236 | `0-9A-Z` (36) | 0.4% | 90 | 22 | 11 |
| `g_120x40` | 120×40 | 3000 | `23456789Z` + thường (30) | 7.4% | 96 | 24 | 13 |
| `g_80x30` | 80×30 | 2000 | `0-9` (10) | 2.4% | 85 | 21 | 11 |
| `g_300x75` | 300×75 | 2000 | `0-9` (10) | 0.6% | 128 | 32 | 11 |
| `g_160x75` | 160×75 | 1995 | `23456789` + thường (30) | 0.3% | 68 | 17 | 13 |
| `g_600x120` | 600×120 | 1784 | mixed (34) | **15.2%** | 160 | 40 | 15 |
| `g_120x38` | 120×38 | 1000 | `0-9` (10) | 0.5% | 101 | 25 | 11 |
| `g_150x34` | 150×34 | 1000 | `0-9` (10) | 1.9% | 141 | 35 | 13 |
| `g_194x35` | 194×35 | 1000 | `0-9z` (11) | 6.7% | 177 | 44 | 15 |
| `g_150x30` | 150×30 | 1000 | mixed (43) | **10.8%** | 160 | 40 | 13 |
| `g_1500x350` | 1500×350 | 1000 | `0-9Oo` (12) | 5.2% | 137 | 34 | 9 |
| `g_250x40` | 250×40 | 1000 | mixed (36) | **17.4%** | 200 | 50 | 21 |
| `g_150x39` | 150×39 | 995 | `567hmny` (7) | 3.8% | 123 | 30 | 9 |
| `g_180x50` | 180×50 | 966 | `123456789` + thường (28) | 2.8% | 115 | 28 | 13 |

Con số luôn đúng là do tool sinh ra: `python -m captcha.inspect_data --report`.

`T` là số timestep CTC mà CNN phát ra, `cần` = `2·maxL+1`. **Mọi nhóm đều thoả.**

### Ba phát hiện quyết định độ chính xác

**1. Nhiễu nhãn là trần accuracy, không phải model.** Nhãn do người gán, và các ký
tự dễ nhầm bị gán sai có hệ thống:

- `g_80x30` (chỉ chữ số): đuôi `l:32 I:3 T:2`. Nhìn ảnh thì font vẽ số `1` kiểu
  serif có chân — **giống hệt `l` và `I`**. `002354_061l` và `002161_1616` dùng
  cùng một glyph. Đuôi `l`/`I` chính là số `1` bị gán sai.
- `g_600x120`: `004293_33xxSM` — ảnh vẽ `XX` hoa, nhãn ghi `xx` thường.
- 227 nhóm trùng nội dung md5 có **3 nhóm nhãn mâu thuẫn**, và một trong đó là
  `QKZFJ` vs `qkzfj` — **lệch đúng hoa/thường**. Hai nhóm còn lại là `0`↔`o`.

Hệ quả: nhãn 5 ký tự có ~5–10% khả năng chứa ít nhất 1 ký tự sai. **Exact-match
không thể đạt 100%, và đó không phải lỗi cần sửa.** `charset_mode: core` loại các
ký tự đuôi hiếm; bảng confusion trong `evaluate` là chỗ phân biệt "nhiễu nhãn" với
"model dở" — nếu top toàn `c↔C, x↔X` thì là nhiễu.

**2. `g_250x40` không phải một nguồn captcha.** Nó là một túi trộn: nền hồng viền
mảnh, đen/trắng, nâu chữ vàng, xanh dương chữ trắng, cầu vồng, kèm watermark "TM".
Đó là lý do drop 17.4%. Nhóm này nên được tách theo style nếu cần accuracy cao.

**3. 5/14 nhóm có nền tối** (ring median 0–87 so với 149–255). Không chuẩn hoá
polarity thì 5 nhóm này bị train trên ảnh đảo ngược so với phần còn lại.

### Ràng buộc CTC — chỗ dễ sai nhất

Quy tắc đúng là **`T ≥ 2·maxL + 1`**, không phải `T ≥ maxL`. Ở downsampling width
**/8** (3× `MaxPool(2,2)`), **3 nhóm vi phạm**: `120x40` (T=12 < 13), `80x30`
(T=10 < 11), `160x75` (T=8 < 13) → CTCLoss trả `inf` và bị `zero_infinity` che mất,
model âm thầm không học gì.

→ Block 3 dùng `MaxPool((2,1),(2,1))`, tổng downsampling = **4**. Khi đó T = 17…50,
cả 14 nhóm thoả. Có **assert lúc dựng dataset** chặn trước khi train, và đếm số mẫu
zero-inf mỗi epoch.

## Cài đặt

```bash
pip install -r requirements.txt            # torch, numpy, Pillow, opencv, pandas,
                                           # onnxruntime, flask, PyYAML
pip install -r requirements-optional.txt   # matplotlib, tqdm, onnx, onnxsim
```

`requirements.txt` **cố ý không pin wheel CUDA**: máy local là `torch +cpu`, pin
`+cu121` sẽ phá môi trường. Trên Colab/Kaggle cài torch bản CUDA riêng.

## Dùng

```bash
# 0. Cổng sanity — BẮT BUỘC chạy trước mọi lần train
python -m captcha.inspect_data --report --write-config configs/groups.yaml
python -m captcha.inspect_data --dump-tail-samples out/tail --per-group 12

# rồi "mắt người phán" các mẫu đuôi: ký tự hiếm có thật sự khác ký tự lõi không?
python -m tools.contact_sheet out/tail --prefix g_80x30 -o out/sheet.png
python -m tools.contact_sheet out/tail --all -o out/sheets/

# 1. Train (một nhóm, hoặc --all)
python -m captcha.train --group g_80x30
python -m captcha.train --all --epochs 60

# smoke test nhanh trên CPU
python -m captcha.train --group g_80x30 --limit 256 --epochs 2 \
    --batch-size 32 --num-workers 0 --device cpu

# 2. Đánh giá: exact-match, per-char, CER, bảng confusion, 30 mẫu tệ nhất
python -m captcha.evaluate --group g_80x30 --split test

# 3. Predict
python -m captcha.predict --image pass/000000_D1XPP.png
python -m captcha.predict --dir out/val --labels-from-filename --csv-out out/pred.csv
python -m captcha.predict --backend onnx --image pass/001046_0005.png

# 4. Export ONNX (kèm parity check torch ↔ onnxruntime)
python -m captcha.export_onnx --all

# 5. Web demo
python app.py --backend onnx
```

Chạy test:

```bash
python -m pytest tests/ -q
python tests/test_config.py           # routing 14/14, charset, guard nhóm merge
python tests/test_train_selection.py  # chọn checkpoint / early-stop qua plateau
python tests/test_overfit.py          # cổng Phase 2: phải overfit được 16 mẫu
```

## Thiết kế

### Preprocessing — đúng thứ tự này, dùng chung cho train và inference

`composite alpha → grayscale → polarity → CLAHE (tuỳ chọn) → stretch percentile
p2/p98 → resize H=32 → normalize [-1,1]`

- **Composite RGBA lên nền trắng** bằng `alpha_composite`. Không bao giờ
  `convert("RGB")` thẳng trên ảnh trong suốt — sẽ tạo viền đen quanh từng ký tự.
- **Polarity** theo median viền 2px: `< 128` thì đảo. Viền gần như chắc chắn là nền,
  và nó tách sạch 14 nhóm (tối 0–87 vs sáng 149–255) nơi Otsu toàn cục không tách được.
- **Stretch percentile** chứ không min/max: các style có hạt nhiễu rời rạc sẽ phá
  min/max stretch.
- **`INTER_AREA`** khi giảm > 2× (`1500×350` giảm 10.9×, bilinear sẽ làm nhoè nét mảnh).
- **Không pad** ở đường per-group: trong một nhóm mọi ảnh cùng kích thước nên sau
  resize cùng width → batch cùng shape → mở được cuDNN CTC fast path.

Flask, CLI, eval và train gọi **cùng một** `preprocess` — không có train/serve skew.
Không dùng torchvision vì lý do này.

### Model

```
(B,1,32,W) → Conv 1→64  ×2 + BN + ReLU + MaxPool(2,2)   → (B,64,16,W/2)
           → Conv 64→128 ×2 + BN + ReLU + MaxPool(2,2)  → (B,128,8,W/4)
           → Conv 128→256 ×2 + BN + ReLU + MaxPool(2,1) → (B,256,4,W/4)   ← /4
           → permute(3,0,1,2).flatten(2) → (T,B,1024)
           → Linear(1024,256) → BiLSTM(256→256, 2 layer)
           → Linear(256,num_classes) → LogSoftmax(dim=2)
```

- `blank = 0`, charset đánh số từ 1, `num_classes = len(charset)+1`.
- `LogSoftmax` nằm **trong** module → ONNX xuất thẳng log-probs, chỉ có một định
  nghĩa decode contract.
- `forward` **assert** `log_probs.shape[0] == conv_out_width(W)` mỗi batch — bắt lỗi
  phổ biến nhất của CRNN+CTC (sai `input_lengths`).

### Augmentation — chỉ những phép CTC-safe

Affine nhẹ (xoay ±3°, shear ±0.05, scale 0.95–1.05, dịch ±2%, `BORDER_REPLICATE`),
stretch width ±5%, gain/bias, Gaussian noise, blur nhẹ, **vẽ thêm 1–2 đường kẻ mảnh
p=0.15** (khoảng nửa số nhóm có đường kẻ cắt ngang — đây là augmentation giá trị
nhất), cutout nhỏ p=0.2.

**Bị cấm và có assert chặn** (`assert_ctc_safe`):

- **Lật ngang** — vừa soi gương ký tự vừa đảo thứ tự trái→phải mà CTC giả định.
- Lật dọc, xoay > 7°, shear lớn — phá tính đơn điệu trái→phải.
- `RandomResizedCrop` — cắt mất ký tự nhưng nhãn vẫn khai báo → hỏng nhãn âm thầm.
- Color jitter (vô nghĩa sau grayscale), đảo polarity ngẫu nhiên (xung đột bước
  polarity), Mixup/CutMix (không hợp nhãn chuỗi).

### Train

- `AdamW(lr=1e-3, weight_decay=1e-4)`, `clip_grad_norm_(5.0)` trước mỗi step
  (gradient CTC hay vọt).
- **`ReduceLROnPlateau` giám sát trên val _loss_, không phải val exact-match.**
  Model CTC những epoch đầu đoán toàn blank, nên EM **đồng nhất bằng 0.0** suốt
  plateau: giám sát EM nghĩa là scheduler chặt LR đúng lúc model cần dư địa nhất.
  Trên `g_80x30` lịch cũ hạ LR xuống 5e-4 ngay tại epoch 4 — giữa plateau, khi loss
  vẫn đang giảm (10.74 → 9.90) và model chưa hề phát ra ký tự nào. Loss là tín hiệu
  duy nhất phân biệt được các epoch plateau.
- **`best.pt` chọn theo EM, hoà thì lấy loss thấp hơn** (`_improved` trong
  `train.py`). Chọn thuần theo EM sẽ ghim `best.pt` vào epoch đầu tiên của plateau
  — tức là một model blank — và `evaluate`/`predict` sẽ load đúng cái model vô
  dụng đó. Early stopping dùng cùng phép so này, nên "EM vẫn 0 nhưng loss đang
  giảm" **không** bị tính là không tiến bộ.
- **AMP gate cứng `device.type == "cuda"`**, không cho override. `autocast("cpu",
  bfloat16)` là bẫy: chậm hơn và hỏng số học CTCLoss.
- **CTCLoss luôn tính ở fp32 ngoài autocast** — fp16 tích luỹ log-prob âm lớn →
  `inf`/`NaN`.
- `zero_infinity=True` là **lưới an toàn, không phải chiến lược**: nó biến "nhãn dài
  hơn T" thành mẫu gradient 0 im lặng. Chặn bằng assert lúc dựng dataset + đếm
  zero-inf mỗi epoch, cảnh báo nếu > 0.1%.
- Split 80/10/10 **theo nhóm nội dung md5** và stratified theo độ dài nhãn — 765 file
  trùng (2.9%) không thể rò rỉ từ train sang val/test.
- Checkpoint nhúng `charset/width/height/group_id/config_hash` + RNG state;
  `predict` **từ chối load** checkpoint có charset lệch config.
- `history.csv` ghi mỗi epoch → Colab disconnect không mất curve.
- **`--limit` lấy mẫu ngẫu nhiên có seed, không cắt đầu danh sách.** `split_records`
  xếp bucket theo độ dài nhãn tăng dần nên danh sách train bị sắp theo độ dài chứ
  không được xáo: cắt đầu sẽ lấy hết nhãn ngắn trước. Đo được trên `g_80x30`: cả 10
  mẫu dài-3 nằm trong 250 mẫu đầu /1562, còn mẫu dài-5 duy nhất thì nằm ngoài. Với
  `g_250x40` (dài 3–9) thì `--limit 250` chỉ phủ 3/7 độ dài và **không có nhãn nào
  dài quá 5** — tức là train trên một phân bố khác hẳn bản full. Vì mọi cổng smoke
  đều dùng `--limit`, lỗi này sẽ âm thầm làm cổng đo sai thứ.
- **Cổng đo phải cố định *số step*, không cố định *số epoch*.** Batch 64: điểm 250
  ảnh chạy 4 step/epoch, điểm 1562 chạy 25. Giữ 25 epoch cho mọi điểm tặng điểm nhỏ
  nhất 100 step và điểm lớn nhất 625 — **chênh 6×**, và đường cong đo *lượng train*
  chứ không đo *lượng dữ liệu*. Đo được: lần chạy learning curve đầu tiên để `n=250`
  kết thúc ở train loss 8.65 / val char 1.8%, vẫn nằm sâu trong plateau blank của
  CTC và **chưa từng phát ra ký tự nào**; đổi sang 300 step cho mọi điểm thì cùng 250
  ảnh đó đạt **92.31%** test exact-match. Cùng loại lỗi với `--limit`: cổng đo trông
  chạy được nhưng đo sai thứ. `out/learning_curve.py` in số step của từng điểm và ghi
  vào `curve.json` để con số tự khai nó được train bao nhiêu.

### ONNX

Export **CNN + BiLSTM + Linear + LogSoftmax** (kết thúc ở log-probs `(T,B,C)`),
**bỏ CTCLoss** (ONNX hỗ trợ kém và không cần), greedy decode chạy numpy.

- **Static shape mặc định** — mọi ảnh trong nhóm cùng kích thước nên production
  không cần width động, và static né được toàn bộ vấn đề dynamic-shape của LSTM.
- `dynamo=False`: dynamo exporter của torch 2.x chặt hơn và fail nhiều hơn với LSTM.
- **Parity check bắt buộc trong chính exporter**: chạy cùng batch qua torch và
  `onnxruntime`, `np.allclose(atol=1e-4)`, fail loudly kèm max abs diff.
- Contract: ONNX nhận **tensor float đã normalize [-1,1]**, không nhận ảnh uint8.

### Routing lúc inference

Khớp size chính xác → model của nhóm. **Size lạ → fallback** chấm điểm
`|log(W/H) − log(w/h)| + 0.5·|log H − log h|`, lấy min, resize về shape canonical.
Kết quả mang cờ `fallback`, bị trừ confidence, và size chưa khớp được log lại để
quyết định thêm nhóm. `--strict` từ chối fallback hoàn toàn.

## Kết quả mong đợi

Exact-match sau hội tụ, **có trọng số ~90–95%**, per-character **~99%+**:

| Nhóm | Kỳ vọng | Lý do |
|---|---|---|
| `g_80x30`, `g_120x38` | 96–99% | Sạch nhất, 10 lớp |
| `g_300x75`, `g_150x34` | 95–98% | drop ≤ 1.9% |
| `g_180x64` | 93–98% | Lớn nhất, 36 lớp, drop 0.4% |
| `g_194x35`, `g_1500x350` | 90–96% | drop 5.2–6.7% chặn trần |
| `g_150x39`, `g_160x75` | 85–94% | 995 mẫu / tương phản thấp (T=17 sát) |
| `g_180x50`, `g_150x30` | 80–92% | Nền tối, 43 lớp |
| `g_600x120` | 75–90% | drop 15.2%, nền tối, giảm 3.75× |
| `g_250x40` | 65–85% | drop 17.4%, maxL=10, 1000 mẫu, **nhiều style** |

`g_600x120` và `g_250x40` phải đọc kèm **bảng confusion**, không đọc con số thô.

## Cấu trúc

```
captcha/
  config.py        # dataclass + YAML, routing, config_hash
  charset.py       # suy ra lõi/đuôi, policy core|full|map, encode/decode
  transforms.py    # load ảnh, polarity, stretch, resize, augment (CTC-safe)
  dataset.py       # index md5, split theo content-hash, Dataset, collate, sampler
  model.py         # CRNN + conv_out_width()
  ctc.py           # greedy decode, input_lengths, assert T >= 2L+1, confidence
  train.py         # loop, AMP guard, clip, sched, early stop, ckpt/resume
  evaluate.py      # exact-match, per-char, CER, confusion, worst-N
  predict.py       # routing + fallback, backend torch/onnx
  export_onnx.py   # export + parity check
  inspect_data.py  # report + dump mẫu đuôi + sinh groups.yaml
  plotting.py      # loss/acc curve, matplotlib lazy
  utils.py         # seed, device, log, history CSV, checkpoint IO
app.py             # Flask demo
configs/groups.yaml
runs/<group>/{best.pt,last.pt,history.csv,model.onnx,plots/}
tests/test_overfit.py
colab_train.ipynb
```

## Colab / Kaggle

Xem `colab_train.ipynb` (train 14 nhóm) và `colab_learning_curve.ipynb` (learning
curve `g_80x30`). Ba điểm quan trọng:

1. **Zip dataset tại nguồn.** Copy 26k file nhỏ qua Drive mount chậm kinh khủng
   (hàng phút đến hàng giờ); một `pass.zip` ~200 MB mất ~30 s. Upload private Kaggle
   dataset rồi `unzip` ra disk instance. **Không bao giờ train trực tiếp trên Drive
   mount.** Trên Kaggle Notebooks thì attach dataset luôn (zero copy).
2. **Loop 14 nhóm độc lập, copy `runs/` lên Drive sau mỗi nhóm** — disconnect không
   mất quá 1 nhóm. Loop tự resume vì nhóm nào có `best.pt` thì bị skip.
3. T4 batch 128: nhóm lớn ~1–3 phút/epoch. 14 nhóm × 40–60 epoch với early-stop vừa
   một session 12 h.

## Giới hạn đã biết

- **Nhiễu nhãn** (~1–2% ký tự) chặn trần exact-match. Đây là thuộc tính của dữ liệu.
- **3 nhóm drop > 10%** (`g_600x120`, `g_250x40`, `g_150x30`) có đuôi quá lớn và quá
  có cấu trúc để là nhiễu thuần. Tooling đánh cờ `review: true` và **không tự quyết**:
  đổi `charset_mode: full` trong `groups.yaml` rồi so sánh, hoặc xem
  `--dump-tail-samples`. Riêng `g_250x40` nhiều khả năng cần tách theo style.
- **Gộp nhóm (`merge`) mới chỉ được hỗ trợ ở tầng dựng batch.** `dataset.py` bucket
  theo width chính xác nên batch không cần pad; nhưng kiểm tra `T ≥ 2L+1`, đường
  inference và ONNX static-shape đều giả định **một** width canonical cho mỗi nhóm.
  Vì vậy `train`/`predict`/`evaluate` **từ chối** nhóm merge bằng
  `GroupCfg.require_unmerged()` thay vì bóp mọi size về width của size đầu rồi cho
  ra kết quả sai âm thầm. Muốn gộp 5 nhóm số thì phải làm nốt phần đó trước.
- **`g_1500x350` charset có `O` và `o`**, `g_194x35` có `z` — ở tần suất trên ngưỡng
  0.5% nên được giữ làm lớp. Nếu thực ra chúng là `0`/`2` bị gán sai thì nên dùng
  `charset_mode: map`.
- 3 file RGBA hiện đều opaque nhưng pipeline vẫn xử lý đúng nếu gặp ảnh trong suốt.
- `g_180x64` có 7,236 mẫu (28% dữ liệu) nên accuracy tổng có trọng số bị nhóm này
  chi phối.
