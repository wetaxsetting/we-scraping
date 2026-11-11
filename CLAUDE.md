# CLAUDE.md

Project gồm 2 phần, mô tả riêng bên dưới:

- **PHẦN 1 – Train captcha:** model CRNN đọc captcha, mỗi nhóm kích thước ảnh một model.
- **PHẦN 2 – Xử lý hóa đơn (XML → PDF):** đăng nhập hoadondientu.gdt.gov.vn bằng model captcha
  → tải XML → parse theo QĐ 1233 → lưu PostgreSQL → lấy PDF bản gốc (site tra cứu NCC / mail).

PHẦN 3 (cuối file) là đặc tả sản phẩm **WeInvoice** của PO (UI Spec v1.0): sản phẩm dùng dữ liệu của PHẦN 2,
P1 demo 23/12/2026.

## Chung

- Môi trường: Windows, PowerShell 5.1, Python 3.13, CPU-only, RAM ít.
  Luôn chạy `python -X utf8 ...`, vì thiếu cờ này thì console Windows hỏng tiếng Việt.
- `example/` **chỉ để tham khảo**, không sửa. Mọi source nằm ở thư mục gốc, `captcha/`, `web/`, `pdf_providers/`.
- Bí mật chỉ nằm trong `.env`. **Không in nội dung `.env`, không chép mật khẩu vào `.env.example`
  hay bất kỳ file nào khác.**
- Job dài trên máy này hay bị ngắt: chạy foreground (hoặc train trên Colab). Job đã bị kill thì không
  tự chạy lại khi user chưa yêu cầu.
- Repo GitHub (`wetaxsetting/we-scraping`) không chứa `env/`, `env_pass/`, `output/`, `.env`, `example/`.
  `runs/` chỉ đưa `best.pt` (Git LFS) + `history.csv` của nhóm đang dùng thật: `g_600x120`, `g_150x34`,
  `g_80x30`, `g_1500x350`, `g_300x75`, `g_200x70`, và từ 27/10 `g_120x38`, `g_194x35`, `g_120x40_cloud`, từ 02/11 `g_180x50_vn`. Adapter mới dùng nhóm khác → thêm `!runs/<nhóm>/` vào `.gitignore`.
  Máy mới: cài Git LFS trước khi clone, nếu không `best.pt` chỉ là file trỏ vài trăm byte và không nạp được.
- Báo cáo tuần: `report/Bao-cao-tuan-<tuần>-<năm>.docx` + bản tiếng Anh `report/Weekly-report-W<tuần>-<năm>.docx`
  (cùng nội dung, sửa bản nào thì sửa cả hai). Số liệu lấy từ `invoice_job.py status`,
  DB (`invoice_pdf.source_id`) và `runs/<nhóm>/history.csv` / `evaluate`, không ước đoán.

---

# PHẦN 1 – TRAIN CAPTCHA

Chi tiết thiết kế (preprocessing, model, augmentation, ONNX) xem [README.md](README.md).

## 1.1 Cấu trúc

| Thành phần                   | Vai trò                                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| `env/<nhóm>/<số>_<nhãn>.png` | Dữ liệu train. **Nhãn nằm trong tên file**, nên sửa nhãn = đổi tên file                     |
| `configs/groups.yaml`        | Mỗi nhóm: charset, kích thước đưa vào model, ghi chú. Sinh bằng `inspect_data`, rồi sửa tay |
| `runs/<nhóm>/best.pt`        | Checkpoint tốt nhất. `meta` lưu `charset` + `config_hash` của lúc train                     |
| `runs/<nhóm>/history.csv`    | Từng epoch: `train_loss, val_loss, val_em` (đúng cả chuỗi), `val_char_acc`, `sec`           |
| `captcha/`                   | `train`, `evaluate`, `predict`, `export_onnx`, `inspect_data`, `config`, `dataset`, `model` |
| `gdt_captcha_solver.py`      | Captcha GDT: SVG 200x40 → render PNG ×3 (600x120) → nhóm `g_600x120`                        |
| `pdf_providers/common.py`    | `solve_captcha(img, group_id, min_conf)` cho các site tra cứu NCC                           |

## 1.2 Lệnh

```powershell
python -X utf8 -m captcha.inspect_data --report --write-config      # quét env/, sinh configs/groups.yaml
python -X utf8 -m captcha.train --group <nhóm>                      # train 1 nhóm (xem README: --all, smoke test)
python -X utf8 -m captcha.predict --dir <thư mục> --labels-from-filename   # đo nhanh trên ~200 mẫu có nhãn
python -X utf8 -m captcha.evaluate --group <nhóm> --split test --device cpu --num-workers 0 --data-dir env/<nhóm>
# export_onnx: xem README
```

## 1.3 Những điểm dễ sai

- **Đổi `charset`/config sau khi train thì phải train lại.** Predictor từ chối nạp checkpoint có charset
  khác config. Ví dụ `g_120x40_vnpt`: bản train 09/10 bị lệch charset nên không nạp được; đã train lại 14/10
  với config hiện tại (xem 1.4).
- **`evaluate`/`train` đọc PNG phẳng trong `--data-dir`, không đọc thư mục con.** Trên máy này ảnh nằm ở
  `env/<nhóm>/` (config còn ghi `data_dir: pass`) → luôn truyền `--data-dir env/<nhóm>`. Nhóm trùng cỡ ảnh
  (vd `g_120x40_vnpt` và `g_120x40_cloud` đều 120x40) bắt buộc phải trỏ đúng thư mục của nhóm.
- **Mẫu cho mỗi ký tự quyết định nhóm có học được hay không**, không phải số ảnh hay độ sạch của nhãn.
  Tính `sum(len(nhãn)) / len(charset)` trên tập train. Dưới khoảng **250 mẫu/ký tự** thì chưa nhóm nào
  thoát được plateau CTC (val_em = 0); khi đó câu trả lời đúng là "thu thêm ảnh".
- **Khi đã trên ngưỡng đó, nhãn sai mới là giới hạn.** Ví dụ `g_1500x350` tăng 70% → 96% chỉ nhờ sửa nhãn,
  không thêm ảnh nào. Model đọc ra kết quả khác nhãn thì kiểm tra nhãn trước (vd `1`/`I`/`l`, `0`/`O`).
- **Luôn ép đúng nhóm khi đọc (`enforce_group`).** Router chọn nhóm theo kích thước ảnh. Gặp captcha lạ
  trùng kích thước, nó vẫn đọc sai mà báo độ tin cao, không có cảnh báo. Nguồn captcha mới phải có nhóm
  riêng với khoảng 500–2.000 mẫu có nhãn.
- **`Path(a) == Path(b)` trên Windows KHÔNG phân biệt hoa/thường.** `pathlib` so sánh case-insensitive
  trên Windows, nên `Path('x_aB.png') == Path('x_Ab.png')` là `True`. Đổi nhãn captcha hầu hết là đổi
  mỗi case, nên bất kỳ chỗ nào viết `if src == dst: return` sẽ **bỏ qua im lặng đúng những file cần
  đổi** mà vẫn báo thành công (bug này làm `relabel_case` 20/10 báo "renamed 6411 files" trong khi
  không file nào đổi). So bằng **chuỗi tên**: `src.name == dst.name`. Cùng họ với bẫy
  `os.path.exists('FILE.png')` trả `True` khi đang có `file.png` (đã dính hồi sửa nhãn `g_70x23`).
  **Kiểm rename phải đối chiếu `os.listdir` trước/sau, đừng tin log** — bài test "apply rồi undo thấy
  tên khớp bản gốc" là vô nghĩa khi cả hai chiều đều không làm gì.
- **Cache index không nhận ra file bị đổi tên.** Chữ ký cache chỉ gồm số file, tổng dung lượng và mtime mới
  nhất, nên sửa nhãn xong cache vẫn "hợp lệ". Số lượng tập train nghi ngờ → quét lại với cache trống.
- **Checkpoint train trước 19/10 có thể ghi sai `meta.charset`** (đã sửa `train.py` 19/10): bản cũ lưu
  charset **trong config** thay vì charset **core đã suy từ data**. Với `charset_mode: core`, hai cái này
  khác nhau khi data có ký tự đuôi hiếm → model encode theo core nhưng decode theo config, **mọi ký tự
  lệch index** (vd `a→8`, `h→g`, `V→U`), test tụt về 0–10% dù model train tốt.
  Dấu hiệu: `meta.note` ghi "core alphabet (N chars)" nhưng `len(meta.charset) != N`, và
  `fc.weight` có `N+1` hàng không khớp. Checkpoint 4 nhóm `g_600x120`, `g_200x70`, `g_180x50_vn`,
  `g_180x50_tax24` đã vá lại meta 19/10 (bản gốc giữ ở `runs/<nhóm>/*.pt.bak`).
  Gặp `evaluate` báo charset mismatch → kiểm `meta.note` trước, **đừng vội đổi config sang `fixed`**:
  làm vậy model nạp được nhưng đọc sai toàn bộ.
- **`charset` trong config ≠ charset model được train** (sửa `predict.py` + `export_onnx.py` 20/10).
  Với `charset_mode: core`, charset thật là phần lõi suy từ data, là **tập con** của charset config.
  `train`/`evaluate` dùng bản suy ra, nhưng `predict`/`export_onnx` trước đây so khớp **cứng** với
  `group.charset` → **từ chối nạp** mọi nhóm core có cắt đuôi (`g_200x70`, `g_180x50_tax24`,
  `g_180x50_vn` đều không dùng thật được), và `predict` còn giải mã bằng charset config nên kể cả
  qua được cũng lệch index. Nay `meta["charset"]` của checkpoint là bảng chữ giải mã chính thức;
  config chỉ dùng để định tuyến + làm chặn trên (train ⊆ config, ngoài tập đó mới báo lỗi).
  Sidecar `model.onnx.meta.json` cũng ghi charset đã train, không phải charset config.
- **`captcha-botdetect-gen` bỏ qua `charset` trong preset** (phát hiện 20/10, chưa sửa).
  `GroupPreset.Charset` chỉ được in ra ở lệnh `list`, **không bao giờ truyền vào BotDetect**
  (`GenerateGroup` chỉ set `CodeLength`, `ImageStyle`, `ImageSize`, `ImageFormat`). Nên ảnh sinh ra
  luôn dùng bảng chữ mặc định của BotDetect = **HOA + số**, bất kể preset ghi gì. Sinh cho nhóm
  chữ thường / hỗn hợp hoa-thường là ra data sai hoàn toàn. Muốn đổi phải dùng
  `CustomCharacterSetName` (BotDetect không có thuộc tính charset trực tiếp — soi bằng reflection:
  `CodeStyle` chỉ có `Alphanumeric/Alpha/Numeric`, không có công tắc hoa/thường; `AutoUppercase`/
  `AutoLowercase` là chuẩn hóa ô nhập, tức BotDetect vốn kiểm case-insensitive).
  Exe trong `bin/` có thể cũ hơn `Program.cs` — build lại bằng MSBuild của VS 2022 trước khi dùng.
- **Generator BotDetect có thể để lại PNG 0 byte** (gặp 26/10 ở `g_250x40/093767_UTWED.png`, 1/106.773 ảnh).
  `Program.cs` gọi `Bitmap.Save(filePath)`: GDI+ tạo file trước, lỗi giữa chừng thì file rỗng nằm lại, còn
  exception bị `catch` nuốt thành `errors++`. Train sẽ chết ở `scan_index` với `UnidentifiedImageError`.
  Sau mỗi đợt sinh: `Get-ChildItem env\<nhóm> -Filter *.png -File | Where-Object Length -eq 0 | Remove-Item`.
- **Thư viện BotDetect không giải được captcha của site dùng nó** (hỏi 26/10 cho VDSG/`g_250x40`).
  `CaptchaBase` chỉ có `GetCode` / `GetImage` / `GetSound` / `Validate`; không có hàm ảnh → chữ.
  Mã captcha sinh và lưu trong session **phía server của site**, nên cầm DLL ở máy mình không đọc được.
  Thư viện chỉ giúp được một việc: sinh dữ liệu train cùng bộ render. Giải vẫn phải bằng OCR (model / ddddocr).
- **Trước khi sinh data, kiểm nguồn data cũ của nhóm đó bằng vân tay PNG** (mode + chunks + cỡ file):
  ảnh cào từ site, ảnh BotDetect và ảnh lưu lại qua PIL khác nhau rõ. Nhầm nguồn dẫn tới chẩn đoán sai
  (xem `g_180x50_vn` ở 1.5).
- Báo kết quả theo **test**, không theo val: hai số có thể lệch vài phần trăm. Bảng 1.4 là val_em
  (từ history.csv); muốn số chính xác thì chạy evaluate trên tập test. **Test chỉ đáng tin khi nhãn
  đúng** — nhãn tập test dính cùng lỗi với tập train thì số test vô nghĩa (`g_180x50_vn`: test 62,32%
  nhưng thật ra 0/8 trên captcha thật). Nhóm nào lấy captcha được từ site thì đo thêm trên ảnh thật.
- **Thời gian train** (đo từ history): `g_600x120` bản 17/10 ~39,6 giờ (54 epoch, 13.757 ảnh),
  `g_200x70` ~43,2 giờ (113 epoch), `g_180x50_vn` ~24,5 giờ (52 epoch), `g_180x50_tax24` ~3 giờ
  (53 epoch, 1.000 ảnh); `g_160x60` ~38 giờ, `g_120x40_vnpt` bản 14/10 ~30 giờ; bản 29–30/10 (máy khác):
  `g_180x50_vn` ~50,7 giờ (92 epoch), `g_250x40` ~52,8 giờ chỉ 6 epoch (106.772 ảnh), `g_70x23` ~12,1 giờ,
  `g_200x80` ~9,5 giờ (100 epoch), `g_150x39` bản 31/10 ~67,8 giờ (120 epoch, 12.873 ảnh). Nhóm lớn nên train
  trên GPU/Colab. Train xong mang sang: chép **đúng 2 file** `runs/<nhóm>/best.pt` + `history.csv`,
  rồi chạy `evaluate --split test` để xác nhận. **`env/<nhóm>` ở máy này phải y hệt data lúc train**,
  vì `core` suy charset từ data — thêm/bớt ảnh xong là charset đổi và checkpoint hết nạp được.
- Cột "Train cho AI(%)" (cột K, sheet "Chi tiết" trong `docs/Danh_sach_NCC.xlsx`) **đã đồng bộ 20/10** theo
  val_em tốt nhất trong `runs/<nhóm>/history.csv` (riêng `g_200x70` ghi test 89,24% của 17/10, xem 1.4); trước đó
  lệch nhiều (vd `g_160x60` ghi 0% nhưng thật 99,75%; `g_150x39` ghi 84,85%, `g_160x75` 74% nhưng history
  chỉ 2,3% / 0%). Cột L đổi `g_120x40` → `g_120x40_vnpt` (VNPT) / `g_120x40_cloud` (Nhóm Mây). Chưa có thư
  mục `runs/` nên giữ nguyên: My-Invoice `g_180x45` (94,47%), VNISC `g_150x50_1` (trống). Lạc Việt
  sửa 04/11 thành `g_100x36` / 98,72% (test). Train xong nhóm nào thì cập nhật lại cột K từ history.csv.

## 1.4 Tình trạng các nhóm (val_em tốt nhất, 19/10/2026)

