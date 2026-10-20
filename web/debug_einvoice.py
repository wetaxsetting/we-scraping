# debug_einvoice.py
from captcha_sources import make_session
import re

s = make_session()
h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# Bước 1: GET home
print('=== GET home ===')
r1 = s.get('https://tracuutvan.einvoice.com.vn/', headers=h, timeout=15)
print('Status:', r1.status_code)
m = re.search(r'id="CaptchaDeText"[^>]*value="([a-f0-9]{32})"', r1.text)
t1 = m.group(1) if m else None
print('Token T1:', t1)
print('Cookies:', dict(s.cookies))
print()

# Bước 2: POST /Refresh
print('=== POST /Refresh ===')
r2 = s.post(
    'https://tracuutvan.einvoice.com.vn/DefaultCaptcha/Refresh',
    data={'t': t1},
    headers={**h, 'X-Requested-With': 'XMLHttpRequest',
             'Referer': 'https://tracuutvan.einvoice.com.vn/',
             'Accept': 'text/javascript, application/javascript, */*'},
    timeout=15,
)
print('Status:', r2.status_code)
print('Content-Type:', r2.headers.get('Content-Type'))
print('Body len:', len(r2.text))
print('Body[:500]:', repr(r2.text[:500]))
print()

m2 = re.search(r'([a-f0-9]{32})', r2.text)
print('Token T2 tìm được:', m2.group(1) if m2 else 'KHÔNG TÌM THẤY')

# Bước 3: Thử tải ảnh với cả T1 và T2 để so sánh
print()
print('=== Thử tải ảnh ===')
for label, tok in [('T1', t1), ('T2', m2.group(1) if m2 else None)]:
    if not tok:
        print(f'{label}: skip (no token)')
        continue
    url = f'https://tracuutvan.einvoice.com.vn/DefaultCaptcha/Generate?t={tok}'
    ri = s.get(url, headers={**h, 'Accept': 'image/*'}, timeout=15)
    print(f'{label} ({tok}): status={ri.status_code}, len={len(ri.content)}, '
          f'ctype={ri.headers.get("Content-Type")}')
    # Lưu để xem
    with open(f'test_{label}.png', 'wb') as f:
        f.write(ri.content)
print()
print('Đã lưu test_T1.png và test_T2.png — mở xem')