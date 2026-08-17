"""
پشتیبان‌گیری و بازیابی تنظیمات حساب‌ها و نمادها.

چه چیزی داخل بکاپ *نمی‌رود* و چرا:
- api_key / api_secret: کلید صرافی است. فایل بکاپ معمولاً در دانلودها،
  ایمیل یا فضای ابری می‌ماند؛ گذاشتن کلید معامله‌ی واقعی داخل چنین فایلی
  یعنی هرکس فایل را ببیند می‌تواند با پول کاربر معامله کند.
- webhook_token: توکن اختصاصی وبهوک همان حساب است. اگر لو برود، هر کسی
  می‌تواند به آن حساب سیگنال بفرستد.
- owner_id / id: هویت داخلی است. اگر بازیابی این‌ها را بازنویسی کند،
  می‌شود حساب کاربر دیگری را با یک فایل دستکاری‌شده تصاحب کرد.

بنابراین بکاپ فقط «تنظیمات و نمادها» است، نه اعتبارنامه. بعد از بازیابی
کاربر باید کلیدهای API را دوباره وارد کند — و تا وقتی وارد نکرده، حساب
بازیابی‌شده در حالت کاغذی می‌ماند.
"""
import copy
from datetime import datetime, timezone

from app.core import config_store

FORMAT = "cplusepro-backup"
# شناسه‌ی نسخه‌های قبل از تغییر نام برند. فایل‌های پشتیبانی که کاربران از قبل
# دانلود کرده‌اند این شناسه را دارند و باید همچنان قابل بازیابی بمانند — پس
# موقع خواندن هر دو پذیرفته می‌شوند، ولی خروجی جدید همیشه با نام تازه است.
LEGACY_FORMATS = {"cryptopulse-backup"}
ACCEPTED_FORMATS = {FORMAT} | LEGACY_FORMATS
VERSION = 1

# این کلیدها هرگز نه صادر می‌شوند و نه از فایل واردشده پذیرفته می‌شوند.
SENSITIVE_KEYS = {"api_key", "api_secret", "webhook_token"}
# was_running وضعیت لحظه‌ای اجراست، نه تنظیمات. اگر وارد بکاپ می‌شد، حساب
# بازیابی‌شده خودش شروع به کار می‌کرد — که کاربر انتظارش را ندارد.
IDENTITY_KEYS = {"id", "owner_id", "is_suggested", "was_running"}
SKIP_KEYS = SENSITIVE_KEYS | IDENTITY_KEYS


def _clean_account(acc: dict) -> dict:
    out = {k: copy.deepcopy(v) for k, v in acc.items()
           if k not in SKIP_KEYS and k != "symbols"}
    out["symbols"] = [copy.deepcopy(s) for s in (acc.get("symbols") or [])]
    # حساب واقعی بدون کلید نمی‌تواند معامله کند؛ بکاپ همیشه کاغذی برمی‌گردد
    # تا بازیابی به‌طور تصادفی یک حساب «واقعیِ ناقص» نسازد.
    out["trading_mode"] = "paper"
    return out


def export_for_owner(owner_id: str, username: str = "") -> dict:
    accounts = config_store.list_accounts(owner_id)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "username": username,
        "accounts": [_clean_account(a) for a in accounts],
    }


def _unique_name(existing: set, name: str) -> str:
    if name not in existing:
        return name
    i = 2
    while f"{name} ({i})" in existing:
        i += 1
    return f"{name} ({i})"


def validate(payload: dict) -> list:
    """فهرست حساب‌های معتبر داخل فایل. ValueError اگر فایل اصلاً بکاپ نباشد."""
    if not isinstance(payload, dict):
        raise ValueError("فایل پشتیبان معتبر نیست.")
    if payload.get("format") not in ACCEPTED_FORMATS:
        raise ValueError("فایل پشتیبان معتبر نیست.")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("فایل پشتیبان هیچ حسابی ندارد.")
    out = []
    for a in accounts:
        if isinstance(a, dict) and str(a.get("name") or "").strip():
            out.append(a)
    if not out:
        raise ValueError("فایل پشتیبان هیچ حسابی ندارد.")
    return out


def restore_for_owner(owner_id: str, payload: dict, mode: str = "new") -> dict:
    """mode='new'  → همیشه حساب تازه می‌سازد (نام تکراری شماره می‌گیرد).
       mode='merge' → حسابی که همین نام را دارد به‌روزرسانی می‌شود.

    حالت پیش‌فرض عمداً 'new' است چون هیچ چیزی را از بین نمی‌برد. 'merge'
    تنظیمات و نمادهای حساب موجود را بازنویسی می‌کند، پس باید صریح خواسته شود.
    """
    incoming = validate(payload)
    if mode not in ("new", "merge"):
        raise ValueError("حالت بازیابی نامعتبر است.")

    mine = config_store.list_accounts(owner_id)
    by_name = {a.get("name"): a for a in mine}
    taken = set(by_name)

    created, updated = [], []
    for raw in incoming:
        data = {k: v for k, v in raw.items() if k not in SKIP_KEYS}
        symbols = data.pop("symbols", []) or []
        data["trading_mode"] = "paper"
        name = str(data.get("name") or "").strip()

        target = by_name.get(name) if mode == "merge" else None
        if target is not None:
            config_store.update_account(target["id"], data)
            # نمادها کامل جایگزین می‌شوند، وگرنه ترکیب دو حالت یک پیکربندی
            # نصفه‌نیمه می‌سازد که در هیچ‌کدام از دو بکاپ وجود نداشته.
            for s in list(target.get("symbols") or []):
                config_store.remove_symbol(target["id"], s["symbol"])
            for s in symbols:
                if s.get("symbol"):
                    config_store.add_symbol(target["id"], s)
            updated.append(name)
        else:
            data["name"] = _unique_name(taken, name)
            taken.add(data["name"])
            acc = config_store.add_account(data, owner_id)
            for s in symbols:
                if s.get("symbol"):
                    config_store.add_symbol(acc["id"], s)
            created.append(data["name"])

    return {"created": created, "updated": updated,
            "symbols": sum(len(a.get("symbols") or []) for a in incoming)}