| Nhóm                                          | Dùng cho                                                                                                   | val_em | Tình trạng                                                                                                                                                                                                                                                                                                                                                                                                                   |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `g_600x120`                                   | Đăng nhập GDT                                                                                              | 89,0%  | **Đang dùng thật.** Train lại 16–17/10 với 13.757 ảnh (cũ 1.784): test 87,30% (1.370 ảnh), char 97,79%. Nhầm chính `X↔K`, `Y→K`, `P↔R`                                                                                                                                                                                                                                                                                       |
| `g_180x50_tax24`                              | VN-Invoice, TAX24…                                                                                         | 100%   | **Train 15/10, tốt nhất hiện có**: test 98,99% (99 ảnh), char 99,80%. n=1.000. Đổi tên từ `g_180x50_lower`                                                                                                                                                                                                                                                                                                                   |
| `g_200x70`                                    | Thái Sơn (einvoice.vn)                                                                                     | 89,2%  | **Học được sau khi sửa nhãn** (upper hết + bỏ nhãn có chữ số): test 89,24% (1.125 ảnh), char 97,24%. n=11.311. Lỗi còn lại chủ yếu là sai độ dài (`4→3`). **20/10 đang train lại với height 48** (sửa lỗi CTC nuốt ký tự lặp `LWWD→LWD`, xem note trong groups.yaml); bản height 32 (89,24%, meta đã vá) giữ ở `runs/g_200x70/h32_89pct/` — không nạp được với config hiện tại; `best.pt` ngoài cùng là lượt h48 mới 1 epoch |
| `g_180x50_vn`                                 | vninvoice.vn (`tracuu.v50.vninvoice.vn`; VN-Invoice MST 0101352495 / "VNISC" dòng 46 Excel MST 0103018807) | 91,4%  | **Train lại 29/10 trên nhãn đã sửa case** (máy khác, 92 epoch ~50,7 giờ, bảng chữ 56): test **90,63%** (1.163 ảnh), char 98,41%. Lỗi chính giờ là **sai độ dài `6→5` (86/109 lỗi, nuốt ký tự lặp `kkDPY2→kDpY2`)**, nhầm case còn lẻ tẻ (`P→p`, `j→J`). **Đo trên site 02/11: server nhận 28/30 (93%)**, 2 lần trượt đều là đọc ra 5 ký tự (nuốt ký tự lặp); 28/28 lần đọc đủ 6 ký tự đều đúng → adapter chỉ cần bỏ đáp án ≠ 6 ký tự và xin ảnh mới. Cách đo: `GET .../PortalApiService/captcha` → `POST .../portal-public` với mã bịa, server kiểm captcha trước (sai → "Mã xác thực không đúng", đúng → "Từ ngày không được để trống"). **Đang dùng** trong adapter `vninvoice` (02/11): 15/15 HĐ thật lấy được PDF + XML |
| `g_120x40_vnpt`                               | VNPT cổng HST chung (captcha API `get-captcha?width=120&height=40`)                                        | 98,3%  | **Train lại 14/10, dùng được**: test 98,59% (1.206 ảnh), char 99,32%; captcha thật 6/6. Chưa adapter nào cần (HST không kiểm captcha phía server)                                                                                                                                                                                                                                                                            |
| `g_120x38`                                    | Megabiz (vinvoice)                                                                                         | 100%   | OK                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `g_160x60`                                    | WEBCASH (wetax)                                                                                            | 99,75% | OK (server nhận 6/6)                                                                                                                                                                                                                                                                                                                                                                                                         |
| `g_80x30`                                     | VNPT cổng riêng (Petrolimex)                                                                               | 98,0%  | Đang dùng trong adapter `vnpt`                                                                                                                                                                                                                                                                                                                                                                                               |
| `g_300x75`                                    | CyberLotus / Liên Sơn / My-Invoice (xcyber)                                                                | 97,7%  | OK                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `g_150x34`                                    | EasyInvoice                                                                                                | 96,0%  | Đang dùng trong adapter `easyinvoice`                                                                                                                                                                                                                                                                                                                                                                                        |
| `g_1500x350`                                  | Visnam                                                                                                     | 96,0%  | Đang dùng trong adapter `visnam` (thật 11/11)                                                                                                                                                                                                                                                                                                                                                                                |
| `g_120x40_cloud`                              | Cloudteam                                                                                                  | 94,7%  | OK                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `g_194x35`                                    | PVOIL                                                                                                      | 91,8%  | OK                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `g_180x64`                                    | —                                                                                                          | 75,2%  | Cần thêm dữ liệu                                                                                                                                                                                                                                                                                                                                                                                                             |
| `g_150x39`                                    | SmartSign (adapter đang dùng ddddocr, xem 2.6)                                                             | 81,0%  | **Train lại 31/10** trên nhãn sửa tay 26/10 (120 epoch ~67,8 giờ, bảng chữ 33): test **81,36%** (1.277 ảnh), char 94,59%. Lỗi chính `4→3` (76 ảnh, nuốt ký tự lặp `uu5u→u`, `77pe→7e`), `m→n`, `l→i`. **Thấp hơn ddddocr (server nhận 54/60 = 90%) → adapter giữ ddddocr.** Còn ~11.200 nhãn 4 ký tự chưa soi. Xem 1.5 mục 7 |
| `g_250x40`                                    | VDSG                                                                                                       | 93,2%  | **Train 30/10** (máy khác, chỉ 6 epoch nhưng ~52,8 giờ, 106.772 ảnh **BotDetect sinh**): test **93,32%** (10.677 ảnh), char 98,96%. Chưa đo trên captcha thật của site; cổng VDSG phải đăng nhập. Thư viện BotDetect **không giải được captcha**, xem 1.3 |
| `g_70x23`                                     | Bitware                                                                                                    | 84,7%  | **Train 30/10** (100 epoch ~12,1 giờ, 3.000 ảnh, bảng chữ 26 chữ HOA): test **83,67%** (300 ảnh), char 95,42%. Lỗi chính `4→3` (nuốt ký tự lặp `ZZRH→ZH`, `UUKO→UKQ`), `I→J`. Chưa có HĐ thật |
| `g_200x80`                                    | Ngô Gia Phát                                                                                               | 15,2%  | **Train 29/10** (100 epoch ~9,5 giờ, 2.625 ảnh, bảng chữ 30): test **74,05%** (262 ảnh), char 92,75%, nhưng val_em tốt nhất chỉ 15,2% — **lệch bất thường, chưa rõ nguyên nhân, kiểm lại trước khi dùng**. Đo lại 31/10 trên máy này: val 70,99% (val_loss lúc train 5,4 vs train_loss 1,3 = overfit), cùng 2.625 ảnh, không file nào đổi sau 28/10 → nghi chia tập ở đây khác lúc train, val/test lẫn ảnh train; số thật có thể ~15%. Phải đo trên captcha thật hoặc train lại trên máy này. Lỗi chính `5→4`. Chưa có HĐ thật |
| `g_150x30`                                    | ASIA (asiainvoice.vn)                                                                                      | —      | **Không train — tra bảng** (29/10). Cổng phát lại từ 1 **kho captcha cố định**: 7.561 file trong env chỉ là 823 ảnh khác nhau (trung bình mỗi ảnh lưu ~9 lần). Đã dọn: giữ 818 ảnh (bản sao → `env/g_150x30_dups/`, 5 ảnh không đọc chắc → `env/g_150x30_review/`, nhật ký `env/g_150x30/dedup_20261029.csv`), sửa tay 31 ảnh có nhãn mâu thuẫn. 818 ảnh ≈ 157 mẫu/ký tự, dưới ngưỡng train, nên dùng bảng `pdf_providers/data/asia_captcha.npz` (`scripts/build_asia_captcha_table.py`). Cổng thật 29/10: 55/60 captcha có trong bảng, bảng trả lời đúng 35/36 lần gửi |
| `g_100x36`                                    | Lạc Việt (accnet.vn, 0301448733)                                                                           | 98,7%  | **Train 02/11** (60 epoch, 10.959 ảnh, bảng chữ 28 HOA + số, không `0 1 5 I O Q S Z`; tên cũ `g_150x32` — ảnh thật 100x36, đổi tên 31/10): test **98,72%** (1.096 ảnh), char 99,66%. Lỗi chính sai độ dài `4→3` (8 ảnh, nuốt ký tự lặp `WVVT→WVT`). Chưa có adapter, chưa đo trên captcha thật |
| `g_150x38`                                    | Dịch vụ công Thuế (captcha đăng nhập `dichvucong.gdt.gov.vn/tthc/login/getCaptcha`)                        | 83,3%  | **Train 03/11** (máy khác, 60 epoch ~24,2 giờ, 10.999 ảnh, bảng chữ 34 `0–9` + `a–z` trừ `j l`): test **79,25%** (1.099 ảnh), char 95,43%. Lỗi chính sai độ dài `5→4` (84 ảnh, nuốt ký tự lặp `yy9pi→y9opi`) + `5→6` (14) → dùng thì bỏ đáp án ≠ 5 ký tự (đọc đủ 5 ký tự đúng ~87%); nhầm `o↔0` 31 ảnh (font có số 0 gạch chéo) → **kiểm nhãn `o`/`0` trước khi train lại**. LR chưa giảm lần nào (0,001 tới epoch 60), val_em đứng ~81% từ epoch 40. WBS2 ghi "Hoàn thành" theo user (04/11), **sẽ train lại**. Chưa có chỗ dùng trong job |
| `g_160x75`                                    | hoadon30s (P.A Việt Nam, `tracuu.hoadon30s.vn/load-capcha`)                                                | 89,4%  | **Train 02–03/11 bằng `train_aug`** (máy khác, 100 epoch ~12 giờ, 5.495 ảnh, bảng chữ 30; bản cũ 1.995 ảnh val_em 0%): test **86,16%** (549 ảnh), char 96,90%, train_em 92,6% (không học thuộc). Lỗi chính sai độ dài `5→4` (40 ảnh, nuốt ký tự lặp `ww3yt→w3t`) → adapter bỏ đáp án ≠ 5 ký tự. Chưa đo trên captcha thật; adapter `pavietnam` đang tra ở cổng khác (hoadon.pavietnam.vn, ddddocr 40/40) nên chưa cần |

## 1.5 Kế hoạch train (theo ưu tiên)

1. ~~`g_120x40_vnpt`~~: đã train lại 14/10 (test 98,59%).
2. ~~`g_200x70`~~ (Thái Sơn, einvoice.vn): xong 17/10, test 89,24%. Sửa nhãn (upper hết, bỏ nhãn có
   chữ số) đưa nhóm này từ 0% lên 89% — đúng mô-típ "nhãn sai mới là giới hạn" ở 1.3.
   Adapter Thái Sơn ở 2.6 giờ train được rồi.
3. **Cập nhật 30/10: đã train lại trên nhãn sửa case → test 90,63%, xem 1.4; còn phải đo trên captcha thật.**
   `g_180x50_vn` thuộc nền tảng **vninvoice.vn** (`tracuu.v50.vninvoice.vn` — VN-Invoice MST 0101352495
   / "VNISC" dòng 46 Excel MST 0103018807), **KHÔNG phải** invoice.abcsys.vn/MSTTCGP 0102331762 như
   nhầm trước đây (đổi tên `g_180x50_vnisc` → `g_180x50_vn` 20/10 cho rõ, tránh lẫn với mục 4).
   Captcha thật **180x50, bảng chữ hỗn hợp hoa/thường/số** (không phải 6 chữ số như đoán ban đầu lúc
   khảo sát qua trang). Trùng cỡ với `g_180x50_tax24` nên phải giữ là nhóm riêng, adapter ép đúng tên
   nhóm. **Train 16/10 với 11.632 ảnh (52 lớp ký tự) chỉ ra 62,32% test, char 92,09%** — đã vượt xa
   ngưỡng ~250 mẫu/ký tự nên thiếu dữ liệu không còn là nguyên nhân. Lỗi còn lại là **nhầm hoa/thường**
   (`B→b`…): bỏ qua case thì EM đã là 97,89%, tức phần đọc chữ coi như xong.
   **Nguyên nhân (đo 20/10): nhãn sai case, do công cụ gán nhãn tự động bỏ sót chữ hoa.**
   Data KHÔNG phải BotDetect sinh ra mà là **captcha thật cào từ site rồi lưu lại bằng PIL**
   (vân tay PNG: env = RGB `IHDR,IDAT,IEND` ~6 KB; tải thật = RGBA `IHDR,pHYs,...`; BotDetect =
   `IHDR,sRGB,gAMA,pHYs,...` ~15 KB, nặng gấp đôi. Tên file có 3 lần `000001_` = gom nhiều đợt cào).
   Thiên lệch rõ trong nhãn: 4.578 nhãn toàn chữ thường nhưng chỉ 45 nhãn toàn chữ hoa.
   Case thành nhiễu không học được: `train_em=1.0` (thuộc lòng) mà val case vẫn sai.
   **`62,32%` test là số ảo** — tập test dính đúng thiên lệch nhãn của tập train. Đo trên 8 captcha
   thật tải từ site 20/10: **0/8 đúng cả chuỗi, nhưng 48/48 mặt chữ đúng**, mọi lỗi đều một chiều
   hoa→thường (`N2HteL→N2hteL`, `kbU2nz→kbu2nz`, `sF3ArQ→sf3arq`). Nhóm này thực tế **chưa dùng được**.
   Muốn sửa phải gán lại nhãn (không có đường tắt từ bộ sinh, vì data là ảnh thật).
   **`captcha/case_from_geometry.py` (viết 20/10) gán case bằng hình học**, không dùng OCR:
   mặt chữ lấy từ CRNN (đo trên 40 captcha thật: **sai 0/40**), case suy từ hình dạng glyph.
   Đo trên 40 ảnh thật tự đọc tay: exact **18% (chỉ model) → 48% (thêm hình học)**; theo nhóm ký tự:
   x-height `acemnorsuvwxz` **97,7%**, chữ số 100%, thò xuống `gjpqy` 74,5%, nét trên `bdfhklt` 75,8%.
   - **Mốc chiều cao phải lấy từ glyph cao-bất-kể-case** (chữ số + `bdfhklt` + `gjpqy`), không lấy
     thống kê trên _mọi_ glyph: trung vị rơi giữa 2 cụm khi nửa số glyph thấp, phân vị 75 rơi hẳn vào
     cụm thấp khi 5/6 glyph thấp (`vEneaa` ra `VENEAA`). Ảnh toàn chữ x-height thì không có mốc nào →
     dùng ngưỡng **tuyệt đối 18px** (đo 40 ảnh: chữ thường 13–21px, chữ hoa 19–28px → cũng 97,7%).
   - **5 cặp tách được bằng hình dạng mực** (`CASE_RULES`): `b`/`d` chữ thường chỉ có **nét sổ mảnh**
     ở dải 28% trên (0,20–0,31 vs hoa 0,54–0,65), `q` tương tự ở dải dưới; `G` là **vòng hở** nên mật
     độ mực thấp (0,30–0,41) còn `g` là bụng đặc (0,45–0,54); `y` có đuôi nên hộp rỗng hơn `Y`.
   - **Mỗi luật phải tự chứng minh bằng cách thắng chính cái nhãn nó ghi đè, kiểm bằng mắt trên ảnh
     TRAIN** — không phải trên ảnh dùng để fit. Đạt: `b/d/q` 10/10, `g` 11/12, `y` 8/9.
     **Đã loại vì trượt bài kiểm đó:** `f` 2/5, `h` 2/3, `j` 1/6, `t` 1/2 — dù chúng đo được 88–97%
     trên glyph cắt từ captcha mới tải. **Lý do sai: tập để fit đã bị lọc bỏ các bbox dính nhau/cụt
     cho dễ đọc, tức là nửa dễ của dữ liệu, nên ngưỡng rơi vào giữa vùng chồng lấn thật.** `K/k` không
     bao giờ tách (76%). `P/p` thì không thể: cùng hình dạng (nét sổ + bụng), chỉ khác vị trí so với
     đường cơ sở mà cách này không thấy được.
   - **Bảng chữ THẬT của site là 56 ký tự**: `23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz`
     — không có `0 1 o O l I` (generator bỏ hết ký tự dễ nhầm; đo trên 40 ảnh đọc tay + 150 ảnh +
     12 glyph `l` cắt ra đều là `L` + 10 nhãn chứa `l` đều là `L`). `FIXED_CASE` trong
     `case_from_geometry.py` ép thẳng `l→L`, `I→i`, không cần đo gì.
     **Việc này quan trọng hơn vẻ ngoài:** ddddocr ghi 315 chữ `L` thành `l`, mà `l` rơi ngay dưới
     ngưỡng tần suất → `core` **vứt luôn ~300 ảnh đó khỏi tập train**. Sửa xong chỉ còn bỏ 5 ảnh.
   - **Charset đổi sau khi sửa nhãn — phải cập nhật config.** Charset suy ra đi từ 52 → **56 ký tự**,
     thêm đúng `A V W Y`: bốn chữ này trước bị ddddocr hạ thành chữ thường gần như mọi lúc nên tần
     suất dạng hoa rơi dưới ngưỡng và không thành lớp. Config cũ khai 59 ký tự (thừa `0 I l`) đã sửa
     về đúng 56. **Checkpoint cũ (52 ký tự) không nạp được nữa** — phải train lại.
   - **Không đoán case cho các cặp còn lại.** Độ thò xuống không đo được: glyph đầu/cuối nằm ngoài
     vùng có điểm neo, ngoại suy sóng sai vài px trong khi độ thò chỉ vài px. Đoán bằng độ tụt đỉnh so
     hàng xóm chỉ được 74,5% — nghe thì hơn, nhưng khi **gán lại nhãn** thì mốc so sánh là nhãn ddddocr
     (ở nhóm này thường đã đúng), nên ghi đè là lỗ: thử 10 ảnh thật, bật đoán thì 13 lỗi→13, tắt đi còn
     13→3. **Bài học: "tốt hơn trên tập validation" không suy ra "tốt hơn khi gán lại nhãn"** — validation
     so với dự đoán của model (thiên lệch), gán nhãn so với nhãn ddddocr (thường đúng ở nhóm này).
   - Glyph thấp hơn 50% glyph cao nhất trong ảnh = **phân đoạn hỏng** (bắt được mảnh vụn), bỏ qua chứ
     đừng đo — chính nó tạo ra ngoại lệ `b` 0,61 làm tưởng `B/b` không tách được.
     `captcha/relabel_case.py` đổi tên file theo kết quả trên (mặt chữ lấy từ tên file cũ — model đã
     thuộc lòng tập này nên cho kết quả y hệt mà chậm hơn). Mặc định dry-run, có `--apply` và `--undo`,
     **tự xóa `env/.cache`** (chữ ký cache không đổi khi rename, xem 1.3). Chạy thật 20/10, **3 lượt**:
     lượt 1 (x-height) đổi 6.410 ảnh; lượt 2 (`b/d/q`) đổi 1.601 (`d→D` 684, `b→B` 571, `q→Q` 471);
     lượt 3 (`g/y`) đổi 1.223 (`y→Y` 789, `g→G` 388, và ngược lại `G→g` 62, `Y→y` 52).
     Còn **17.450 vị trí** `fhklt`/`jp` giữ nguyên nhãn ddddocr. Nhãn toàn chữ thường giảm 4.578 → ~1.4k.
     Trên 40 ảnh thật tự đọc tay, exact đi từ 18% (chỉ model) lên **52%**.
     **Nhật ký lượt 1 đã mất nên không lùi về nhãn ddddocr gốc được nữa**; ảnh chụp danh sách tên ở
     `env/g_180x50_vn/filenames_snapshot_20261020.txt`, nhật ký lượt 2 ở `relabel_case_pass2.csv`.
     Lần sau chạy nhiều lượt thì đổi tên `relabel_case.csv` giữa các lượt, vì lượt sau ghi đè lượt trước.
     Sau đó vẫn còn nhiễu ở nhóm nét trên/thò xuống → muốn sạch hẳn phải gán tay, nhưng không cần cả
     11.632 ảnh: nhiễu nhãn mới là giới hạn chứ không phải thiếu data — 3.000 ảnh sạch đã là ~346
     mẫu/ký tự, vượt ngưỡng 250 ở 1.3.
     **KHÔNG được fold case:** đã thử thật trên site 20/10 — gõ captcha đảo case bị trả
     "Mã xác thực không đúng" y như gõ bậy, còn gõ đúng case thì qua được sang bước tra dữ liệu.
     Server phân biệt hoa/thường, client không chuẩn hóa (`text-transform: none`, bundle không có
     `toUpperCase/toLowerCase` trên captcha). Fold case chỉ cho ra model không bao giờ qua được site.
     **Chưa có HĐ thật nào dùng cổng này** (kiểm DB 20/10: 0 dòng với nbmst 0101352495/0103018807) nên
     chưa cấp thiết; giữ lại phòng khi phát sinh HĐ từ 1 trong 2 MST đó.
     Khảo sát cổng 20/10 (để dành khi viết adapter): captcha lấy ở
     `GET https://gateway.v50.vninvoice.vn/api/portal/PortalApiService/captcha` → `{image: data-URI PNG 180x50, key}`;
     tra cứu `POST .../portal-public` với `{transactionId, captcha, secret: key, taxCode, fromDate, toDate}`.
     **Không có reCAPTCHA** (không `grecaptcha`/sitekey/iframe — nút "reCaptcha" trên trang chỉ là nút đổi ảnh).
     Lỗi trả về phân biệt rõ: captcha sai → "Mã xác thực không đúng"; captcha đúng nhưng không có dữ liệu →
     "Mã số thuế người bán … không tìm thấy". Mã xác thực dài đúng 6 ký tự (`maxlength=6`).
