"""
کاربران پنل (چندکاربره) — ذخیره در config/users.json.
همان الگوی config_store.py: فایل JSON مسطح + threading.Lock + نوشتن اتمیک.
هش پسورد با hashlib.pbkdf2_hmac استاندارد پایتون (بدون وابستگی خارجی).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "users.json")
USERS_PATH = os.getenv("USERS_CONFIG_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

PBKDF2_ITERATIONS = 200_000
LINK_CODE_TTL_MINUTES = 15
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

USER_DEFAULTS = {
    "role": "user",              # "admin" | "user"
    "enabled": True,
    "email": None,
    "lang": None,                # None = هنوز انتخاب نکرده؛ از کوکی/مرورگر تشخیص داده می‌شود
    "telegram_chat_id": None,
    "telegram_link_code": None,
    "telegram_link_code_expires": None,
    "notify_telegram": True,
    # تأیید ایمیل
    "email_verified": False,
    "email_code": None,
    "email_code_expires": None,
    "email_code_sent_at": None,
    # ورود دو مرحله‌ای (TOTP) — برای فعال‌کردن معامله‌ی واقعی لازم است
    "totp_secret": None,
    "totp_enabled": False,
    "recovery_codes": [],        # فقط هشِ کدها ذخیره می‌شود، نه خودشان
}

EMAIL_CODE_TTL_MINUTES = 15
EMAIL_CODE_RESEND_SECONDS = 60


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    if not os.path.exists(USERS_PATH):
        return {"users": []}
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "users" not in data:
            return {"users": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"users": []}


def _save(data: dict):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_PATH)


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def list_users() -> list:
    with _lock:
        return list(_load()["users"])


def get_user(user_id: str) -> dict | None:
    for u in list_users():
        if u["id"] == user_id:
            return u
    return None


def get_user_by_username(username: str) -> dict | None:
    uname = (username or "").strip().lower()
    for u in list_users():
        if u["username"].lower() == uname:
            return u
    return None


def create_user(username: str, password: str, email: str = "", role: str = "user") -> dict:
    username = (username or "").strip()
    password = password or ""
    email = (email or "").strip().lower()
    if not username or len(username) < 3:
        raise ValueError("نام کاربری باید حداقل ۳ کاراکتر باشد.")
    if not password or len(password) < 6:
        raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
    if not is_valid_email(email):
        raise ValueError("ایمیل معتبر نیست.")
    if role not in ("admin", "user"):
        role = "user"
    with _lock:
        store = _load()
        if any(u["username"].lower() == username.lower() for u in store["users"]):
            raise ValueError("این نام کاربری قبلاً ثبت شده است.")
        user = {
            **USER_DEFAULTS,
            "id": uuid.uuid4().hex[:12],
            "username": username,
            "email": email,
            "password_hash": _hash_password(password),
            "role": role,
            "created_at": _now_iso(),
        }
        store["users"].append(user)
        _save(store)
        return user


def verify_login(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if user is None or not user.get("enabled", True):
        return None
    if not _verify_password(password or "", user.get("password_hash", "")):
        return None
    return user


def _update(user_id: str, patch: dict) -> dict | None:
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] == user_id:
                u.update(patch)
                _save(store)
                return u
        return None


def set_password(user_id: str, new_password: str) -> dict | None:
    return _update(user_id, {"password_hash": _hash_password(new_password)})


def set_lang(user_id: str, lang: str) -> dict | None:
    return _update(user_id, {"lang": lang})


def set_email(user_id: str, email: str) -> dict | None:
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        raise ValueError("ایمیل معتبر نیست.")
    return _update(user_id, {"email": email})


def set_enabled(user_id: str, enabled: bool) -> dict | None:
    return _update(user_id, {"enabled": bool(enabled)})


def set_telegram_chat_id(user_id: str, chat_id: str | None) -> dict | None:
    return _update(user_id, {"telegram_chat_id": chat_id, "telegram_link_code": None,
                             "telegram_link_code_expires": None})


def set_notify_telegram(user_id: str, enabled: bool) -> dict | None:
    return _update(user_id, {"notify_telegram": bool(enabled)})


def set_link_code(user_id: str) -> str | None:
    code = secrets.token_hex(3)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
    updated = _update(user_id, {"telegram_link_code": code, "telegram_link_code_expires": expires})
    return code if updated else None


def find_by_link_code(code: str) -> dict | None:
    now = _now_iso()
    for u in list_users():
        if u.get("telegram_link_code") == code:
            expires = u.get("telegram_link_code_expires")
            if not expires or expires < now:
                return None
            return u
    return None


def public_view(user: dict) -> dict:
    # totp_secret و کدهای بازیابی هرگز از API بیرون نمی‌روند: با دانستن secret
    # می‌شود کدهای دو مرحله‌ای را خودت تولید کرد، یعنی ۲FA بی‌معنا می‌شود.
    hidden = ("password_hash", "telegram_link_code", "totp_secret",
              "recovery_codes", "email_code")
    return {k: v for k, v in user.items() if k not in hidden}


# ---------- تأیید ایمیل ----------
def _expired(iso: str | None) -> bool:
    if not iso:
        return True
    try:
        return datetime.fromisoformat(iso) < datetime.now(timezone.utc)
    except ValueError:
        return True


def set_email_code(user_id: str) -> str | None:
    """کد ۶ رقمی تازه می‌سازد. اگر هنوز فاصله‌ی مجاز ارسال مجدد نگذشته باشد
    None برمی‌گرداند تا endpoint بتواند جلوی اسپم را بگیرد."""
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] != user_id:
                continue
            last = u.get("email_code_sent_at")
            if last:
                try:
                    delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                    if delta < EMAIL_CODE_RESEND_SECONDS:
                        return None
                except ValueError:
                    pass
            code = f"{secrets.randbelow(10**6):06d}"
            now = datetime.now(timezone.utc)
            u["email_code"] = _hash_password(code)   # کد هم مثل رمز هش می‌شود
            u["email_code_expires"] = (now + timedelta(minutes=EMAIL_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
            u["email_code_sent_at"] = now.isoformat(timespec="seconds")
            _save(store)
            return code
        raise KeyError("کاربر پیدا نشد")


def verify_email_code(user_id: str, code: str) -> bool:
    code = (code or "").strip()
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] != user_id:
                continue
            if u.get("email_verified"):
                return True
            stored = u.get("email_code")
            if not stored or _expired(u.get("email_code_expires")):
                return False
            if not _verify_password(code, stored):
                return False
            u["email_verified"] = True
            u["email_code"] = None
            u["email_code_expires"] = None
            _save(store)
            return True
        raise KeyError("کاربر پیدا نشد")


def migrate_pre_verification_users() -> int:
    """کاربرانی که پیش از افزوده‌شدن تأیید ایمیل ساخته شده‌اند کلید
    email_verified را ندارند. بدون این مهاجرت، بعد از آپدیت همه‌ی آن‌ها —
    از جمله ادمین — پشت صفحه‌ی تأیید قفل می‌شدند. ایدمپوتنت است."""
    with _lock:
        store = _load()
        n = 0
        for u in store["users"]:
            if "email_verified" not in u:
                u["email_verified"] = True
                n += 1
        if n:
            _save(store)
        return n


def set_email_verified(user_id: str, verified: bool) -> dict:
    """برای ادمین — تأیید دستی وقتی کاربر به ایمیلش دسترسی ندارد."""
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] == user_id:
                u["email_verified"] = bool(verified)
                if verified:
                    u["email_code"] = None
                    u["email_code_expires"] = None
                _save(store)
                return u
        raise KeyError("کاربر پیدا نشد")


# ---------- ورود دو مرحله‌ای ----------
def set_totp_secret(user_id: str, secret: str) -> dict:
    """secret را ذخیره می‌کند ولی هنوز فعالش نمی‌کند — فعال‌سازی فقط بعد از
    اینکه کاربر یک کد درست از اپلیکیشن وارد کند انجام می‌شود، وگرنه ممکن است
    کسی خودش را از حساب بیرون بیندازد."""
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] == user_id:
                u["totp_secret"] = secret
                u["totp_enabled"] = False
                _save(store)
                return u
        raise KeyError("کاربر پیدا نشد")


def enable_totp(user_id: str, recovery_hashes: list) -> dict:
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] == user_id:
                if not u.get("totp_secret"):
                    raise ValueError("ابتدا ورود دو مرحله‌ای را راه‌اندازی کنید.")
                u["totp_enabled"] = True
                u["recovery_codes"] = list(recovery_hashes)
                _save(store)
                return u
        raise KeyError("کاربر پیدا نشد")


def disable_totp(user_id: str) -> dict:
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] == user_id:
                u["totp_secret"] = None
                u["totp_enabled"] = False
                u["recovery_codes"] = []
                _save(store)
                return u
        raise KeyError("کاربر پیدا نشد")


def consume_recovery_code(user_id: str, code: str) -> bool:
    """کد بازیابی یک‌بارمصرف است: بعد از استفاده از فهرست حذف می‌شود."""
    code = (code or "").strip().replace("-", "").lower()
    if not code:
        return False
    with _lock:
        store = _load()
        for u in store["users"]:
            if u["id"] != user_id:
                continue
            for h in list(u.get("recovery_codes") or []):
                if _verify_password(code, h):
                    u["recovery_codes"].remove(h)
                    _save(store)
                    return True
            return False
        raise KeyError("کاربر پیدا نشد")


def ensure_admin_seed(default_username: str, default_password: str) -> dict | None:
    """فقط در اولین اجرا (وقتی هنوز هیچ کاربری وجود ندارد) یک کاربر ادمین از
    DASHBOARD_USER/DASHBOARD_PASSWORD می‌سازد، تا دیپلوی موجود دسترسی خود را
    از دست ندهد. اگر رمز خالی بود (حالت قدیمی بدون‌رمز)، یک رمز تصادفی
    ساخته و در لاگ استارتاپ چاپ می‌شود — تنها کانال ارتباطی نصب اولیه."""
    with _lock:
        store = _load()
        if store["users"]:
            return None
        username = (default_username or "admin").strip() or "admin"
        password = default_password or secrets.token_urlsafe(9)
        user = {
            **USER_DEFAULTS,
            "id": uuid.uuid4().hex[:12],
            "username": username,
            "password_hash": _hash_password(password),
            "role": "admin",
            "created_at": _now_iso(),
        }
        store["users"].append(user)
        _save(store)
        if not default_password:
            print(f"[crypto-bot] رمز ادمین به‌صورت خودکار ساخته شد — نام کاربری: {username} / رمز: {password}")
        return user
