"""
ورود دو مرحله‌ای با TOTP (سازگار با Google Authenticator، Authy، ۱Password…).

پیاده‌سازی مستقیم روی RFC 6238/4226 با کتابخانه‌ی استاندارد پایتون است، چون
کل الگوریتم چند خط HMAC است و افزودن یک وابستگی pip تازه برای آن ارزش ندارد
(این پروژه عمداً وابستگی‌هایش را کم نگه می‌دارد).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
# پنجره‌ی ±۱ گام یعنی کد تا ۳۰ ثانیه قبل/بعد هم پذیرفته می‌شود. این برای
# اختلاف ساعت گوشی و سرور لازم است؛ بزرگ‌ترش کردن، پنجره‌ی حمله را باز می‌کند.
WINDOW = 1
RECOVERY_CODE_COUNT = 8


def generate_secret(length: int = 20) -> str:
    """secret خام تصادفی به شکل Base32 بدون padding — همان چیزی که اپ‌های
    احرازکننده انتظار دارند."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** DIGITS)).zfill(DIGITS)


def now_code(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // PERIOD))


def verify(secret: str, code: str, at: float | None = None) -> bool:
    """مقایسه با compare_digest تا زمان‌سنجی اطلاعاتی لو ندهد."""
    if not secret or not code:
        return False
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return False
    counter = int((at if at is not None else time.time()) // PERIOD)
    for drift in range(-WINDOW, WINDOW + 1):
        try:
            candidate = _hotp(secret, counter + drift)
        except (ValueError, TypeError):
            return False
        if hmac.compare_digest(candidate, code):
            return True
    return False


def provisioning_uri(secret: str, account_name: str, issuer: str = "cplusepro") -> str:
    """otpauth:// — همان چیزی که داخل کد QR می‌رود."""
    label = quote(f"{issuer}:{account_name}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list:
    """کد بازیابی برای وقتی گوشی گم می‌شود. بدون این، کاربری که اپ
    احرازکننده‌اش را از دست بدهد برای همیشه از معامله‌ی واقعی محروم می‌شود."""
    out = []
    for _ in range(count):
        raw = secrets.token_hex(5)          # ۱۰ کاراکتر hex
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def normalize_recovery(code: str) -> str:
    return (code or "").strip().replace("-", "").lower()