4. **VNISC thật — MSTTCGP 0102331762** (`invoice.abcsys.vn`, mã `MTCuu`): có **3 HĐ thật** (Bảo Gia
   Trần C26TBV-238805; Dặm Vàng C26TSS-1198, C26TSS-981) nhưng **chưa có model captcha nào** cho đúng
   site này — trước đây nhầm gán `g_180x50_vnisc` (nay là mục 3, thuộc vninvoice.vn, khác site). User
   đang tự tra tay 3 mã trên tại invoice.abcsys.vn để xác nhận lại kích thước/kiểu captcha thật, trước
   khi thu thập mẫu train nhóm riêng (PHẦN 2, mục 2.6).
5. `g_100x30` mới (FAST, einvoice.fast.com.vn): 5 ký tự hoa/thường lẫn lộn, cần nhiều mẫu.
6. Các nhóm < 25%: làm khi phát sinh hóa đơn thật từ NCC đó.
7. `g_150x39` (SmartSign, captcha 3–4 ký tự, sọc chéo đỏ). **Sửa nhãn tay 26/10, train lại 31/10: test 81,36%
   — vẫn thua ddddocr (90%), chưa đổi adapter** (xem 1.4). Muốn hơn: soi ~11.200 nhãn 4 ký tự + sửa lỗi nuốt ký tự lặp.
   Nhãn cũ do ddddocr đọc trên **ảnh thô còn sọc đỏ** nên hay **sót nét mảnh `l 1 j i`** ở đầu/cuối chuỗi
   (`lx51→x5`, `j1j1→j1`, `cctl→cc`): soi 20 ảnh nhãn 2 ký tự thì 18 ảnh sai. Đọc lại bằng bộ lọc sọc của
   adapter (`smartsign.clean_captcha`) ở 4 ngưỡng, chỉ nhận khi cả 4 khớp: đúng ~77%, phần sai vẫn là sót
   `1`/`l`, vì lỗi này lặp lại ở mọi ngưỡng nên so khớp không bắt được → **soi tay cả 3.253 ảnh nhãn 2–3 ký tự**.
   Kết quả: giữ 1.241, đổi tên 1.870, 142 ảnh không đọc chắc + 294 ảnh nhãn 1 ký tự → `env/g_150x39_review/`
   (không xóa). Nhật ký: `env/g_150x39/relabel_150x39_20261026.csv`, `drop_len1_20261026.csv`; tên gốc:
   `filenames_snapshot_20261026.txt`. **Bảng chữ thật 33 ký tự** `12345678abcdefghijklmnopqrstuvwxy`
   (không có `0 9 z`, không chữ hoa: trên 13.308 nhãn mấy ký tự này chỉ 22–59 lần = nhiễu), config đã sửa
   (bản cũ `configs/groups.yaml.bak_20261026`). Nạp thử: train/val/test 10.218/1.277/1.277, ~1.197 mẫu/ký tự,
   `fixed` tự bỏ 100 ảnh có ký tự ngoài bảng. **Chưa soi ~11.200 ảnh nhãn 4 ký tự** → số test có thể thấp hơn
   thực tế; muốn chắc thì đo trên captcha thật tải từ SmartSign. Lệnh:
   `python -X utf8 -m captcha.train --group g_150x39 --data-dir env/g_150x39 --epochs 60`
   (cất `runs/g_150x39` cũ trước hoặc thêm `--force`). Train xong, nếu hơn ddddocr (server nhận 54/60)
   thì đổi adapter `smartsign` sang `solve_captcha(..., "g_150x39")`.

Train xong 1 nhóm của NCC đã có cổng: cập nhật adapter tương ứng ở PHẦN 2 (mục 2.6).

---

# PHẦN 2 – XỬ LÝ HÓA ĐƠN (XML → PDF)

## 2.1 Quy tắc

- `tick`, `sweep` và `add-company` đăng nhập GDT bằng tài khoản thuế thật và ghi DB thật. Chỉ chạy
  khi user đồng ý. Test DB (`tests/test_invoice_store.py`) tạo schema tạm trong DB, cũng phải hỏi trước.
- Code chỉ xóa dữ liệu ở đúng 1 chỗ: `replace_details` xóa dòng `invoice_details` cũ trước khi ghi lại.
  User đôi khi chủ động xóa bảng `invoices` để đổ lại data, nên thấy DB trống không phải lỗi job.
  `tick` tự lấy lại 30 ngày gần nhất; tháng cũ hơn thì chạy `sweep`.

## 2.2 Lệnh

```powershell
# Cài đặt
pip install -r requirements.txt -r requirements-invoice.txt
playwright install chromium                     # adapter VNPT dựng PDF từ HTML

# Job (cấu hình trong .env: DATABASE_URL, HDT_PASSWORD_<MST>, INVOICE_MAIL_*, INVOICE_LIST_SOURCE)
python -X utf8 web/invoice_job.py init-db
python -X utf8 web/invoice_job.py add-company --mst 1201496252 --username 1201496252-U002 --name "..."
python -X utf8 web/invoice_job.py tick          # 1 lượt đầy đủ = xml → mail → pdf (Task Scheduler gọi)
# 3 bước của tick, chạy riêng được (mỗi lệnh 1 hàm riêng, dùng chung khóa nên không chạy chồng):
python -X utf8 web/invoice_job.py xml [--company MST] [--limit N]   # B1: chỉ GDT — danh sách + tải XML (đăng nhập GDT)
python -X utf8 web/invoice_job.py mail [--days 7] [--limit N]       # B2: đọc mail hóa đơn (IMAP, chỉ đọc) + link → adapter
python -X utf8 web/invoice_job.py pdf [--company MST] [--limit N]   # B3: chỉ PDF từ site NCC, KHÔNG đăng nhập GDT
python -X utf8 web/invoice_job.py sweep [--month 2026-09]           # quét lại cả tháng (HĐ lên GDT muộn)
# Mô hình hàng đợi (23/10, thay cho tick — xem 2.4):
python -X utf8 web/invoice_job.py sync-lists [--company MST]        # chỉ danh sách GDT → hộp chờ XML
python -X utf8 web/invoice_job.py worker --type xml|pdf [--per-company 20] [--batch 200] [--once]
python -X utf8 web/invoice_job.py status        # hộp chờ, lỗi, NCC chưa có adapter, HĐ hthdon=2, mail
python -X utf8 web/invoice_job.py requeue --status dead|no_source [--type xml|pdf] [--mst-tcgp X]
python -X utf8 web/invoice_job.py set-provider --mst-tcgp X --source-id Y
# API nội bộ cho WeInvoice backend (06/11): đăng nhập thử GDT khi user thêm MST — chạy thường trực cạnh job
python -X utf8 web/internal_api.py    # POST /internal/gdt/login-test, X-Api-Key = SCRAPING_INTERNAL_API_KEY, mặc định 127.0.0.1:8090
# Worker yêu cầu WeInvoice (09/11): SYNC / FETCH_XML / FETCH_PDF / IMPORT / TEST_DATA từ weinvoice.winv_engn_rqst — chạy thường trực
python -X utf8 web/weinvoice_worker.py [--once]   # cần WEINVOICE_STORAGE_ROOT = WECMS_WEINVOICE_STORAGE_ROOT của backend

# Tải PDF cho XML có sẵn trên đĩa (không cần DB, không gọi GDT)
python -X utf8 web/fetch_pdf.py --company 1201496252 [--dry-run] [--mst-tcgp X] [--seller MST] [--limit N] [--force]

# Lịch chạy tự động (Windows Task Scheduler, mặc định 06:30 / 12:30 / 19:00)
powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1

# Test phần hóa đơn (offline; test DB tự skip khi không có TEST_DATABASE_URL).
# PowerShell không tự mở rộng tests/test_pdf_*.py → dùng -k thay vì glob.
python -X utf8 -m pytest tests -q -k "invoice or xml_parser or fetch_pdf or pdf_ or mail or main_auto"
```

```powershell
# Lấy HĐ theo khoảng ngày tự chọn → DB (chạy tay; cùng luồng với job, không làm lệch mốc đồng bộ của tick)
python -X utf8 web/main_auto.py --from 2026-08-01 --to 2026-09-30 [--company MST | --all-companies] [--type all|mua-all|ban-all] [--pdf] [--excel]
```

`--all-companies` (04/11): chạy lần lượt mọi công ty `active` dưới 1 khóa; mỗi công ty 1 dòng `job_runs`; công ty lỗi (kể cả
exception) không chặn công ty sau, cuối lượt in `--company` cần chạy lại. `--xml-limit` tính riêng từng công ty.

`web/main_auto.py` (từ 14/10) **ghi thẳng vào DB** qua đúng các hàm của job (`sync_lists` với
`move_cursor=False`, `process_xml`, tùy chọn `process_pdf`). Khoảng dài tự chia đoạn ≤ 30 ngày (giới hạn GDT,
`split_range`, `--chunk-days`), **mỗi đoạn lấy danh sách rồi tải XML luôn**; đoạn lỗi không chặn đoạn sau và được in
ra cuối lượt (`stats.failed_chunks`) để chạy lại riêng (26/10).
**27/10 sửa 3 lỗi làm lượt 2023→2026 ngày 26/10 dừng sau 3/47 đoạn:** (1) GDT giới hạn **≤ 1 tháng lịch**, không phải
30 ngày (31/01→01/03 bị 400 "không được lớn hơn 1 tháng") → `job_policy.chunk_range` cắt thêm theo `_month_limit`
(sửa cả cửa sổ 30 ngày của `tick` đầu tháng 3); (2) `sco-query` (HĐ MTT) nay **bắt captcha** cho mọi request tìm/sang
trang/xuất Excel (500 "Required request parameter 'ckey'") → `api_client.sco_captcha()` giải captcha SVG bằng `g_600x120`,
gửi `ckey`+`cvalue` làm query param, sai thì xin ảnh mới (tối đa 4); `export-xml` không cần; (3) GDT 429 khi tải XML →
`xml_batch` nghỉ 60/120/240/300 s thay vì gọi tiếp 0,7 s (5 lần 429 trong 4 s từng làm ngắt mạch). Cả 3 mới kiểm offline,
**chưa chạy thật trên GDT**. Không còn ghi `hoa_don.json` /
`hoa_don_chitiet.json`; `--excel` xuất từ DB. Ghi `job_runs.command='manual'`.
Thư mục cũ `output/xml/1201496252-U002/` và 2 file JSON là của bản cũ (đặt theo username) và có thể xóa.
Khi chạy `fetch_pdf.py` luôn thêm `--company 1201496252`.

## 2.3 File chính và thư mục output

