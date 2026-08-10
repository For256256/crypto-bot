"""
کاربران پنل (چندکاربره) — ذخیره در config/users.json.
همان الگوی config_store.py: فایل JSON مسطح + threading.Lock + نوشتن اتمیک.
هش پسورد با hashlib.pbkdf2_hmac استاندارد پایتون (بدون وابستگی خارجی).
"""
import hashlib
import hmac
import json
import os
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

USER_DEFAULTS = {
    "role": "user",              # "admin" | "user"
    "enabled": True,
    "telegram_chat_id": None,
    "telegram_link_code": None,
    "telegram_link_code_expires": None,
    "notify_telegram": True,
}


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


def create_user(username: str, password: str, role: str = "user") -> dict:
    username = (username or "").strip()
    password = password or ""
    if not username or len(username) < 3:
        raise ValueError("نام کاربری باید حداقل ۳ کاراکتر باشد.")
    if not password or len(password) < 6:
        raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
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
    return {k: v for k, v in user.items() if k not in ("password_hash", "telegram_link_code")}


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
