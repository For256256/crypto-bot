"""
ذخیره و بازیابی پیکربندی حساب‌ها و نمادها در یک فایل JSON ساده.
مسیر فایل با متغیر محیطی ACCOUNTS_CONFIG_PATH قابل تغییر است
(پیش‌فرض: config/accounts.json کنار کد).
"""
import copy
import json
import os
import secrets
import threading
import uuid

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "accounts.json")
CONFIG_PATH = os.getenv("ACCOUNTS_CONFIG_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

ACCOUNT_DEFAULTS = {
    "owner_id": None,
    # ادمین می‌تواند حساب خودش را «پیشنهادی» کند تا کاربران روند کلی و تنظیمات
    # آن را ببینند و با یک کلیک حساب مشابهی برای خودشان بسازند.
    "is_suggested": False,
    "exchange": "toobit",
    "trading_mode": "paper",
    "api_key": "",
    "api_secret": "",
    "paper_balance": 10000.0,
    "risk_percent": 1.0,
    "default_leverage": 5,
    "sl_tp_atr_mult": 3.0,
    "max_margin_per_trade_pct": 25.0,
    "max_open_positions": 2,
    "max_daily_loss_percent": 5.0,
    "poll_interval_seconds": 60,
    "recycle_on_new_signal": False,
    # وقتی پوزیشن باز است و سیگنال جهت مخالف می‌رسد چه کنیم؟
    #   none       = هیچ‌کدام بسته نشود؛ پوزیشن به SL/TP خودش سپرده شود (پیش‌فرض)
    #   profitable = فقط اگر پوزیشن در سود باشد بسته و معکوس شود (رفتار قدیمی)
    #   always     = همیشه بسته و معکوس شود
    # پیش‌فرض عمداً none است: حالت profitable بردها را زود می‌بُرد ولی باخت‌ها
    # را تا حد ضرر کامل رها می‌کرد، یعنی میانگین باخت را بزرگ‌تر از میانگین برد
    # می‌کرد — حتی با نرخ برد بالا نتیجه منفی می‌شد.
    "reversal_policy": "none",
    # همه‌ی سیگنال‌ها (استراتژی داخلی و وبهوک) وارونه اجرا می‌شوند: خرید→فروش
    "invert_signals": False,
    "accept_webhook": True,
    "enabled": True,
    # گزارش باز/بسته شدن معامله‌ی همین حساب. هشدارهای ایمنی (خطای صرافی،
    # رسیدن به سقف ضرر روزانه) عمداً از این دو مستثنا هستند: کسی که گزارش
    # معاملات یک حساب را خاموش می‌کند، معمولاً نمی‌خواهد از خرابی هم بی‌خبر بماند.
    "notify_telegram": True,
    "notify_browser": True,
    # آیا ربات این حساب در لحظه‌ی خاموش‌شدن سرویس در حال اجرا بود؟ وضعیت اجرا
    # فقط در حافظه بود، پس با هر ری‌استارت (مثلاً بعد از آپدیت) همه‌ی ربات‌ها
    # خاموش می‌شدند و کاربر باید دستی روشنشان می‌کرد. این فلگ «قصد کاربر» را
    # ماندگار می‌کند تا استارتاپ بتواند دقیقاً همان‌ها را برگرداند.
    "was_running": False,
}

SYMBOL_DEFAULTS = {
    "timeframe": "1h",
    "enabled": True,
    "strategy": "supertrend_ema_rsi",
    "strategy_params": {},
    "leverage": None,
    "min_qty": None,
    "qty_step": None,
    "max_qty": None,
}


def _load() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"accounts": []}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "accounts" not in data:
            return {"accounts": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"accounts": []}