| File                                 | Vai trò                                                                                                                                                                                                               |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_client.py`                      | Client GDT: captcha, authenticate, danh sách (file Excel `export-excel*` hoặc API JSON; sco-query cho MTT), export-xml (ZIP)                                                                                          |
| `invoice_list_excel.py`              | Đọc file Excel danh sách GDT xuất → dòng cùng khóa với API JSON (dò cột theo tên)                                                                                                                                     |
| `xml_parser.py`                      | Parse XML theo QĐ 1233 (docs/QD 1233_Quy dinh chuan DL HD CT.pdf), QR EMV, cảnh báo, `dlhdon_id`                                                                                                                      |
| `job_policy.py`                      | Chính sách thử lại theo loại lỗi, ngắt mạch (circuit breaker), cửa sổ 30 ngày, `summary_only` (hthdon=2)                                                                                                              |
| `invoice_store.py`                   | Lớp PostgreSQL: hàng đợi `tasks` (FOR UPDATE SKIP LOCKED), khóa advisory, JWT cache, upsert HĐ/NCC/chi tiết, mail                                                                                                     |
| `db/schema.sql`                      | companies, gdt_sessions, partners, einvoice_providers, invoices, invoice_details, invoice_xml, invoice_pdf, tasks, task_attempts, sync_cursors, job_runs, job_run_companies, email_messages, email_files, email_links |
| `web/invoice_job.py`                 | CLI của job                                                                                                                                                                                                           |
| `web/fetch_pdf.py`                   | Tải PDF từ XML trên đĩa (không DB); `invoice_from_xml()` = dict HĐ chuẩn cho adapter                                                                                                                                  |
| `pdf_providers/`                     | 1 adapter/NCC, đăng ký trong `__init__.py`; `common.py` (HTTP, `solve_captcha`, `check_pdf_is_invoice`)                                                                                                               |
| `mail_reader.py` / `mail_ingest.py`  | Đọc IMAP (chỉ đọc), nhận mail HĐ, bóc XML/PDF/link; khớp vào HĐ trong DB                                                                                                                                              |
| `scripts/register_invoice_tasks.ps1` | Đăng ký Task Scheduler                                                                                                                                                                                                |

Thư mục output (`INVOICE_OUTPUT_DIR`, mặc định `output/`). Các thư mục `xml/`, `pdf/`, `temp/` dùng
chung một cấu trúc:

```
output/xml/<MST công ty>/<năm>/<tháng>/<MST người bán>/{khmshdon}_{khhdon}_{shdon}.xml
output/pdf/...             (cùng cấu trúc, .pdf)
output/temp/...            (ZIP gốc tải từ Thuế, .zip)
output/list/<MST công ty>/<sub_type>_<từ ngày>_<đến ngày>.xlsx   (file danh sách gốc GDT xuất)
output/mail/unmatched/     (file từ mail chưa khớp được HĐ)
output/logs/invoice_job_<yyyy-mm>.log
```

## 2.4 Luồng xử lý (`tick`)

Code tách 2 hàm chính: `run_xml_step` (bước 2–4: GDT → XML, lệnh `xml`) và `process_pdf`
(bước 6: site NCC → PDF, lệnh `pdf`); `run_mail_step` (bước 5, lệnh `mail`) nằm giữa. `tick` chỉ gọi lần lượt 3 phần.

1. Lấy khóa advisory (không chạy chồng) → thu hồi task kẹt → HĐ `no_source` của NCC vừa có adapter quay lại hàng đợi.
2. **Token:** dùng lại JWT trong `gdt_sessions` nếu còn hạn; chỉ đăng nhập lại (giải captcha `g_600x120`)
   khi hết hạn hoặc gặp 401.
3. **Danh sách (lấy nguyên danh sách trước, rồi mới tải XML):** mỗi loại HĐ 1 file Excel như nút
   "Xuất Excel" của cổng (từ 20/10; `INVOICE_LIST_SOURCE=json` = API phân trang cũ), file gốc lưu
   `output/list/`. GDT từ chối xuất hoặc file không đúng mẫu → khoảng đó tự lấy lại qua API JSON.
   Luôn quét lại 30 ngày gần nhất, cộng phần còn hở theo `sync_cursors`; ngày 5 và 15 tự quét lại
   tháng trước. HĐ đã có thì bỏ qua, HĐ mới vào hộp chờ (task `xml`). Task xong = `done`, không bao giờ
   được lấy lại. HĐ `hthdon=2` (chỉ API JSON có trường này) thì không tạo task tải, để ở `no_source`/`summary_only`.
   Danh sách GDT mỗi lần lấy một khác (nhiều/ít/thiếu/trùng dòng) cũng không sao: khóa (MST bán, mẫu, ký
   hiệu, số) chỉ vào hộp 1 lần, HĐ vắng mặt ở lần sau không bị xóa. Log mỗi danh sách ghi
   "GDT trả N HĐ → đã có X (bỏ qua), mới Y (vào hộp chờ)". Task `dead` (bỏ cuộc sau nhiều lần lỗi)
   không tự vào lại hộp khi GDT liệt kê lại — dùng `requeue --status dead --type xml`.
   Dữ liệu parse từ XML chỉ ghi DB, không ghi file JSON (2 file `output/hoa_don*.json` là của bản cũ 14/10).
4. **XML:** tải ZIP → lưu `temp/` → lưu XML → ghi `invoices` / `invoice_xml` / `invoice_details`
   / `partners` trong 1 transaction → tạo task `pdf`.
   **PDF của Thuế** (10/11): ZIP export-xml **không có PDF** (4.402/4.402 ZIP chỉ có `invoice.xml`, `invoice.html`,
   `details.js`, 2 ảnh). `gdt_pdf.py` in `invoice.html` (trang xem HĐ của cổng Thuế, có QR + khung chữ ký) ra PDF bằng
   Chromium chặn mạng → `output/pdf_gdt/...` + `invoice_xml.gdt_pdf_path`; lỗi chỉ ghi log. Bản tạm: backend chỉ liệt kê
   (`pdf-gdt`, kind `pdf_gdt`) khi chưa có PDF gốc NCC / PDF tải lên; không tính là "có PDF". Dựng bù từ ZIP đã lưu:
   `invoice_job.py gdt-pdf [--company MST] [--limit N] [--all]` (không gọi GDT; ZIP ở máy khác thì phải chạy ở máy đó).
   Tắt: `INVOICE_GDT_PDF=0`.
5. **Mail** (nếu cấu hình), xem mục 2.7: XML cho HĐ Thuế không có XML, PDF đính kèm.
   Chạy trước bước PDF để PDF đã có trong mail thì không phải gọi site NCC.
6. **PDF:** chọn adapter theo `source_id` gắn tay (set-provider), nếu không có thì theo MSTTCGP
   (MST chi nhánh `-001` dùng adapter công ty mẹ) → tải về → kiểm tra PDF có đúng số HĐ → ghi `invoice_pdf`.
   `--limit` chỉ tính những HĐ thật sự gọi site NCC.
7. **Lỗi:** task được hẹn giờ thử lại theo `RETRY_POLICY`. GDT lỗi tạm thời 5 lần liên tiếp thì dừng phần
   GDT của lượt đó. Mỗi site NCC có circuit breaker riêng.

**Nhật ký theo MST** (`job_run_companies`, thêm 22/10): mỗi lượt chạy × mỗi công ty 1 dòng
(`run_id, company_mst, started_at, finished_at, status, stats`), ghi ở cả 3 bước — GDT, mail→PDF
và PDF từ site NCC. `job_runs.stats` vẫn giữ bản tổng; bảng này để query theo MST không phải đào
JSONB. `tick` ghi 2 lần cho cùng 1 công ty (sau phần GDT, sau phần PDF) nên `stats` được **trộn**
bằng `||`, không ghi đè. `status` theo công ty: `error` = công ty đó không đăng nhập được GDT,
`partial` = chạy được nhưng có HĐ lỗi / ngắt mạch, `ok` = sạch — **khác** `job_runs.status` vốn
là trạng thái của cả lượt. Dòng của các lượt trước 22/10 là dồn ngược từ `job_runs.stats` nên
mang `status` mức lượt (vd lượt #23 ghi `ok` dù có 3 XML lỗi).

**Mô hình hàng đợi** (23/10, thay cho `tick` khi nhiều công ty / nhiều HĐ): `tick` làm lần lượt
từng công ty dưới **khóa toàn cục**, nên công ty cuối danh sách chờ hết các công ty trước, và cả
hệ thống chỉ chạy 1 việc 1 lúc. Mô hình mới tách người sinh việc khỏi người làm việc:
- `sync-lists` (3 lần/ngày): chỉ lấy danh sách → hộp chờ XML. Khóa riêng `SYNC_LOCK_KEY`: không
  chạy chồng chính nó, không chặn worker/`tick`. Ngày 5/15 vẫn tự `sweep`.
- `worker --type xml|pdf` (chạy mãi): `claim_round_robin` nhận mỗi công ty tối đa `--per-company`
  HĐ mỗi mẻ (cũ trước) → công ty 10.000 HĐ không chặn công ty 30 HĐ. **Không** giữ khóa toàn cục:
  nhiều worker chạy song song, `SKIP LOCKED` lo chia việc. Kiểm trên PostgreSQL thật (bảng tạm):
  1.000/30/5 HĐ → mẻ 1 chia 20/20/5.
- `mail` chạy lịch riêng (30–60 phút), **không phải bước sau NCC**: mail không tra theo từng HĐ
  được, thường đến trước GDT, và rẻ hơn NCC. `apply_email_pdf`/`apply_email_xml` tự đóng task,
  nên worker PDF không gọi site NCC cho HĐ đã có PDF từ mail.
- **Khóa đăng nhập theo MST** (`login_lock`, advisory 2 khóa): nhiều worker cùng gặp token hết hạn
  thì chỉ 1 worker đăng nhập; worker có khóa đọc lại token trong DB trước, khác cái bị từ chối thì
  dùng luôn. Không có khóa này thì các worker giải captcha trùng và có thể đá phiên nhau.
- Công ty không đăng nhập được / GDT lỗi liên tiếp → nghỉ `COOLDOWN_MINUTES` (15), công ty khác
  chạy bình thường. Site NCC ngắt mạch → task hẹn lại `DEFER_MINUTES` (10) bằng
  `release_tasks(delay_minutes=…)` — trả ngay thì mẻ sau nhận lại đúng task đó và quay vòng vô ích.
- `job_runs` của worker (`worker-xml`/`worker-pdf`): 1 dòng mỗi **đợt việc**, mở khi nhận được việc,
  đóng khi hộp chờ rỗng hoặc quá 1 giờ; worker ngồi chờ không sinh dòng nào.
- Lịch: `scripts\register_queue_tasks.ps1 -RunWhenLoggedOff` tạo 6 task (Sync Lists 06/14/22h,
  Mail mỗi 30 phút, Worker XML/PDF, và từ 10/11 **WeInvoice API** `web/internal_api.py` (log
  `output/logs/internal_api_<yyyy-mm>.log`) + **WeInvoice Worker** `web/weinvoice_worker.py`; 4 task chạy mãi khởi động
  cùng máy, không giới hạn thời gian, chết thì bật lại sau 1 phút). Máy không chạy WeInvoice: thêm `-NoWeInvoice`. Đang chạy `tick` thì gỡ task đó đi cho khỏi trùng (chạy cùng lúc không hỏng dữ liệu).
- **Chưa có**: giới hạn số request đồng thời mỗi tài khoản GDT khi chạy nhiều worker XML (hiện chỉ
  nhịp 0,7 s/HĐ mỗi worker) — chạy 1 worker XML cho tới khi đo được ngưỡng của GDT; rải lịch
  `sync-lists` theo MST khi lên vài chục công ty.

## 2.5 Những điểm dễ sai về GDT

- Mọi request phải có header `request-id` (UUID), thiếu là bị 403 "hành vi không hợp lệ".
- Query hóa đơn mua vào phải có `ttxly` (5 = có mã, 6 = không mã, 8 = MTT). MTT dùng `/sco-query/`, và từ 10/2026
  mọi request danh sách/xuất Excel `sco-query` phải kèm `ckey`+`cvalue` (captcha GDT, `api_client.sco_captcha`).
- Khoảng tìm kiếm ≤ 30 ngày **và** ≤ 1 tháng lịch (31/01→01/03 bị từ chối dù chỉ 30 ngày).
- **`tdlap` là giờ UTC.** Luôn đổi qua `api_client.tdlap_vn()`, nếu không HĐ ngày 1 sẽ bị xếp nhầm sang tháng trước.
- Username đăng nhập (vd `1201496252-U002`) có thể khác MST: DB tách `company_mst` và `gdt_username`.
- **`hthdon=2`** (HĐ không mã, gửi qua bảng tổng hợp): export-xml **luôn trả 500**. Job vẫn lưu phần
  đầu HĐ, còn XML/PDF lấy từ mail.
- **File Excel danh sách thiếu `hthdon`** (chỉ 17–19 cột, API JSON có 150 trường). Nên HĐ không mã (`K...`)
  lấy từ Excel được tải thử XML 1 lần: GDT trả 500 → `no_source`/`summary_only` luôn, không thử lại 10 ngày
  (dữ liệu thật: 19/19 HĐ hthdon=2 trả 500, 30/30 HĐ không mã hthdon=0 tải được).
- Endpoint xuất Excel (URL chép từ trình duyệt): bán ra `query/invoices/export-excel`; mua vào
  `export-excel-sold` + `type=purchase`; MTT dùng `sco-query`; cổng gửi `ttxly==5` kèm 4 dấu cách.
  File do Apache POI tạo ghi `<dimension ref="A1"/>` → **không đọc bằng openpyxl `read_only`** (chỉ thấy 1 dòng).
  HĐ MTT có bố cục cột khác HĐ thường → `invoice_list_excel` dò cột theo tên.
- Ký hiệu HĐ: ký tự đầu `C` = có mã CQT (MCCQT 34 ký tự), `K` = không mã; ký tự thứ 4 là `M` = máy tính tiền.

## 2.6 Adapter PDF (`pdf_providers/`)
- **Thuế khóa tài khoản sau 5 lần sai mật khẩu** ("Tài khoản đã bị khoá vì đã nhập sai thông tin quá số lần quy định").
  04–10/11 đã khóa 3 tài khoản (3600823561-U002, 3603512483 dùng cho 3603512483-001, 0318793907): `authenticate_auto` dừng
  đúng sau 1 lần sai, nhưng **mỗi lượt sau lại thử đúng mật khẩu sai đó** (`tick` + `sync-lists` + `sweep`, worker XML mỗi 15 phút).
  Sửa 10/11: `api_client.classify_login_failure` → chỉ **sai captcha** mới thử lại; sai mật khẩu / bị khóa ghi `gdt_login_blocks`
  (`gdt_login_guard.py`, mỗi mật khẩu bị từ chối 1 dòng, vân tay HMAC, không lưu mật khẩu) → mọi lượt sau dùng đúng mật khẩu đó
  **không gọi Thuế** (`TokenProvider._login`, `internal_api` login-test). Mật khẩu mới (sửa `.env` / cập nhật trên WeInvoice) thì
  được thử; mật khẩu lấy từ WeInvoice bị từ chối → `winv_cmpn_infm.conn_stts='ERROR'` + câu của Thuế (màn Kết nối hiện lỗi).
  Xem / bỏ chặn: `invoice_job.py login-blocks`, `unblock-login --mst X` (bỏ chặn **và bật lại** `companies.active`). 3 tài khoản
  trên đã chặn + `active=false` 10/11 (máy khác dùng chung DB còn chạy code cũ, chỉ tôn trọng `active`) → mở khóa/đổi mật khẩu
  xong thì `unblock-login`. Máy nào chạy job phải cập nhật code mới, nếu không vẫn thử lại mật khẩu sai.

Các adapter dưới đây đã thử thật trên XML thật (10/2026):

| source_id        | NCC         | MSTTCGP                | Mã dùng để tra                                   | Captcha                                                                                                                                                                                                                                            |
| ---------------- | ----------- | ---------------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `misa_meinvoice` | MISA        | 0101243150             | TTKhac `TransactionID`                           | không (cần header `Accept` kiểu trình duyệt)                                                                                                                                                                                                       |
| `easyinvoice`    | Softdreams  | 0105987432             | `Fkey` (+ MST bán có gạch)                       | `g_150x34`                                                                                                                                                                                                                                         |
| `vnpt`           | VNPT        | 0100684378             | `Fkey` (VNPT để ở `<HDon><Fkey>`) / MCCQT        | `g_80x30`. Mỗi khách lớn có cổng riêng (`SELLER_PORTALS`): Petrolimex (dựng PDF từ HTML), VNPT TP.HCM `vttphcm-tt78` (server có sẵn PDF + XML có chữ ký; HĐ không tồn tại → server treo, timeout 45 s → `no_pdf`); không có cổng riêng thì thử HST |
| `ipos`           | iPOS        | 0104789847             | link mail `token=base64(mẫu:ký hiệu:số:MST bán)` | không. PDF + XML (XML không ký số)                                                                                                                                                                                                                 |
| `viettel`        | Viettel     | 0100109106             | TTKhac "Mã số bí mật" + MST bán                  | slider, nhưng server không kiểm                                                                                                                                                                                                                    |
| `hilo`           | T-VAN HILO  | 0106713804             | TTKhac `Fkey` (cổng Grab)                        | SVG → ddddocr                                                                                                                                                                                                                                      |
| `bkav`           | BKAV        | 0101360697             | `DLHDon/@Id` (InvoiceGUID) → `dlhdon_id`         | không                                                                                                                                                                                                                                              |
| `vetc`           | VETC        | 0107500414, 0106858609 | TTKhac "Mã tra cứu"                              | không                                                                                                                                                                                                                                              |
| `minvoice`       | M-INVOICE   | 0106026495 (+ -001)    | "Số bảo mật" / "Mã tra cứu" + MST bán            | không                                                                                                                                                                                                                                              |
| `myinvoice`      | My Software | 0108971656             | MCCQT                                            | không                                                                                                                                                                                                                                              |
| `visnam`         | Visnam      | 0401486901             | `QuanLy_SoBaoMat`                                | `g_1500x350`                                                                                                                                                                                                                                       |
| `thaco`          | THACO (cổng riêng tập đoàn Trường Hải) | 3600252847 | TTKhac `DownloadPdf` (`getinvoice?token=` base64 mẫu_ký hiệu_số\|MST bán; phần mẫu nội bộ không suy được nên chỉ dùng link có sẵn) | không. Trả ZIP chứa PDF; không có `DownloadPdf` → `no_code` (form tra có captcha giống `g_194x35`, chưa làm). Thật 4/4 (23/10) |
| `vinhhy`         | Vĩnh Hy (ehoadondientu.com) | 0314743623 | `DLHDon/@Id` 24 số = DB 14 số + bản ghi 10 số → `GenerateFile.aspx?r=ct_<14>_<10>&type=pdf`; mã mail 10–11 ký tự qua `Tracuu.aspx/tracuu` | không. Có `fetch_xml` (XML có chữ ký). Thật 3/3 (23/10) |
| `minhkhang`      | Minh Khang Group (hoadondientuvat.com) | 0314209362 | như `vinhhy` (cùng phần mềm, khác host/DB) | không. Thật 1/1 (23/10) |
| `xcyber`         | CyberLotus (xcyber/cyberbill) | 0105232093 | TTKhac `MaTraCuu` (12/15 ký tự) | `g_300x75` (server có kiểm; 38/38 câu trả lời được nhận). 2 hệ thống: bill2app trước, không thấy thì bill1app. Thật 1/3 — 2 HĐ 2023 hệ thống 1 báo "Lỗi phát sinh" (lỗi phía NCC, để `server` thử lại) |

| `thaison`        | Thái Sơn (einvoice.vn, einvoice.mmvietnam.com) | 0101300842 | TTKhac "Mã TC"; cổng theo TTKhac "DC TC"/`lookup_url`, không có thì `SELLER_PORTALS` (MM, Jollibee, Kiến An), rồi cổng chung einvoice.vn | `g_200x70` (server có kiểm, 19/22 = 86%); cần header `Referer` không thì ra ảnh lỗi. **Lấy được đa số người bán — riêng Grab không lấy được (lấy qua mail).** Thật 14/14. Cổng riêng thêm 23/10 (host lấy từ dòng "tra cứu tại (Tracking)" in trên PDF): Cổng Vàng `vat.ggg.com.vn` (`g_200x70`, 3/3), KFC `invoice.kfcvietnam.com.vn` (bỏ `www.`, 2/2) và Zalo `invoice.zaloplatforms.vn` (2/2) dùng `/Captcha` 200x60 6 ký tự **phân biệt hoa/thường** → ddddocr + sửa case theo hình dạng (`solve_img_captcha`, server nhận 44/51), Diệu Phúc/CellphoneS `hddt.cellphones.com.vn` (captcha sinh bằng JS, server không kiểm, 2/2). Grab 0312650437 → `no_portal` ngay: HĐ 2025 có trên cổng HILO nhưng tải trả 400, HĐ 2023–24 không cổng nào có |
| `fast`           | FAST (einvoice.fast.com.vn) | 0100727825 | TTKhac `KeySearch` (base64 vào `p`) + `n`=số HĐ | bước tải PDF (`t=4`) **không kiểm captcha** (chỉ bước xem `t=2` kiểm). Thật 6/6. Link mail `?c=<KeySearch>`: số HĐ lấy từ tiêu đề mail ("…hóa đơn điện tử số 5470"), sửa 23/10 sau khi 76 link báo `no_code` |
| `vnisc`          | VNISC (invoice.abcsys.vn) | 0102331762 | TTKhac `MTCuu` | ảnh 100x46, 6 chữ số → **ddddocr** 20/20 (không cần model). Thật 10/10 |
| `smartsign`      | VI NA / SmartSign (tracuuhd.smartsign.com.vn) | 0309612872 | TTKhac `Matracuu` | ảnh 150x39 phân biệt hoa/thường → ddddocr sau khi lọc sọc đỏ 54/60. Thật 4/4; người bán 0304892753 (2024) ghi `Matracuu` trong XML khác mã in trên PDF → `no_pdf`; HĐ 2023 không có TTKhac → `no_code` |
| `pavietnam`      | P.A Việt Nam (hoadon.pavietnam.vn, **không phải** tracuu.hoadon30s.vn) | 0302431595 | mã 13–14 chữ hoa **chỉ có trên PDF/mail**, XML không có → HĐ chỉ có XML ra `no_code` | ddddocr 40/40 sau xử lý ảnh; tường lửa SafeLine chặn `new_session` (cipher cũ) nên dùng session `requests` mặc định. Thật 7/7 bằng mã từ mail |

| `express247`     | 247Express / CTCP Hai Bốn Bảy (người bán 0304043037, cổng riêng hoadon.247express.vn) | — (HĐ hthdon=2, không có XML) | mã 24 ký tự **chỉ từ link mail** → `GET /service/api/Invoice/DownloadFilePDF/<mã>` | không (form tra trên web có reCAPTCHA nên không dùng, không lấy XML). Thật 8/8 (23/10), khớp HĐ qua `_match_pdf_text` |

| `evat`           | Phương Nam (phuongnam.evat.vn, nền eVAT) | 0316642395 | TTKhac "Mã tra cứu hóa đơn" + "Mã công ty" (thiếu thì `nbmst`) → POST `/bill` (CSRF) → ZIP có PDF + XML ký số | không. Thật 4/4 PDF + XML (26/10). `PORTALS` MSTTCGP → subdomain: hoadonsovn/tvl/vlc/trandinhtung.evat.vn cùng form, thêm khi có HĐ thật. `tracuu.evat.vn` nay chuyển sang wininvoice (0312303803), không thuộc adapter này |
| `matbao`         | Mắt Bão (matbao.in) | 0302712571 | XML không có mã → API của cổng `GET /api/tra-cuu-hoa-don/fkey?taxcode=&pattern=&serial=&no=` → Fkey → `pdfUrl`/`xmlUrl` | trang có widget reCAPTCHA v2 nhưng **server không kiểm** (gửi không token vẫn trả bình thường); adapter không gửi token, gặp 403 → `no_portal`. Thật 2/2 + 1 qua mã mail. PDF dựng lại theo mẫu hiện tại (khác nhẹ bố cục so với PDF mail) |
| `onlinevina`     | Online VI NA (hoadon.onlinevina.com.vn) | 0314185087 | TTKhac `SearchInvoice` (24 hex) → `POST /api/v1/invoice/invoice/view` | không. **Server không có PDF** (trang dựng bằng html2pdf) → adapter áp XSLT của người bán bằng lxml (cấm đọc file/mạng), bỏ JS, render bằng `render.html_to_pdf`; chặn PDF trắng. Chữ trùng 100% PDF mail, thiếu viền bảng/watermark. Thật 1/1 |
| `vininvoice`     | VININVOICE (tracuu.vininvoice.vn) | 0109282176 | `ma_tra_cuu`/TTKhac, không có thì MCCQT → `/erp/rest/s1/iam-entry/invoices/<mã>/pdf` (cùng phần mềm `myinvoice`) | không. Thật 3/3, chữ trùng PDF mail |

| `wininvoice`     | WinInvoice / Win Tech (tracuu.wininvoice.vn; `tracuu.evat.vn` chuyển về đây) | 0312303803 | như `evat` (kế thừa `EvatAdapter`): TTKhac "Mã tra cứu hóa đơn" + "Mã công ty" (bắt buộc) | không. Thật 6/6 (26/10). Mail chỉ có mã, không có mã công ty → link thường `ignored` (mail đã kèm PDF/XML); giá trị chính là HĐ từ XML Thuế |
| `rosysoft`       | Rosy Soft ROSY_ERP (einv.rosysoft.vn:8386) — người bán Ánh Dương/Vinasun 0302035520 | 0305142231 | `TaxCode`+`InvID` (link mail) hoặc `TaxCode`+`InvSerial`+`InvNo` → lưới trả `PINCode` → PDF base64 + XML ký số | không. Thật 26/26 (24 link mail + 2 HĐ trong DB). HĐ Vinasun là hthdon=2, không có XML → không có task PDF tự động; lấy qua link mail |
| `asiainvoice`    | ASIA / A-Invoice (asiainvoice.vn, tracuu./tt78.) | 0101162173 | FKey = "Mã tra cứu" **chỉ có trong mail** (XML Thuế không có → `no_code`) → `POST /tra-cuu {FKey, Captcha}` → PDF base64; XML qua `/tra-cuu/downloadxml` cùng phiên | JPEG 150x30 5 chữ HOA, lấy từ **kho cố định** → tra bảng `data/asia_captcha.npz` (so điểm ảnh, lệch ≤ 4 mức xám/điểm; 29/10: 55/60 ảnh có trong bảng, server nhận 35/36), ngoài bảng mới ddddocr + `.upper()` (11/15). Thử tối đa 6 lần. Thật 2/2. **Tự học** (29/10): đáp án ddddocr được server nhận (`status` hoặc `error 1`) → lưu `<INVOICE_OUTPUT_DIR>/captcha_learned/asiainvoice/<sha1>_<NHÃN>.png`, lần sau tra thẳng, xét trước bảng gốc; đáp án bảng gốc bị trả `error 3` → bỏ dòng đó tới hết tiến trình. Thử thật 15 lượt: 15/15 qua captcha, học +2 ảnh. Gộp phần đã học vào bảng commit: `scripts/build_asia_captcha_table.py --with-learned output/captcha_learned/asiainvoice` |
| `fahasa`         | Fahasa (hoadon.fahasa.com, người bán 0304132047) — **FPT tự host** | — (không khai 0104128565 vì cổng FPT chung có reCAPTCHA) | link mail `sec=<mã>`, số HĐ từ tiêu đề → `/api/seek/search1?...&token=` | bản cài này không bật reCAPTCHA (`captchaType` không đặt), token rỗng được nhận 5/5. Thật 5/5 PDF + XML |

| `mobifone`       | MobiFone (tracuuhoadonvienthong. / tracuuhoadon.mobifone.vn; cổng chung tracuuhoadon.mobifoneinvoice.vn) | 0100686209 (+ -002) | "số bảo mật" **chỉ có trong mail** (`sobaomat=`) → `POST /Tracuu2/GetInfoInvoice` → `PrintInvoice` (PDF) + `ExportZipFileXML` | không. Thật 10/10 (27/10, link mail). XML Thuế không có mã → HĐ chỉ có XML vẫn `no_code` |
| `kiotviet`       | KiotViet (api.einvoice.kiotviet.vn) | 0104359717 | `mtc` từ link mail `?shd=&mtc=` (HĐ MTT không có mã trong XML) | không. Thật 1/1 PDF + XML (27/10) |
| `vninvoice`      | VN-Invoice (tracuu.v50.vninvoice.vn; API gateway.v50.vninvoice.vn) | 0101352495, 0103018807 | TTKhac "Mã bảo mật" (FamilyMart 20 ký tự, Phong Vũ 7 số; PDF in là "Mã tra cứu"/"Số bảo mật") + MST bán + ngày lập → `POST portal-public` → `download/public/unofficial` (PDF base64 ở `fileBytes`) / `download/public/xml` | `g_180x50_vn` (6 ký tự, phân biệt hoa/thường; đáp án ≠ 6 ký tự → xin ảnh mới). Cổng trả **HTTP 500** cho captcha sai ("Mã xác thực không đúng") nên adapter tự gọi HTTP, không qua `common.request`. Thật **15/15 PDF + XML** (02/11, ~3 s/HĐ) |
| `fpt`            | FPT eInvoice (tracuuhoadon.fpt.com.vn) | 0104128565 | MST bán + mã `sec` (**chỉ có trong mail**) | reCAPTCHA v3, server kiểm → job **không tự tra**: nhập JSON `/api/search` do script trình duyệt `pdf_providers/fpt.py` (người chạy) lưu vào `output/manual/fpt/` (JSON có `pdf` base64 + `xml`), xem mục "HT, EFY, FPT". Thật 4/4 (03/11) |
| `ameinvoice`     | AMEINVOICE / NC9 Việt Nam (ameinvoice.vn) | 0312270160 | `key_receive` (20 số) trong `lookup_url` của XML (`/einvoice/?key_receive=`) hoặc link mail → `GET /view-teamplate-invoice/?down_pdf=true&key_receive=` | không (reCAPTCHA trên trang chỉ của form liên hệ Contact Form 7). PDF có BOM trước `%PDF` → adapter cắt. `down_xml` trả HTML, không dùng. Thật 2/3 (03/11); C23TLB-128 người bán 0314391594: máy chủ trả `Connection Failed: 1049` (mất CSDL 2023 phía NCC) → `server` |

**Adapter thêm 27/10 cho NCC trong `docs/Danh_sach_NCC.xlsx` — CHƯA có HĐ thật** (DB không có HĐ/link nào của các MSTTCGP
này): mỗi cổng chỉ mới kiểm phản hồi "không tìm thấy" (và captcha sai/đúng) bằng mã bịa; nhánh tải PDF suy từ JS/HTML của
cổng. Mọi PDF vẫn qua `check_pdf_is_invoice`, nên sai thì ra `no_pdf`/`bad_content` chứ không lưu nhầm. Có HĐ thật thì chạy
`pdf --limit` để xác nhận rồi cập nhật dòng ở đây. Tên trường TTKhac chưa đối chiếu XML thật → `pdf_providers/lookup_codes.py`
gom mọi trường giống "mã tra cứu/số bảo mật".
- Cùng phần mềm adapter cũ: `tialuaviet`, `tuanchau`, `minhthu`, `htsaigon` (Vĩnh Hy); `lienson`, `myinvoicevn` (xcyber,
  `g_300x75`; cổng hoadondientuvn.info chết 27/10); `evat` thêm hoadonsovn/tvl/vlc/trandinhtung.evat.vn + tracuu.ttltax.com;
  `wininvoice` thêm 0311174986; `homecasta` (VININVOICE); `atis` (phần mềm VETC); `epay`, `atom` (phần mềm M-Invoice);
  `iam` (T-HOADON 0318511655, IAM 0109193102), `suninvoice` (iam-entry như myinvoice/vininvoice — mỗi host CSDL riêng);
  `vietinvoice` (iCORP, 2 cổng); `easyinvoice` nhận cả subdomain người bán (`0109045760hd.`; sửa regex Fkey có dấu `-`).
- Captcha model (server nhận 10/10 mỗi cổng): `megabiz` `g_120x38`, `pvoil` `g_194x35`, `cloudteam` `g_120x40_cloud`
  (cổng theo subdomain khách, lấy từ `lookup_url`).
- Không captcha: `pmbk` (2 cổng Bách Khoa), `ptp`, `acman`, `avse`, `koffi`, `vietinfo`, `ehcm` (`formlookup.py` dùng chung),
  `sonphat`, `aits`, `kimtuthap`, `intrust` (INTRUST + SCHOOL TECH; reCAPTCHA chỉ ở giao diện, server không kiểm), `sapo`,
  `igas`, `sctv`, `mmhoadon`, `gohoadon`, `winvoice` (Khánh Linh, khác `wininvoice`), `vnpost` ("mã xác thực" chỉ kiểm ở trình duyệt).
- Không làm được qua link (ghi ở sheet WBS2 cột Ghi chú): reCAPTCHA v2 (L.C.S, EFY, HT, Máy tính & TT, New-Invoice, idoc,
  ACC Online, Safecert, CMC, Mobile-ID), v3 (FPT + đại lý oinvoice, VNPAY, Nhân Hòa, Haravan, Pancake, Taxnet, Bado), Turnstile
  (TS24, Bizzi), đăng nhập (VDSG, NCCA, ONTIK, TAX24 — `tax24.com.vn/einvoice` là cổng đăng nhập, `g_180x50_tax24` là captcha
  đăng nhập), model chưa dùng được (Ngô Gia Phát, Bitware); Lạc Việt có model `g_100x36` (02/11) nhưng chưa có adapter. (NC9/ameinvoice: đã làm 03/11, adapter
  `ameinvoice`; chỉ CSDL 2023 của người bán 0314391594 lỗi phía NCC.) **Cần 1 mã
  thật mới làm được:** KK VAT, Nacencomm, 3A, TADU (ZK `/zkau`) — script dò để trong scratchpad phiên 27/10.
  FPT Shop `hddt.fptshop.com.vn`: tra bằng mã 16 số không cần captcha, nhưng bước tải trả **PDF của khách khác** → không làm.

Không làm (26/10): `einvoice.com.vn` / `hoadon.ftg.vn` là Thái Sơn (cổng riêng yeungkyung., lotterentacarvina.einvoice.com.vn —
mail đã kèm file; link `/xac-nhan-email/` **không bao giờ mở**); e-invoice-tt78.vingroup.net
(tra cần MST + mẫu + ký hiệu + số + mã bí mật + captcha, mail không đủ thông tin).

**Cổng riêng người bán thêm 23/10** (tìm từ dòng "Tra cứu hóa đơn tại website" in trên PDF trong mail):
- VNPT `-tt78` (luồng tt78 + `g_80x30`; đọc captcha độ tin thấp thì xin ảnh mới thay vì gửi bừa): Golf Long Thành
  3600718503 (TTKhac `Extra`, thiếu thì MCCQT, 6/6), SW F&B CN2 0313894637-002 (MCCQT, 6/6), SW F&B trụ sở 0313894637
  (cổng riêng `swfoodsbeverages-tt78`, MCCQT, 3/3 — 03/11; không nằm trên cổng CN2), Navigos 0304836029
  (MCCQT, 1/1). Rule trong `SELLER_PORTALS` có `keys` = lấy mã tra từ trường nào. `seller_rule()`: khớp MST đúng →
  MST công ty mẹ → tên; nhờ đó Xăng dầu Sài Gòn 0300555450-001 vào cổng Petrolimex (2/2).
- HILO Kim Đại Phát 3603526528 (`tracuuhddt78.hilo.com.vn`, 3/3): form tra có reCAPTCHA v3 nên không dùng; tải qua
  `/Invoice/getinvoice?token=base64(mẫu:ký hiệu:số:MST bán)` — đúng link HILO gửi trong mail. Với người bán trong
  `LEGACY_SELLERS` adapter **tự dựng token từ XML**, đồng thời nhận link mail `getinvoice` / `ViewFromEmail`
(`invoice_from_link`) — user chọn giữ cả 2 đường (23/10). Người bán mới dùng cổng này: thêm MST vào `LEGACY_SELLERS`.
  `tt78hd.hilo.com.vn` dùng token kiểu khác (`1_C23TIH_927|MST`) — làm 26/10 (`decode_tt78hd_token`, link mail 2/2).

`pdf_providers/ocr_ddddocr.py`: nạp ddddocr dùng chung (ddddocr 1.6 `set_ranges` làm hỏng kết quả → mỗi adapter tự lọc ký tự).
Kiểm chứng 23/10 đều bằng HĐ thật của công ty; PDF lấy được trùng nội dung với PDF đã có từ mail.

**HĐ máy tính tiền không có MSTTCGP trong XML** (23/10, `invoice_job.pick_adapter`): chọn adapter theo host link
tra cứu trong XML (`lookup_url` / TTKhac `PortalLink`), không có thì theo **NCC quen của người bán** (MSTTCGP hay gặp nhất
trên HĐ khác của họ, `store.seller_mst_tcgp`). `_prepare_queues` tự đưa các HĐ `no_source` thiếu MSTTCGP đã định tuyến
được về hàng đợi. Trước đó 294 HĐ MTT của Grab nằm chờ vô lý (adapter `hilo` lấy được, thử thật 2/2).

**Kiểm trên DB 22/10** (PDF đã lưu, đều qua `check_pdf_is_invoice`): chạy chắc trên **toàn nền tảng**
(nhiều người bán khác nhau): MISA 39 PDF/15 người bán, BKAV 35/11, Viettel 23/6, EasyInvoice 7/6,
M-Invoice 10/4. Chỉ mới chứng minh **cho đúng 1 người bán**: HILO (chỉ cổng Grab, 52), VNPT (chỉ
Petrolimex, 14 — HST chung thử 7 lần đều `no_portal`, cổng -tt78 HCM chưa lấy được PDF nào qua job),
VETC (chỉ thu phí 0107500414, 7), Visnam (Ngân Phi, 7), My Software (DHN, 2), iPOS (Làng Mèo, 1, qua link mail).
Người bán mới của nhóm "1 người bán" có thể cần cổng riêng (`no_portal`). Lỗi còn lại chỉ là tạm thời
(timeout/HTTP 520, tự thử lại).

Cách thêm 1 NCC:

1. Viết `pdf_providers/<ten>.py`, kế thừa `PdfAdapter`, khai báo `mst_tcgp`, cuối file gọi `register()`.
2. Import module đó trong `pdf_providers/__init__.py`.
3. Viết test offline `tests/test_pdf_<ten>.py`.

Lượt `tick`/`pdf` kế tiếp tự đưa các HĐ `no_source` của NCC đó quay lại hàng đợi.

Quy tắc khi viết adapter:

- Không tìm được HĐ thì phải ném `PdfFetchError` với kind đúng (`no_pdf`, `no_code`, `no_portal`,
  `captcha`, ...). Không bao giờ trả về bytes không phải PDF.
- Luôn gọi `check_pdf_is_invoice` trước khi trả PDF.
- `solve_captcha` luôn phải chỉ định nhóm model (xem 1.3).

**Chưa làm được** (khi bổ sung thì cập nhật bảng trên):

- ASIA 0101162173 (asiainvoice.vn/tra-cuu): captcha 150x30 đọc được bằng ddddocr + `.upper()` (22/30 qua),
  nhưng tra bằng **FKey** chỉ có trong mail ASIA gửi; XML Thuế không có (thử MCCQT: không ra). Viết adapter khi có mail ASIA.
- **HT, EFY, FPT — cổng tra cứu có reCAPTCHA (03/11): FPT đã lấy được; HT, EFY đang nghiên cứu** (quá trình nghiên cứu,
  khách hàng yêu cầu lấy HĐ).
  Kiểm 23/10 (gửi tra cứu bằng mã thật, để trống token, 2–3 lần/cổng): server cả 3 đều kiểm token thật.

  | NCC | MSTTCGP | Cổng tra cứu | reCAPTCHA | Server trả khi thiếu token |
  | --- | --- | --- | --- | --- |
  | HT | 0315638251 | laphoadon.htinvoice.vn (`POST /TraCuu/TimTheoMaTraCuu`) | v2 | `"Xác thực không thành công. Recaptcha"` |
  | EFY | 0102519041 | ihoadon.vn/kiem-tra (`POST /kiem-tra/check`) | v2 Enterprise | `VE001 "Mã xác minh không đúng"` (mã bịa cũng vậy → chặn trước khi tra) |
  | FPT | 0104128565 | tracuuhoadon.fpt.com.vn (`GET /api/search?...&token=`) | v3 | HTTP 500 `invalid-input-response` (server gọi siteverify của Google) |

  **FPT/HT/EFY: tìm cách xử lý hóa đơn** (03/11, theo user: khách hàng yêu cầu, phục vụ nghiên cứu). Chỉ tra HĐ
  của chính công ty bằng mã tra cứu thật, nhịp chậm. Thử theo thứ tự: (1) mail — XML/PDF đính kèm (mục 2.7);
  (2) trình duyệt (Playwright, `pdf_providers/fpt.py` là bản thử cho FPT: mở `?mst=&sec=`, bắt JSON `/api/search`
  rồi trích PDF); (3) người tự tra trên trình duyệt rồi nạp PDF vào (lệnh `import-pdf`, đang đề xuất);
  (4) xin NCC kênh chính thức (API / tài khoản khách hàng) nếu số HĐ lớn.
  **Kết quả 03/11:** FPT 4 (C25TAB 943/1128/1129/1156 của Samsung SDS, mã `sec` từ mail; JSON `/api/search` có sẵn
  `pdf` base64 + `xml` + `form/serial/seq/stax/idt`), HT 2, EFY 6 (người tích reCAPTCHA, script bắt PDF + kiểm số HĐ
  + lưu đúng thư mục) → ghi `invoice_pdf.source_id` = `fpt`/`ht`/`efy`. Còn thiếu vì **không có mã ở đâu**: FPT 13 HĐ
  C23TAB 2023, EFY 3 HĐ (0317582981 số 57; 3703125142 số 20, 50). XML FPT/EFY không có mã tra cứu — mã chỉ có trong mail.
  **FPT: đã nối vào job** — adapter `fpt` (`pdf_providers/fpt_import.py`): người chạy script trình duyệt
  `pdf_providers/fpt.py` (`?mst=<MST bán>&sec=<mã mail>`) và lưu JSON `/api/search` vào
  `<INVOICE_OUTPUT_DIR>/manual/fpt/*.json`; lượt `pdf`/`tick` kế tiếp: `_prepare_queues` thấy JSON chưa dùng →
  đưa HĐ FPT `no_portal` về hộp chờ → adapter khớp (MST bán, mẫu, ký hiệu, số), kiểm số HĐ, ghi `invoice_pdf`,
  chuyển JSON sang `used/`. Chưa có JSON của HĐ → `no_portal` (nằm chờ, không tốn lượt). Job không tự mở cổng FPT.
  **HT, EFY: đang nghiên cứu.** `pdf_providers/manual_portals.py` khai báo 2 NCC này (source_id `ht`/`efy`, MSTTCGP,
  cổng, tình trạng); `fetch_pdf` ném `no_portal` cho tới khi có adapter.
  `fpt`/`ht`/`efy` **cố ý không khai `hosts`** để link mail giữ `new` chứ không thành `ignored` vĩnh viễn. Adapter `fpt`
  nhận MSTTCGP 0104128565 nên HĐ Fahasa (FPT tự host) trong XML cũng chờ JSON; Fahasa vẫn lấy qua link mail (`fahasa`).
  Cùng ngày: BKAV 35/35 HĐ thiếu PDF (task `pending`, `attempts=0` — worker PDF chưa chạy tới, adapter `bkav` không
  lỗi) và NC9 C23TVN-424 (`ameinvoice`) cũng ghi `invoice_pdf` → BKAV đủ 501/501.
- TS24 0309478306 (tracuu.xuathoadon.vn, Cloudflare Turnstile): cùng lý do — lấy qua mail.
- **Khảo sát 23/10, không làm:** MobiFone 0100686209 (+ -002, 61 HĐ hthdon=2): cổng `tracuuhoadonvienthong.mobifone.vn`
  không captcha, tra bằng "số bảo mật" (`/Tracuu/PrintInvoicePdf?sobaomat=`), nhưng không HĐ nào của mình có mã này
  (GDT chỉ có `mkhang`) và mail cước 2026 không về hộp einvoice@ → cần MobiFone gửi mail thông báo HĐ.
  EVN HCMC Thủ Đức 0300951119-011 (10 HĐ): HĐ tiền điện ở `cskh.evnhcmc.vn` phải đăng nhập tài khoản khách hàng
  (mã PE12000248547); không có mail EVN. equanlyhoadon.vn (~300 link) = trang quảng cáo của BKAV, không có mã
  (mã MTC nằm ở link `tracuu.ehoadon.vn` cùng mail, đã xử lý bằng `bkav`). M-Invoice: xem đoạn "Link trong mail".
  (FAST, VNISC, Thái Sơn, VI NA, PA Việt Nam: đã làm 23/10, xem bảng trên.)
- WEBCASH/wetax 1201496252 (HĐ bán ra của chính công ty): cổng chỉ tra bằng mã riêng, mã này không có trong XML.
- HILO GSM (cổng chung dùng reCAPTCHA, tra không ra) và VETC phí đỗ xe 0106858609 (chỉ xem
  được trong app): cả hai nằm chờ ở `no_portal`. PDF của GSM đã lấy được qua mail.
- CMC Telecom (cinvoice.cmctelecom.vn, mã `CTEL.<32 hex>` trong mail):
  server **kiểm token reCAPTCHA thật** → không làm, lấy qua mail (không vượt reCAPTCHA). Nếu bỏ reCAPTCHA: CMC chỉ cần
  `POST /v1/invoice/lookup {invoiceNo, recaptchaToken}` → `{pdfContent, xmlContent}`.

**Nguyên tắc:** chỉ viết adapter cho NCC có hóa đơn thật để thử (XML từ Thuế hoặc mã trong mail). Muốn thêm NCC
→ lấy thêm HĐ cũ (`main_auto.py --from ... --to ...`), xem `status` NCC nào nhiều HĐ nhất.

**Link trong mail → adapter:** adapter khai báo `hosts` + `invoice_from_link(url, code, mail_text)` — chỉ đọc
tham số link / mã trong mail, **không mở link**; `fetch_pdf` (và `fetch_xml` nếu có) gọi endpoint của chính adapter.
Đã có: misa, visnam, viettel, easyinvoice (giải mã `token`), ipos, vnpt (-tt78), bkav (23/10: link
`tracuu.ehoadon.vn/<MTC>` → GET `tchd.ehoadon.vn/TCHD?MTC=` có sẵn `ViewInvoice('<GUID>')` = `dlhdon_id`, không gửi
form nên không đụng reCAPTCHA; thật 8/8; link trang chủ / `Login?AGUID=` → `ignored`). Lượt `mail` đầu sau khi thêm sẽ
xử lý ~400 link BKAV một lúc (2–3 request/link). M-Invoice: link trong mail chỉ là link theo dõi Mailgun / trang chủ,
không có khóa HĐ (124/126 mail đã kèm sẵn PDF+XML) → không làm.
Thêm host 26/10: `hoadon.petrolimex.com.vn` → `vnpt` (mã = Fkey, thật 5/5); `tracuuhoadon.vetc.com.vn` → `vetc` (mã 12 ký tự
phân biệt hoa/thường + số HĐ từ tiêu đề, 4/4); `sinvoice.vn`/`sinvoice.viettel.vn` → `viettel` (link chỉ là trang chủ, API cần
MST bán mà tiêu đề chỉ có **tên** → bảng `MAIL_SELLERS` tên → MST, hiện chỉ An Yang 0318949262, 11/11); `tt78hd.hilo.com.vn`
→ `hilo`. Chỉ nhận host để đánh `ignored`: `hoadon.thaco.com.vn` (27 link cùng 1 URL cố định ở chân mail → 1 HĐ 2019) và
`tracking.hilo.com.vn` (link đếm click quảng cáo). **`invoice_from_link` trả None = link `ignored` vĩnh viễn**: thêm người bán vào
`MAIL_SELLERS` thì phải đặt lại link cũ về `new` bằng tay. Chưa làm: `0105987432999hd.softdreams.vn` (PINK HEO, host chết,
cổng EasyInvoice cần MST người bán chưa biết).
**27/10, theo file user kiểm tay `output/mail/link_can_kiem_tay_20261027.xlsx`** (kết quả từng host: `..._xuly.xlsx`):
`mail_reader` bắt thêm mã "Mã tra cứu nhanh", "Số bảo mật" (không có chữ "Mã"), "mã kiểm tra X" (không dấu `:`), tham số
`maKTra`; và **MST người bán** ghi trong mail (`extract_seller_msts`, chỉ nhãn rõ là bên bán) → `email_messages.stats.seller_msts`
→ `process_links` thêm dòng `MST người bán: X` vào `mail_text`. Nhờ đó: `minvoice` nhận link `tracuuhoadon.minvoice.vn`
(MST bán + số bảo mật, thật 2/2); `vnpt` nhận `invoice-noibai.vietnamairport.vn` → cổng `han-tt78` theo mã tra cứu nhanh
(`LINK_HOST_PORTALS`, thật 1/1 — link PdfDownload 2023 đã hết hạn, trả trang "Nhập tổng tiền") và host `-tt78` có dấu `_`.
**Link mail Viettel `vinvoice.viettel.vn/tracuu?tax-code=&secret-code=` (03/11):** `secret-code` 32 hex của link KHÔNG tra
được (`downloadPDF` báo không tìm thấy, `downloadPDFForMail` của trang /tracuu trả `query.invoice.not.valid`); "Mã số bí mật"
15 ký tự in trong thân mail thì tra được (4/4) → adapter ưu tiên mã đó. 95 link đã đặt lại `attempts=0`.
CMC `invoice.cmctelecom.vn/outputs/CTEL….pdf` (10 link 2024): host cũ không còn trả lời → không làm.
Cổng Genuwin (`genuwinsolution.com`, TTKhac `Gkey` + `PortalLink`): tên miền không còn phân giải → 2.161 HĐ bán ra
Webcash 2023-01 → 2024-06 + 12 HĐ mua (Genuwin D&C, Paris Baguette) không lấy PDF tự động được; xin team WeTax nội bộ.
**Mail đã xử lý trước 27/10 không tự có mã/MST mới** (stats lưu lúc đọc mail) → phải sửa `stats` + đặt lại link về `new`.
Link EFY (`tracuu.ihoadon.vn`), FPT `?mst=&sec=` (reCAPTCHA, server kiểm): đang tìm cách xử lý, xem mục HT/EFY/FPT.
Không làm: CMC, HDBank (OTP), Grab Zendesk, FPT Shop;
ABBank (captcha ảnh, HĐ của Genuwin 1201547669 không phải công ty mình), Vingroup (captcha, chưa model).

## 2.7 Mail hóa đơn (`mail_reader.py`, `mail_ingest.py`)

- Hộp mail công ty chạy **Hanbiro tự host** (`mail.webcashvietnam.com`), IMAP SSL cổng 993, đăng nhập
  user/password. Không có `INVOICE_MAIL_USER` thì `tick` bỏ qua bước mail. Mẫu đầy đủ: `.env.example`.
- **Công tắc `INVOICE_MAIL_AUTH`** (chỉ đổi cách đăng nhập, phần xử lý giữ nguyên):
  - `password` (mặc định): mail tự host; Gmail/Yahoo dùng App Password.
  - `oauth2_microsoft`: Microsoft 365, đã tắt IMAP đăng nhập bằng mật khẩu. Cần `INVOICE_MAIL_MS_TENANT_ID/CLIENT_ID/CLIENT_SECRET`;
    token app-only, quyền `IMAP.AccessAsApp`.
  - `oauth2_google`: Workspace chặn App Password. Cần `INVOICE_MAIL_GOOGLE_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN`.
  - Thiếu biến của chế độ đã chọn → lệnh `mail` báo thiếu biến nào, còn `tick` bỏ qua bước mail.
  - Không hỗ trợ POP3.
- `INVOICE_MAIL_FOLDER` nhận **nhiều thư mục**, cách nhau dấu phẩy (hiện `INBOX,HDDT`: user đã tạo bộ lọc
  chuyển mail HĐ sang `HDDT`). Tên thư mục có dấu được mã hóa modified UTF-7. Mail bị chuyển thư mục được nhận
  ra theo Message-ID trong cùng tài khoản, nên không xử lý lại.
- Hiện đọc hộp cá nhân `the.lai@` (mail được forward về, 7 ngày gần nhất). Sau này chuyển sang đọc thẳng
  hộp `einvoice@` (chỉ cần đổi user/password).
- **Chỉ đọc**: EXAMINE + BODY.PEEK (không đánh dấu đã đọc, không xóa). Mail không phải HĐ chỉ lưu
  Message-ID, không lưu tiêu đề hay người gửi. **Không bao giờ tự mở link trong mail.**
- Nhận ra mail HĐ **theo nội dung**: mail forward mất người gửi gốc, và không giữ `einvoice@` ở To/Cc.
- Luồng xử lý:
  1. XML đính kèm (kể cả trong .zip / .eml) → khớp HĐ theo (MST bán, mẫu, ký hiệu, số).
     HĐ chưa có XML (hthdon=2) → lưu với `nguon='xml_email'`. HĐ đã có XML từ Thuế → không ghi đè.
  2. PDF → ghép với XML cùng mail, hoặc dò MST + ký hiệu + số HĐ trong chữ của PDF
     → lưu `invoice_pdf.source_id='email'`.
  3. Chưa khớp được → `output/mail/unmatched/`; mỗi lượt khớp lại.
  4. Link + mã tra cứu → bảng `email_links` → `process_links`: adapter theo host (xem 2.6) tải PDF/XML,
     khớp HĐ như file đính kèm. Link lỗi thử lại tối đa 3 lần (`attempts`); host chưa có adapter thì để `new`.
     **Mail đã kèm PDF hóa đơn thì không gọi cổng** (27/10): link → `mail_file`, dùng file trong mail (kể cả PDF đang
     `unmatched`, lượt sau tự khớp). Lượt đầu chuyển 1.632 link backlog sang `mail_file`. Link hết 3 lần do lỗi môi trường
     (vd `WinError 1114` nạp torch) phải đặt lại `attempts=0` bằng tay.
- Lần chạy đầu (14/10, 42 mail): 9 PDF + 2 XML hthdon=2 lấy từ mail, trong đó có PDF của những
  NCC adapter chưa làm được (GSM, Jollibee, VNISC).
- **Loại mail** (kiểm 20/10 trên hộp thật, quét thử chỉ đọc): XML + PDF đính kèm (cả trong .zip, mail forward
  dạng .eml/rfc822) → xử lý được; chỉ có link + mã → được với EasyInvoice, MISA, Viettel, iPOS, VNPT -tt78,
  BKAV (`tracuu.ehoadon.vn/<MTC>`), 247Express, VNISC, Thái Sơn, SmartSign, FAST (bổ sung 23/10);
  còn CMC/FPT/HILO GSM (reCAPTCHA) để `new`. **Không đọc được:** .rar/.7z, ảnh chụp HĐ, Excel, file để trên
  Google Drive/OneDrive, file > 20 MB. Zip có mật khẩu/hỏng chỉ bỏ đúng file đó (sửa 20/10, trước đây cả
  mail báo lỗi). PDF HĐ thay thế/điều chỉnh có cả số HĐ gốc → bỏ số HĐ gốc khi khớp (sửa 20/10).
- Hộp cá nhân `the.lai@` có nhiều mail nội bộ "[Webcash Việt Nam] BÀN GIAO DATA HÓA ĐƠN PHẦN MỀM WeTax…"
  (ảnh + xlsx + link Google Drive): tiêu đề có "hóa đơn" nên bị tính là mail HĐ, không có gì để lấy →
  `no_file`. Vô hại, hết khi chuyển sang hộp `einvoice@`.
- **Cửa sổ 7 ngày:** mail không được quét trong 7 ngày kể từ lúc nhận thì bị bỏ qua luôn. Chưa có lịch chạy
  (Task Scheduler/server) thì phải chạy `mail`/`tick` ít nhất mỗi tuần, hoặc tăng `INVOICE_MAIL_DAYS`
  (mail đã xem chỉ đọc lại header Message-ID nên cửa sổ rộng vẫn nhẹ).

## 2.8 Khảo sát cổng tra cứu (`docs/Danh_sach_NCC.xlsx`)

Sheet "Chi tiết", cột M–AF (14/10/2026; user đã xóa 1 cột trống nên dịch trái 1 cột — script đọc file nên
dò cột theo tên header): với 117 NCC có link, ghi form tra cứu mặc định cần những tham số gì (mỗi tham số
1 cột, đánh ✓), kèm captcha chi tiết, cách tra khác, ghi chú và URL form thật. Sheet "Tổng quát" từ dòng 30:
5 bảng tổng hợp bằng công thức COUNTIF(S) trỏ sang "Chi tiết" (tự cập nhật khi sửa ✓).
Chỉ đọc form, không gửi tra cứu thử → captcha chỉ hiện sau khi bấm tìm có thể bị sót.
Kết quả: 107 OK; 49 site chỉ cần mã tra cứu, 27 có captcha ảnh, 13 captcha dạng tích.

## 2.9 Dựng trên máy chủ mới

Thứ tự đúng (mỗi bước đều kiểm được, đừng bỏ bước kiểm):

1. **Cài Git LFS TRƯỚC khi clone** (`git lfs install`). Clone trước rồi mới cài thì `runs/*/best.pt`
   chỉ là file trỏ vài trăm byte — nạp lên sẽ lỗi khó hiểu chứ không báo thiếu file.
   Kiểm: `git lfs ls-files` phải ra **5 file** (`g_600x120`, `g_150x34`, `g_80x30`, `g_1500x350`,
   `g_200x70/h32_89pct`) và mỗi `best.pt` phải ~25 MB.
2. Python 3.13 + `pip install -r requirements.txt -r requirements-invoice.txt` + `playwright install chromium`
   (bắt buộc: adapter VNPT dựng PDF từ HTML).
3. **`py -X utf8 scripts\check_setup.py`** (viết 21/10) kiểm hết trong 1 lần: Python, 13 thư viện,
   4 model production còn nguyên hay chỉ là con trỏ LFS, nạp model thật, Playwright chromium,
   `.env` có đủ khóa chưa (**chỉ báo có/không, không in giá trị**), và kết nối PostgreSQL.
   Thoát mã 1 nếu còn lỗi. Chạy trước `init-db`.
   4 model job cần: `g_600x120` (đăng nhập GDT), `g_150x34` (easyinvoice), `g_80x30` (vnpt),
   `g_1500x350` (visnam). Adapter mới dùng nhóm khác thì phải thêm `!runs/<nhóm>/` vào `.gitignore`
   **và thêm vào `PROD_MODELS` trong script**, nếu không repo không mang model đó theo.
4. `.env` copy từ `.env.example`, điền `DATABASE_URL`, `HDT_PASSWORD_<MST>`, phần `INVOICE_MAIL_*`.
   **Không có trong repo**, phải tạo tay trên từng máy.
5. `init-db` → `add-company --mst ... --username ...` → chạy thử `status` (không đụng GDT) rồi
   `xml --limit 5`.
6. Lịch chạy: `scripts\register_invoice_tasks.ps1`. **Trên máy chủ bắt buộc thêm `-RunWhenLoggedOff`**
   (thêm 21/10) — mặc định task đăng ký kiểu Interactive, tức **chỉ chạy khi user đang đăng nhập**,
   nên trên server không ai ngồi thì job im lặng không chạy. Cờ này dùng `LogonType S4U` (chạy cả khi
   đã đăng xuất, không phải lưu mật khẩu).
   Mô hình hàng đợi (2.4) thì dùng `scripts\register_queue_tasks.ps1 -RunWhenLoggedOff` thay cho script trên.
   **Tạo task tay trong GUI dễ sai** (đã dính trên server 22/10, task treo "Running" mãi không log,
   không dòng DB): ô Program/script phải là `pythonw.exe` thật — **không phải file `.py`** (Windows mở
   theo file association, phiên ngầm treo vĩnh viễn) và **không phải `...\WindowsApps\python.exe`**
   (alias Microsoft Store, cũng treo); Arguments `"<dự án>\web\invoice_job.py" <lệnh>` có nháy;
   Start in = thư mục dự án, **không nháy** (có nháy → `0x8007010B` "directory name is invalid").
   Kiểm bằng `(Get-ScheduledTask -TaskName X).Actions | Format-List Execute,Arguments,WorkingDirectory`.

Thứ **không** có trong repo và không cần cho job: `env/` (dữ liệu train), `env_pass/`, `output/`,
`example/`. Job chỉ cần `configs/groups.yaml` + `runs/<nhóm>/best.pt`.
Nhớ **cửa sổ mail 7 ngày** (mục 2.7): lịch 3 lần/ngày là dư, nhưng nếu server tắt quá 1 tuần thì
mail trong khoảng đó mất luôn — tăng `INVOICE_MAIL_DAYS` nếu dự kiến gián đoạn dài.

## 2.10 Việc còn mở

- Lấy thêm HĐ cũ (tháng 7–8) bằng `main_auto.py` để có dữ liệu thật làm adapter NCC mới.
- Test DB (`tests/test_invoice_store.py`) chưa chạy trên DB thật.

<!-- Bổ sung thêm bên dưới -->

---

# PHẦN 3 – WEINVOICE (UI SPEC v1.0)

Đặc tả sản phẩm **WeInvoice** do PO WeCMS bàn giao 30/10/2026 (file Roadmap 02/11/2026). PHẦN 2 là nguồn dữ liệu
(engine + nghiên cứu P0) cho sản phẩm này. **Giao diện + quy tắc nghiệp vụ (file 02) là bắt buộc; kiến trúc, CSDL,
API, engine do Dev tự quyết.** Thấy vô lý / không khả thi → phản hồi PO, PO ra bản spec mới (v1.1…).
Chỉ sửa bộ spec khi user yêu cầu, và ghi lại ở mục "Đã sửa" bên dưới (2 file zip giữ nguyên bản gốc v1.0).

**Đã sửa so với bản v1.0 (02/11/2026, theo user):**
- Danh mục NCC 138 → **144** (`weinvoice-web/lib/providers.ts` stt 139–144, `02` mục 3.4): THACO 3600252847,
  VETC thu phí 0107500414, FPT Retail 0311609355, Genuwin 0304248595, Paris Baguette 0310634327 (phần mềm Genuwin),
  Thế Giới Di Động 0306731335 (tên pháp nhân + trang tra cứu còn phải xác nhận).
- Tiền tệ: `Invoice.currency` từ `"VND" | "USD"` thành mã ISO 4217 bất kỳ theo `DVTTe` (`VNĐ` → `VND`); `02` mục 3.3.
- Thuế suất **KKKNT**: `vatRate = -2` (KCT vẫn `-1`), hàm `vatRateLabel` trong `lib/types.ts` dùng chung cho
  `InvoiceSheet`, `lib/xml.ts`, `lib/api/checks.ts`; nhãn `risk.rate_note` VI/EN/KO; `02` mục 3.3 và 3.7.
  Chưa chạy `tsc` (máy không có `node_modules`).
- File Roadmap: VNISC **0103018807 → 0102331762** ở cả 7 ô (sheet NCC theo Phase, Roadmap-WBS-Function, P0-Nghien cuu).
  0102331762 (invoice.abcsys.vn) mới là NCC đã xong 2 kênh.

## 3.1 Bộ tài liệu

| File (`docs/`)                                      | Nội dung                                                                                                                                                                                      |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WeInvoice_UI_Spec_v1.0/00_DOC_TRUOC.md`            | Đọc trước: phải theo gì / Dev tự quyết gì, cách chạy prototype                                                                                                                                |
| `WeInvoice_UI_Spec_v1.0/02_Yeu_cau_nghiep_vu.md`    | Phạm vi P1 + quy tắc nghiệp vụ bắt buộc (mục 3.1–3.10 của file đó)                                                                                                                            |
| `WeInvoice_UI_Spec_v1.0/01_Dac_ta_UI.html`          | 19 màn hình: mục đích, thành phần, thao tác + 38 ảnh. Nặng 3,6 MB vì ảnh base64 → mở bằng trình duyệt; muốn đọc chữ thì lọc bỏ `data:` trước, **đừng Read cả file**                          |
| `WeInvoice_UI_Spec_v1.0/weinvoice-web/`             | Mã nguồn UI (Next.js 16, React 19, Tailwind 4), logic đã gỡ thành stub `TODO(dev)`. `lib/types.ts` = dữ liệu UI cần, `lib/providers.ts` › `PROVIDERS` = danh mục 144 NCC, `lib/i18n` = nhãn VI/EN/KO. Bản đã sửa (xem trên), khác zip |
| `WeInvoice_UI_Spec_v1.0/chay_prototype.bat` + 2 zip | Prototype chạy không cần cài (`demo`/`demo1234`, cổng 3001), dữ liệu bên trong là mô phỏng. Bat giải nén vào `WeInvoice_Prototype/` và ghi `data/state.json`                                 |
| `WeInvoice_Roadmap_WBS_Function_v1.0.xlsx`          | Roadmap P0–P7, WBS/Gantt, Function List 117 chức năng (66 thuộc P1), sheet "NCC theo Phase" (P1 = 27 NCC)                                                                                     |

