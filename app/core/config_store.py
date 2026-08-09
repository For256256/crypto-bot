"""
ذخیره و بازیابی پیکربندی حساب‌ها و نمادها در یک فایل JSON ساده.
مسیر فایل با متغیر محیطی ACCOUNTS_CONFIG_PATH قابل تغییر است
(پیش‌فرض: config/accounts.json کنار کد).
"""
import copy
import json
import os
import threading
import uuid

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "accounts.json")
CONFIG_PATH = os.getenv("ACCOUNTS_CONFIG_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

ACCOUNT_DEFAULTS = {
    "exchange": "toobit",
    "trading_mode": "paper",
    "api_key": "",
    "api_secret": "",
    "paper_balance": 10000.0,
    "risk_percent": 1.0,
    "default_leverage": 5,
    "sl_tp_atr_mult": 3.0,
    "max_open_positions": 2,
    "max_daily_loss_percent": 5.0,
    "poll_interval_seconds": 60,
    "recycle_on_new_signal": False,
    "accept_webhook": True,
    "enabled": True,
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


def list_accounts() -> list:
    with _lock:
        return list(_load()["accounts"])


def get_account(account_id: str):
    for a in list_accounts():
        if a["id"] == account_id:
            return a
    return None


def add_account(data: dict) -> dict:
    with _lock:
        store = _load()
        account = {**ACCOUNT_DEFAULTS, **data, "id": uuid.uuid4().hex[:12], "symbols": []}
        store["accounts"].append(account)
        _save(store)
        return account


def update_account(account_id: str, data: dict) -> dict:
    with _lock:
        store = _load()
        for a in store["accounts"]:
            if a["id"] == account_id:
                # نمادها و id از ورودی به‌روز نمی‌شوند
                data = {k: v for k, v in data.items() if k not in ("id", "symbols")}
                a.update(data)
                _save(store)
                return a
        raise KeyError("حساب پیدا نشد")


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