def _save(data: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def list_accounts(owner_id: str | None = None) -> list:
    with _lock:
        accounts = list(_load()["accounts"])
    if owner_id is None:
        return accounts
    return [a for a in accounts if a.get("owner_id") == owner_id]


def get_account(account_id: str):
    with _lock:
        accounts = list(_load()["accounts"])
    for a in accounts:
        if a["id"] == account_id:
            return a
    return None


def add_account(data: dict, owner_id: str) -> dict:
    with _lock:
        store = _load()
        data = {k: v for k, v in data.items() if k not in ("id", "symbols", "owner_id", "webhook_token")}
        account = {
            **ACCOUNT_DEFAULTS, **data,
            "id": uuid.uuid4().hex[:12],
            "owner_id": owner_id,
            "webhook_token": secrets.token_hex(24),
            "symbols": [],
        }
        store["accounts"].append(account)
        _save(store)
        return account


def update_account(account_id: str, data: dict) -> dict:
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] == account_id:
                # نمادها، id، مالکیت و توکن وبهوک از راه ادیت معمولی تغییر نمی‌کنند
                data = {k: v for k, v in data.items() if k not in ("id", "symbols", "owner_id", "webhook_token")}
                a.update(data)
                _save(store)
                return a
        raise KeyError("حساب پیدا نشد")


def set_running_flag(account_id: str, running: bool) -> None:
    """وضعیت «در حال اجرا بودن» را ماندگار می‌کند تا بعد از ری‌استارت سرویس
    قابل بازگردانی باشد. عمداً سکوت می‌کند اگر حساب پیدا نشود: این تابع از دل
    مسیر شروع/توقف ربات صدا زده می‌شود و نباید خودِ آن عملیات را بشکند."""
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] == account_id:
                if a.get("was_running") == bool(running):
                    return                      # بدون تغییر، بدون نوشتن روی دیسک
                a["was_running"] = bool(running)
                _save(store)
                return


def list_previously_running() -> list:
    return [a for a in _load()["accounts"] if a.get("was_running")]


def set_suggested(account_id: str, is_suggested: bool) -> dict:
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] == account_id:
                a["is_suggested"] = bool(is_suggested)
                _save(store)
                return a
        raise KeyError("حساب پیدا نشد")


def list_suggested() -> list:
    with _lock:
        return [a for a in _load()["accounts"] if a.get("is_suggested")]


def clone_for_user(account_id: str, owner_id: str) -> dict:
    """از یک حساب پیشنهادی، حساب مشابهی برای کاربر دیگر می‌سازد.

    عمداً کلیدهای API کپی نمی‌شوند و حالت روی paper برمی‌گردد: کاربر باید
    کلیدهای خودش را وارد کند و برای معامله‌ی واقعی توکن فعال داشته باشد.
    نماد و استراتژی‌ها کپی می‌شوند، چون کل ارزش «حساب مشابه» همان‌هاست.
    """
    with _lock:
        store = _load()
        src = next((a for a in store["accounts"] if a["id"] == account_id), None)
        if src is None:
            raise KeyError("حساب پیدا نشد")
        clone = copy.deepcopy(src)
        clone.update({
            "id": uuid.uuid4().hex[:12],
            "owner_id": owner_id,
            "webhook_token": secrets.token_hex(24),
            "is_suggested": False,     # کپیِ کاربر خودش پیشنهادی نیست
            "trading_mode": "paper",
            "api_key": "",
            "api_secret": "",
            "enabled": True,
        })
        existing = {a["name"] for a in store["accounts"]}
        base = src.get("name", "account")
        name, n = base, 2
        while name in existing:
            name = f"{base} ({n})"
            n += 1
        clone["name"] = name
        store["accounts"].append(clone)
        _save(store)
        return clone


def rotate_webhook_token(account_id: str) -> str:
    """توکن وبهوک این حساب را با یک مقدار تصادفی جدید جایگزین می‌کند (قبلی بلافاصله باطل می‌شود)."""
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] == account_id:
                new_token = secrets.token_hex(24)
                a["webhook_token"] = new_token
                _save(store)
                return new_token
        raise KeyError("حساب پیدا نشد")