Git chỉ nhận bản bàn giao + `weinvoice-web/`. `.gitignore` bỏ 2 bản giải nén tay (`WeInvoice_Prototype_v1.0/`,
`WeInvoice_UI_Source_v1.0/`, trùng 100% zip), thư mục bat sinh ra và mọi file trong `TestData/` (dev hay bỏ XML/PDF
thật vào để thử prototype; dữ liệu thật không lên repo).

**P1: dev 02/11 → demo 23/12/2026**, Webcash + 27 NCC đã xong cả 2 kênh (XML + email). Màn "lộ trình" (kê khai GTGT,
bảng kê, xuất theo mẫu, phụ lục giảm thuế, nối PDF, gói) chỉ cần trang giới thiệu. Theo Function List, kiểm tra HĐ,
rủi ro NCC, hạch toán, tờ khai đã nộp thuộc P3–P6; Partner API cho nút "Update" của WeCMS (thay Coocon) là P1 Must.

## 3.2 Ánh xạ spec ↔ DB

| Spec (`lib/types.ts`, file 02)                                                    | DB hiện tại                                                                                         | Ghi chú                                                                                                                       |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Khoá loại trùng: MST bán + mẫu + ký hiệu + số (bỏ số 0 đầu)                       | `UNIQUE (company_mst, nbmst, khmshdon, khhdon, shdon)`                                              | `shdon` BIGINT nên số 0 đầu đã mất. Thêm `company_mst`: HĐ giữa 2 MST cùng quản lý thành 2 dòng, hợp với việc lọc theo MST làm việc |
| `direction` purchase / sales                                                      | `direction` mua / ban                                                                               |                                                                                                                               |
| `status` gốc / thay thế / bị thay thế / điều chỉnh / bị điều chỉnh / đã huỷ       | `tthai` 1 / 2 / 4 / 3 / 5 / 6                                                                       |                                                                                                                               |
| `relatedId` (thay thế ↔ bị thay thế)                                              | `parsed.thong_tin_chung.hoa_don_lien_quan` (XML), `list_raw.khhdgoc/shdgoc` (API JSON)              | Không có cột, phải nối lúc đọc                                                                                                |
| `cqtCode`                                                                         | `parsed.ma_cqt`, `list_raw.mhdon`                                                                   |                                                                                                                               |
| `provider` + `providerTaxId`                                                      | `invoice_xml.mst_tcgp` (thiếu thì `list_raw.msttcgp`) → tra `PROVIDERS`                             | `einvoice_providers` đã điền 03/11: `name` = tên ngắn trong `PROVIDERS` (46/47, thiếu 0314420012), `source_id` = adapter job tự chọn theo MSTTCGP (37/47) — chỉ điền ô trống |
| `lookupCode` / `lookupUrl`                                                        | `invoice_xml.ma_tra_cuu` / `lookup_url`                                                             | BKAV tra bằng `parsed.dlhdon_id`                                                                                              |
| `sellerAddress` / `buyerAddress`                                                  | `parsed.nguoi_ban/nguoi_mua.dia_chi`, `list_raw.nbdchi/nmdchi`, `partners.address`                  |                                                                                                                               |
| `items[].vatRate` (0/5/8/10, −1 = KCT)                                            | `invoice_details.thue_suat` TEXT                                                                    | Có thêm `KKKNT`, `KHAC:x%`                                                                                                    |
| `files[]`, `origin` gdt / vendor / email / upload                                 | `invoice_xml` (XML từ mail: `invoice_details.nguon='xml_email'`), `invoice_pdf.source_id` (`email`, `email:<adapter>`, `<adapter>`) | Không có tệp đính kèm khác, không có nguồn `upload`                                                       |
| `GdtConnection`                                                                   | `companies` + `gdt_sessions` + `job_run_companies.status`                                           | Không có nhãn; mật khẩu ở `.env`                                                                                              |
| `SyncRun`                                                                         | `job_runs` + `job_run_companies.stats` (`new_invoices`, `xml_ok`, `failed_chunks`…)                 | Không có số "cập nhật", không lưu kỳ từ–đến                                                                                   |
| `EmailMessage`                                                                    | `email_messages` + `email_files`                                                                    | Mail không phải HĐ không lưu tiêu đề / người gửi                                                                              |
| Tag, người dùng, tài khoản con + quyền, hộp thư theo khách, địa chỉ nhận mail theo MST | —                                                                                              | Chưa có                                                                                                                       |

