"""
توکن فعال‌سازی معاملات واقعی (live) — ذخیره در config/tokens.json.
خرید کاملاً دستی/آفلاین است: کاربر از پشتیبانی (واحد مالی) درخواست می‌کند،
ادمین بعد از تأیید پرداخت از پنل ادمین یک توکن با مدت‌زمان مشخص صادر می‌کند.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "tokens.json")
TOKENS_PATH = os.getenv("TOKENS_CONFIG_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list:
    if not os.path.exists(TOKENS_PATH):
        return []
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(tokens: list):
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TOKENS_PATH)


def list_tokens(user_id: str | None = None) -> list:
    with _lock:
        tokens = _load()
    if user_id is not None:
        tokens = [t for t in tokens if t.get("user_id") == user_id]
    return sorted(tokens, key=lambda t: t.get("issued_at", ""), reverse=True)


def issue_token(user_id: str, duration_days: int, note: str, issued_by: str) -> dict:
    duration_days = int(duration_days)
    if duration_days <= 0:
        raise ValueError("مدت‌زمان توکن باید بزرگ‌تر از صفر باشد.")
    now = datetime.now(timezone.utc)
    with _lock:
        tokens = _load()
        token = {
            "id": uuid.uuid4().hex[:12],
            "user_id": user_id,
            "issued_by": issued_by,
            "issued_at": now.isoformat(timespec="seconds"),
            "duration_days": duration_days,
            "expires_at": (now + timedelta(days=duration_days)).isoformat(timespec="seconds"),
            "note": (note or "").strip(),
            "revoked": False,
            "revoked_at": None,
        }
        tokens.append(token)
        _save(tokens)
        return token


def revoke_token(token_id: str) -> dict | None:
    with _lock:
        tokens = _load()
        for t in tokens:
            if t.get("id") == token_id:
                t["revoked"] = True
                t["revoked_at"] = _now_iso()
                _save(tokens)
                return t
        return None


def get_active_token(user_id: str) -> dict | None:
    """آخرین توکن غیرباطل و منقضی‌نشده‌ی این کاربر (بر اساس دورترین expires_at)."""
    now = _now_iso()
    active = [t for t in list_tokens(user_id) if not t.get("revoked") and t.get("expires_at", "") > now]
    if not active:
        return None
    return max(active, key=lambda t: t.get("expires_at", ""))


def has_active_token(user_id: str) -> bool:
    return get_active_token(user_id) is not None


def delete_by_user(user_id: str) -> int:
    """پاک‌کردن همه‌ی توکن‌های یک کاربر. تعداد حذف‌شده را برمی‌گرداند."""
    with _lock:
        items = _load()
        keep = [t for t in items if t.get("user_id") != user_id]
        n = len(items) - len(keep)
        if n:
            _save(keep)
    return n