def migrate_owner_less_accounts(default_owner_id: str) -> int:
    """به هر حساب قدیمی بدون owner_id (از قبل از معرفی چندکاربره)، مالک پیش‌فرض
    (معمولاً ادمین) می‌دهد؛ ایدمپوتنت — بعد از اولین بار روی هر حساب کاری نمی‌کند.
    همچنین حساب‌های قدیمی بدون webhook_token هم یکی می‌گیرند."""
    with _lock:
        store = _load()
        changed = 0
        any_modified = False
        for a in store["accounts"]:
            if not a.get("owner_id"):
                a["owner_id"] = default_owner_id
                changed += 1
                any_modified = True
            if not a.get("webhook_token"):
                a["webhook_token"] = secrets.token_hex(24)
                any_modified = True
        if any_modified:
            _save(store)
        return changed


def duplicate_account(account_id: str) -> dict:
    """از یک حساب، کپی کامل می‌سازد: همه تنظیمات + نمادها (با id و نام جدید)."""
    with _lock:
        store = _load()
        src = None
        for a in store["accounts"]:
            if a["id"] == account_id:
                src = a
                break
        if src is None:
            raise KeyError("حساب پیدا نشد")
        clone = copy.deepcopy(src)
        clone["id"] = uuid.uuid4().hex[:12]
        clone["webhook_token"] = secrets.token_hex(24)
        existing = {a["name"] for a in store["accounts"]}
        base = f"کپی از {src['name']}"
        name, n = base, 2
        while name in existing:
            name = f"{base} ({n})"
            n += 1
        clone["name"] = name
        store["accounts"].append(clone)
        _save(store)
        return clone


def delete_account(account_id: str):
    with _lock:
        store = _load()
        store["accounts"] = [a for a in store["accounts"] if a["id"] != account_id]
        _save(store)


# ---------- نمادها ----------
def _find_account(store: dict, account_id: str) -> dict:
    for a in store["accounts"]:
        if a["id"] == account_id:
            return a
    raise KeyError("حساب پیدا نشد")


def add_symbol(account_id: str, data: dict) -> dict:
    with _lock:
        store = _load()
        account = _find_account(store, account_id)
        symbol = data["symbol"]
        if any(s["symbol"] == symbol for s in account.get("symbols", [])):
            raise KeyError(f"نماد {symbol} از قبل در این حساب وجود دارد")
        symbol_cfg = {**SYMBOL_DEFAULTS, **data}
        account.setdefault("symbols", []).append(symbol_cfg)
        _save(store)
        return symbol_cfg


def update_symbol(account_id: str, symbol: str, data: dict):
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] != account_id:
                continue
            for s in a.get("symbols", []):
                if s["symbol"] == symbol:
                    data = {k: v for k, v in data.items() if k != "symbol"}
                    s.update(data)
                    _save(store)
                    return s
            return None
        return None


def remove_symbol(account_id: str, symbol: str):
    with _lock:
        store = _load()
        account = _find_account(store, account_id)
        before = len(account.get("symbols", []))
        account["symbols"] = [s for s in account.get("symbols", []) if s["symbol"] != symbol]
        if len(account["symbols"]) == before:
            raise KeyError(f"نماد {symbol} پیدا نشد")
        _save(store)


def bulk_update_symbols(account_id: str, data: dict) -> int:
    """اعمال دسته‌جمعی تنظیمات روی همه نمادهای یک حساب.
    فقط کلیدهای موجود در data (مثلاً timeframe، strategy، strategy_params،
    leverage، min_qty، qty_step، max_qty، enabled) روی همه نمادها اعمال می‌شود.
    خروجی: تعداد نمادهای به‌روزشده."""
    allowed = {"timeframe", "strategy", "strategy_params", "leverage",
               "min_qty", "qty_step", "max_qty", "enabled"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise ValueError("هیچ تنظیمی برای اعمال انتخاب نشده است")
    with _lock:
        store = _load()
        account = _find_account(store, account_id)
        symbols = account.get("symbols", [])
        for s in symbols:
            s.update(copy.deepcopy(updates))
        _save(store)
        return len(symbols)


def toggle_symbol(account_id: str, symbol: str, enabled: bool):
    with _lock:
        store = _load()
        account = _find_account(store, account_id)
        for s in account.get("symbols", []):
            if s["symbol"] == symbol:
                s["enabled"] = bool(enabled)
                _save(store)
                return
        raise KeyError(f"نماد {symbol} پیدا نشد")