## 3.3 Dữ liệu thật so với spec (đo 02/11/2026, chỉ đọc DB)

3.516 HĐ của 1 MST (1201496252): mua 2.502, bán 1.014.

- **Đã đáp ứng:** ngày lập, MST/tên bên bán, tên bên mua, tổng tiền 3.516/3.516; hướng HĐ khớp MST 100%; HĐ có mã (C)
  có XML 2.758/2.758 và mã CQT 2.758/2.758; 45/61 HĐ thay thế/điều chỉnh nối được HĐ gốc trong DB, cả 45 HĐ gốc mang
  đúng `tthai` 4/5. Khối "Tình trạng dữ liệu" của Tổng quan lúc này: HĐ có mã thiếu XML 0, HĐ không mã chưa có XML 553,
  HĐ chưa có PDF gốc 1.911.
- **Thủng theo kỳ:** không có HĐ nào trong 2024-05 → 2024-12 và 2025-02 → 2026-02; bán ra chỉ có 2023-01, 2023-07 → 11,
  2026-03 → 10. `sync_cursors` chỉ giữ 1 mốc `synced_to` mỗi loại (cả 6 loại đều 02/11/2026) nên **không trả lời được
  "kỳ nào chưa lấy đủ"** (banner của spec) → cần lưu độ phủ theo (MST, loại, tháng). 903 XML trong mail chưa khớp đều là
  HĐ mua của công ty, phần lớn 2024 (345) và 2025 (450): spec sẽ "Tạo mới", còn mình để `unmatched` vì HĐ chỉ được tạo
  từ danh sách GDT.
