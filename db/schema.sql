-- Schema cho job lấy hóa đơn GDT (XML) + tra cứu bản gốc (PDF).
--
-- Chạy lại nhiều lần vẫn an toàn (IF NOT EXISTS). Áp dụng bằng:
--     python web/invoice_job.py init-db
--
-- Nguyên tắc:
--   * invoices.UNIQUE(...) là thứ chặn lấy trùng: đồng bộ danh sách dùng
--     ON CONFLICT DO NOTHING, nên hóa đơn đã có (kể cả đã xử lý xong) không bị đụng.
--   * tasks là "hộp chờ": mỗi (hóa đơn, loại việc) một dòng. Thử lại = status
--     'pending' với next_run_at ở tương lai, không có trạng thái "retry" riêng.
--   * Task PDF chỉ được tạo khi XML đã xong (cần mst_tcgp + mã tra cứu trong XML).
--   * Tiền dùng NUMERIC, ngày lập lưu theo giờ VN (tdlap của GDT là UTC).

-- Công ty mình quản lý. Mật khẩu GDT KHÔNG lưu ở đây (env HDT_PASSWORD_<MST>).
-- gdt_username tách riêng vì tài khoản con có dạng '1201496252-U002' (khác MST).
CREATE TABLE IF NOT EXISTS companies (
    company_mst  VARCHAR(14) PRIMARY KEY,
    gdt_username VARCHAR(64) NOT NULL UNIQUE,
    name         TEXT,
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cache JWT: còn hạn thì dùng tiếp, hạn chế đăng nhập (= giải captcha).
CREATE TABLE IF NOT EXISTS gdt_sessions (
    company_mst    VARCHAR(14) PRIMARY KEY REFERENCES companies,
    token          TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    obtained_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_at TIMESTAMPTZ
);

-- Đối tác (NCC / khách hàng), suy ra từ XML.
CREATE TABLE IF NOT EXISTS partners (
    mst           VARCHAR(14) PRIMARY KEY,
    name          TEXT,
    address       TEXT,
    phone         TEXT,
    email         TEXT,
    bank_account  TEXT,
    bank_name     TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Đơn vị cung cấp HĐĐT (MSTTCGP trong XML) → adapter tra cứu PDF.
-- source_id NULL = chưa có adapter; task PDF của họ nằm ở 'no_source'.
CREATE TABLE IF NOT EXISTS einvoice_providers (
    mst_tcgp      VARCHAR(14) PRIMARY KEY,
    name          TEXT,
    source_id     TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hóa đơn từ API danh sách.
CREATE TABLE IF NOT EXISTS invoices (
    id             BIGSERIAL PRIMARY KEY,
    company_mst    VARCHAR(14) NOT NULL REFERENCES companies,
    direction      TEXT NOT NULL CHECK (direction IN ('mua', 'ban')),
    sub_type       TEXT NOT NULL,           -- ban | ban-khma | ban-mtt | mua | mua-khma | mua-mtt
    loai_hd        TEXT,                    -- Có mã | Không có mã | MTT
    nbmst          VARCHAR(14) NOT NULL,
    nbten          TEXT,
    nmmst          TEXT,                    -- khách lẻ có thể không phải MST chuẩn
    nmten          TEXT,
    khmshdon       SMALLINT NOT NULL,
    khhdon         TEXT NOT NULL,
    shdon          BIGINT NOT NULL,
    ngay_lap       DATE,                    -- giờ VN
    tien_chua_thue NUMERIC(18, 2),
    tien_thue      NUMERIC(18, 2),
    tong_tien      NUMERIC(18, 2),
    tthai          SMALLINT,
    -- Hình thức HĐ theo API GDT: 0 không mã, 1 có mã, 5 máy tính tiền,
    -- 2 không mã gửi Thuế theo BẢNG TỔNG HỢP → Thuế không có XML từng HĐ
    hthdon         SMALLINT,
    list_raw       JSONB NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_mst, nbmst, khmshdon, khhdon, shdon)
);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS hthdon SMALLINT;     -- DB tạo trước khi có cột này
CREATE INDEX IF NOT EXISTS invoices_company_date ON invoices (company_mst, ngay_lap);

-- Chi tiết dòng hàng hóa/dịch vụ (HHDVu). Ghi lại toàn bộ mỗi lần lưu nguồn mới
-- (xóa dòng cũ của HĐ rồi ghi) nên không bị trùng khi chạy lại.
CREATE TABLE IF NOT EXISTS invoice_details (
    id            BIGSERIAL PRIMARY KEY,
    invoice_id    BIGINT NOT NULL REFERENCES invoices ON DELETE CASCADE,
    line_no       INT NOT NULL,              -- thứ tự dòng trong hóa đơn (1..n)
    stt           INT,
    tinh_chat     SMALLINT,                  -- 1 HH/DV, 2 khuyến mại, 3 chiết khấu, 4 ghi chú, 5 đặc trưng
    ma_hhdv       TEXT,
    ten_hhdv      TEXT,
    don_vi_tinh   TEXT,
    so_luong      NUMERIC(27, 6),
    don_gia       NUMERIC(27, 6),
    tl_ck         NUMERIC(10, 4),
    st_ck         NUMERIC(27, 6),
    thanh_tien    NUMERIC(27, 6),
    thue_suat     TEXT,                      -- 8%, 10%, KCT, KKKNT, KHAC:x% ... (Phụ lục V)
    tien_thue     NUMERIC(27, 6),
    tong_tien     NUMERIC(27, 6),
    ma_nhom_nganh TEXT,
    extra         JSONB,                     -- tt_khac của dòng, hàng hóa đặc trưng, tỷ giá dòng...
    nguon         TEXT NOT NULL DEFAULT 'xml'
                  CHECK (nguon IN ('xml', 'xml_email', 'pdf')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (invoice_id, line_no)
);

CREATE TABLE IF NOT EXISTS invoice_xml (
    invoice_id BIGINT PRIMARY KEY REFERENCES invoices ON DELETE CASCADE,
    xml_path   TEXT NOT NULL,
    xml_sha256 CHAR(64) NOT NULL,
    mst_tcgp   VARCHAR(14) REFERENCES einvoice_providers,
    ma_tra_cuu TEXT,
    lookup_url TEXT,
    parsed     JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoice_xml_tcgp ON invoice_xml (mst_tcgp);

CREATE TABLE IF NOT EXISTS invoice_pdf (
    invoice_id BIGINT PRIMARY KEY REFERENCES invoices ON DELETE CASCADE,
    pdf_path   TEXT NOT NULL,
    pdf_sha256 CHAR(64) NOT NULL,
    source_id  TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hộp chờ.
CREATE TABLE IF NOT EXISTS tasks (
    id          BIGSERIAL PRIMARY KEY,
    invoice_id  BIGINT NOT NULL REFERENCES invoices ON DELETE CASCADE,
    task_type   TEXT NOT NULL CHECK (task_type IN ('xml', 'pdf')),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'dead', 'no_source')),
    attempts    INT NOT NULL DEFAULT 0,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_kind  TEXT,          -- network | server | rate_limit | not_ready | auth | captcha | parse | bad_content | other
    last_error  TEXT,
    locked_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (invoice_id, task_type)
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks (task_type, next_run_at) WHERE status = 'pending';

-- Lịch sử từng lần thử, để tra lỗi.
CREATE TABLE IF NOT EXISTS task_attempts (
    id          BIGSERIAL PRIMARY KEY,
    task_id     BIGINT NOT NULL REFERENCES tasks ON DELETE CASCADE,
    run_id      BIGINT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN NOT NULL,
    error_kind  TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS task_attempts_task ON task_attempts (task_id);

-- Mốc đồng bộ danh sách: lấp khoảng trống khi job nghỉ nhiều ngày.
CREATE TABLE IF NOT EXISTS sync_cursors (
    company_mst VARCHAR(14) NOT NULL REFERENCES companies,
    sub_type    TEXT NOT NULL,
    synced_to   DATE NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_mst, sub_type)
);

-- Mail hóa đơn đã xem (lệnh `mail`). Mỗi mail 1 dòng theo Message-ID → không xử lý lại.
-- Mail KHÔNG phải hóa đơn chỉ lưu Message-ID (không lưu tiêu đề/người gửi — hộp thư cá nhân).
CREATE TABLE IF NOT EXISTS email_messages (
    id           BIGSERIAL PRIMARY KEY,
    mailbox      TEXT NOT NULL,                 -- user/thư mục
    message_id   TEXT NOT NULL,
    received_at  TIMESTAMPTZ,
    subject      TEXT,
    from_addr    TEXT,
    is_invoice   BOOLEAN NOT NULL,
    status       TEXT NOT NULL,                 -- ignored | done | partial | unmatched | links_only | no_file | error
    stats        JSONB,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mailbox, message_id)
);

-- File XML/PDF lấy từ mail. Chưa khớp được HĐ (HĐ chưa lên danh sách GDT) → 'unmatched',
-- mỗi lượt `mail` khớp lại.
CREATE TABLE IF NOT EXISTS email_files (
    id         BIGSERIAL PRIMARY KEY,
    email_id   BIGINT NOT NULL REFERENCES email_messages ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('xml', 'pdf')),
    sha256     CHAR(64) NOT NULL,
    saved_path TEXT NOT NULL,                  -- tương đối so với thư mục output
    nbmst      VARCHAR(14),
    khmshdon   SMALLINT,
    khhdon     TEXT,
    shdon      BIGINT,
    nmmst      TEXT,
    invoice_id BIGINT REFERENCES invoices ON DELETE SET NULL,
    status     TEXT NOT NULL,                  -- matched | unmatched | duplicate | not_ours | not_invoice
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (email_id, sha256)
);
CREATE INDEX IF NOT EXISTS email_files_unmatched ON email_files (status) WHERE status = 'unmatched';

-- Link tra cứu + mã trong mail chỉ có link (chưa tự tải: chờ adapter theo cổng).
CREATE TABLE IF NOT EXISTS email_links (
    id         BIGSERIAL PRIMARY KEY,
    email_id   BIGINT NOT NULL REFERENCES email_messages ON DELETE CASCADE,
    url        TEXT NOT NULL,
    host       TEXT NOT NULL,
    code       TEXT,
    invoice_id BIGINT REFERENCES invoices ON DELETE SET NULL,
    status     TEXT NOT NULL DEFAULT 'new',     -- new | done | failed | ignored
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (email_id, url)
);
-- Bước "mail chỉ có link": dùng mã trong link/mail để gọi adapter của cổng (thử lại tối đa 3 lần)
ALTER TABLE email_links ADD COLUMN IF NOT EXISTS attempts   INT NOT NULL DEFAULT 0;
ALTER TABLE email_links ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE email_links ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- Nhật ký mỗi lần chạy.
CREATE TABLE IF NOT EXISTS job_runs (
    id          BIGSERIAL PRIMARY KEY,
    command     TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT,
    stats       JSONB
);

-- Mỗi lượt chạy × mỗi công ty một dòng: lấy dữ liệu từ Thuế và lấy PDF bản gốc từ NCC
-- đều ghi vào đây. job_runs.stats vẫn giữ bản tổng của cả lượt; bảng này để truy theo
-- MST bằng SQL thẳng thay vì đào JSONB.
CREATE TABLE IF NOT EXISTS job_run_companies (
    run_id      BIGINT NOT NULL REFERENCES job_runs ON DELETE CASCADE,
    company_mst VARCHAR(14) NOT NULL REFERENCES companies,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT,                            -- ok | partial | error
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, company_mst)
);
CREATE INDEX IF NOT EXISTS job_run_companies_mst ON job_run_companies (company_mst, started_at DESC);