- **PDF gốc 1.605/3.516:** mua có MSTTCGP 1.261/1.575 (80%), mua không có MSTTCGP 338/927, **bán ra 6/1.014** (Webcash
  tự phát hành qua WeTax; adapter `wetax` chỉ chạy qua link mail).
- **"Phát hành qua":** 2.507 HĐ ra tên NCC trong danh mục 138. 82 HĐ có MSTTCGP ngoài danh mục 138 (đã thêm vào,
  danh mục nay 144): THACO 3600252847 (58), VETC thu phí 0107500414 (11), FPT Retail 0311609355 (6), Genuwin 0304248595
  (4), Paris Baguette 0310634327 (2), 0306731335 (1, người bán Thế Giới Di Động). 927 HĐ không có MSTTCGP: 553 HĐ không
  mã chưa có XML, 323 HĐ MTT (adapter vẫn định tuyến theo host / NCC quen, 319 có PDF), 51 khác.
- **Trạng thái:** 19 HĐ "Hóa đơn đã bị xóa bỏ/hủy bỏ" lấy từ file Excel có `tthai` NULL vì `invoice_list_excel._TTHAI`
  chỉ có "hóa đơn đã bị hủy" → mất trạng thái "Đã huỷ" (spec dùng để lọc, hạch toán bỏ qua HĐ huỷ).
- **Trạng thái không bao giờ cập nhật:** `upsert_invoices` là `ON CONFLICT DO NOTHING` → HĐ đã lưu rồi mới bị thay thế /
  điều chỉnh / huỷ vẫn giữ trạng thái cũ. Dữ liệu hiện chưa dính, nhưng spec cần (nhãn trạng thái, "HĐ biến động",
  số "cập nhật" trong lịch sử đồng bộ).
- **Tiền tệ:** VND 3.357, `VNĐ` 16 (cần chuẩn hoá), trống 129, USD 11, KRW 3 (spec đã sửa nhận mọi mã tiền tệ).
- **Thuế suất dòng:** 10% 7.768, 8% 5.533, KCT 1.242, 0% 44, 5% 29, KKKNT 13 (spec đã thêm, `vatRate = -2`), trống 104.
- 28 XML trong mail vẫn `unmatched` dù HĐ đã có XML từ Thuế: file tạm đã mất nên `rematch` bỏ qua (`OSError` →
  `continue`) thay vì đánh `duplicate`.

## 3.4 Lệch trong file Roadmap

- ~~VNISC 0103018807~~ đã sửa thành 0102331762 (xem "Đã sửa" ở đầu PHẦN 3). 0103018807 tra ở vninvoice.vn, model
  `g_180x50_vn` chưa dùng được (1.5 mục 3).
- VETC ghi xong 2 kênh, nhưng 8 HĐ của 0106858609 (phí đỗ xe, chỉ xem trong app) đều chưa có PDF; 11 PDF VETC lấy được
  là của 0107500414, mà MST này không có trong danh mục.
- Dữ liệu demo P1 mỏng: iPOS 0104789847 có 0 HĐ mang MSTTCGP này (8 PDF iPOS lấy qua link mail nằm ở HĐ MTT không có
  MSTTCGP), KiotViet 0104359717 chưa có PDF nào trong DB; ASIA 1, Minh Khang 2, Mắt Bão 2, Online VI NA 2, Phương Nam 4,
  Rosy 5, MobiFone 5, VININVOICE 5, Win Tech 7 HĐ có PDF. User sẽ tự tìm thêm dữ liệu cho VETC, iPOS, KiotViet.

## 3.5 Backend WeInvoice (`we-invoice/we-scraping-api`, viết 02/11/2026, chưa commit)

- 05/11: code nằm trên nhánh local `f1_v1_weinvoice_api` (tách từ `origin/sit`, chưa commit/push; stash dự phòng
  `weinvoice-api-2026-11-02` trên `main`). `sit` bắt `.env.*.example` **chỉ có khoá, giá trị trống** → cờ trong
  `WeInvoiceProperties` là `Boolean` (trống = tắt). Deploy SIT: thêm 14 khoá `WECMS_WEINVOICE_*` vào `.env` host
  trước (thiếu khoá = không khởi động được). 13 test fail sẵn có: 12 thiếu `application-local.yml`,
  1 `VatDeclarationLifecycleTest` (fail cả trên `origin/sit` sạch).
- 05/11 (sau khi push `8823f7f` lên `sit`): app **không khởi động** — `WeInvoiceSecretCipher` có 2 constructor mà
  thiếu `@Autowired`. Đã sửa + test `WeInvoiceBeanConstructionTest` (record `docs/bugs/weinvoice-secret-cipher-bean-startup-fix-2026-11-05.md`),
  chờ commit tay. Unit test gọi constructor trực tiếp nên không bắt được lỗi Spring; muốn chắc thì chạy `bootRun`.
- Chạy local (IntelliJ hoặc `bootRun`): cần `.env.local` + `src/main/resources/application-local.yml` (cả 2 gitignore, đã tạo
  05/11; yml import `.env.local`). Seed chứng thư `WECMS_CRYPTO_LEGACY_CERT_SEED` phải bằng giá trị legacy
  (xem `docs/security/ops-secret-rotation-handoff.md` của api, không chép giá trị ra đây); khoá `*_ENABLED` của dispatch là cờ boolean, khoá `*_CRON` thì tắt bằng `-`.
- **MST thuộc người dùng, không thuộc tenant** (sửa 05/11 theo user, record `docs/issues/weinvoice-company-ownership-by-user-2026-11-05.md`):
  mọi bảng `winv_*` khoá theo `owner_id` = `USER_ID` đăng nhập (spec "một tài khoản quản lý nhiều MST"), không có chặn admin.
  Nối MST ↔ `BIZ_KEY` là việc của WeCMS. Schema `weinvoice` đã tạo trên DB engine local 05/11 (144 NCC); `.env.local` bật WeInvoice,
  `WECMS_WEINVOICE_DB_PASSWORD` user tự điền.
- **Thêm / đổi mật khẩu MST = đăng nhập Thuế đồng bộ** (06/11, theo user; record `docs/issues/weinvoice-gdt-login-test-synchronous-2026-11-06.md`
  ở cả api lẫn web): web → api `POST /companies` → engine `POST /internal/gdt/login-test` (`web/internal_api.py`,
  `authenticate_auto` giải captcha `g_600x120`) → đúng mới mã hoá + lưu `CONNECTED`, trả `CompanyItem` ngay; sai → `WBE3027`
  (câu của Thuế), không kiểm được → `WBE3028`; không lưu gì. Bỏ hàng đợi `LOGIN_TEST`. Engine đọc MST `CONNECTED` của
  `weinvoice.winv_cmpn_infm` + giải mã mật khẩu (`weinvoice_bridge.py`, `WEINVOICE_SECRET_KEY` = `WECMS_WEINVOICE_SECRET_KEY`)
  để tự thêm vào `companies` trước mỗi lượt đồng bộ. **Thứ tự mật khẩu (11/11): WeInvoice (DB) → `HDT_PASSWORD_<MST>` → không có thì
  báo thiếu, không gọi Thuế** (`invoice_job.resolve_password`). Đã bỏ `HDT_PASSWORD` chung: nó bị gửi cho mọi MST thiếu khóa riêng
  (kể cả `3603512483-001` vì khóa tìm là `HDT_PASSWORD_3603512483_001`) — vân tay chặn của 3700860461, 3600823561-U002, 3603512483
  đều là mật khẩu chung đó. Giải mã WeInvoice lỗi (sai khóa) → dừng, không lùi về `.env`. 3 project độc lập, dùng chung DB.
  **GDT khoá tài khoản khi sai mật khẩu nhiều lần** → thử bằng tài khoản giả, đừng thử sai với tài khoản thật.

- Repo con có `CLAUDE.md` riêng, rất chặt: tạo record trước khi làm (`docs/features|bugs|issues/...-YYYY-MM-DD.md`), SQL để
  DBA chạy tay, dừng ở READY FOR MANUAL COMMIT, không tự commit/push. Record của đợt này:
  `docs/features/addon-service/weinvoice/weinvoice-api-implementation-2026-11-02.md` (+ `README.md`, `interface-specification.md`).
- Module `src/main/java/com/kosign/wecms/domain/weinvoice/`: 40 endpoint `/api/v1/weinvoice` + Partner API `/api/partner/v1`
  (header `X-Api-Key`). BE nối **thẳng DB của engine** bằng pool riêng (`WeInvoiceDb`, không phải bean `DataSource` — tạo bean
  thì auto-config của Spring lùi lại và các repo JDBC cũ trỏ nhầm DB). Bảng engine chỉ đọc; bảng WeInvoice ở schema `weinvoice`
  (`src/main/resources/db/weinvoice/V1__winv_schema.sql`, 11 bảng) — **chưa chạy trên DB nào**.
- **Worker yêu cầu** (09/11, `web/weinvoice_worker.py`, record `docs/issues/weinvoice-engine-request-worker-2026-11-09.md` của api):
  SYNC theo tháng (danh sách + XML, ghi `winv_sync_cvrg`), FETCH_XML / FETCH_PDF chạy ngay (mã/link nhập tay thắng mã đoán từ XML),
  IMPORT (lô `winv_upld_*` hoặc 1 XML của 1 HĐ; NEW/MERGED/DUPLICATE/FOREIGN/PDF_PENDING/ERROR), TEST_DATA (thư mục TestData).
  **11/11:** HĐ đã có XML/PDF trong DB nhưng **máy đang chạy không có file** (engine máy khác tải) → IMPORT ghi file nạp vào
  đúng `xml_path`/`pdf_path` đã lưu, báo MERGED "Bổ sung file … (máy chủ chưa có file)", không parse lại; có file thì vẫn DUPLICATE.
  **Chỉ chạy `weinvoice_worker.py` trên máy có thư mục lưu trữ của api (.13)**: worker máy khác nhận yêu cầu thì không đọc
  được file tải lên (11/11 laptop nhận #13 → `FileNotFoundError`).
  HĐ tạo từ file tải lên: `list_raw.nguon = upload|test_data`, XML `invoice_details.nguon = 'xml_upload'` (CHECK nới trong
  `db/schema.sql`, áp bằng `init-db`), PDF `source_id = upload|test_data`. MST chưa có tài khoản GDT → dòng `companies` tạm
  `gdt_username='#upload:<MST>'`, `active=false`; khi CONNECTED, `weinvoice_bridge` đổi sang tài khoản thật. Hợp đồng: `README.md`
  §"Engine contract" của module.
- Build trên máy này: Gradle cần JVM 17+ mà máy mặc định Java 11 →
  `JAVA_HOME=$HOME/.gradle/jdks/eclipse_adoptium-21-amd64-windows.2 ./gradlew test --offline`. Bộ test đầy đủ có **13 lỗi sẵn
  có** (`ActuatorEndpointSecurityTest`, `SchedulerConfigurationContractTest`, `OperatingHoursZoneConsistencyTest`) vì thiếu
  `application-local.yml` (gitignore) — có cả trên HEAD gốc, không phải lỗi mới.
- Endpoint mới phải có `@menuAuthz` hoặc nằm trong allow-list của `EndpointAuthorizationCoverageTest` (WeInvoice đang ở allow-list).
- pgjdbc không cho `rs.getObject(col, Long.class)` với cột `numeric` → đọc số có thể null bằng `getLong` + `wasNull`.
