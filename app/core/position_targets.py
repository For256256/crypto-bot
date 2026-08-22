"""
حد ضرر/حد سود و زمان ورودِ پوزیشن‌های باز — ماندگار روی دیسک.

چرا لازم است: این مقادیر تا امروز فقط در حافظه‌ی پروسه نگه داشته می‌شدند، پس
با هر توقف/شروع حساب (و هر ری‌استارت سرویس) از بین می‌رفتند. نتیجه دو ایراد
بود: داشبورد برای پوزیشن‌های از قبل باز، SL/TP را خالی نشان می‌داد؛ و وقتی
آن پوزیشن بعداً در سمت صرافی بسته می‌شد، تشخیص «حد ضرر خورد یا حد سود»
ممکن نبود و معامله با علت نامعلوم ثبت می‌شد.

کلید عمداً «نماد|جهت» است نه شناسه‌ی پوزیشن: شناسه‌ی پوزیشن ممکن است بین
ری‌استارت‌ها یا بین پاسخ‌های مختلف صرافی یکی نباشد، ولی روی هر حساب همیشه
حداکثر یک پوزیشن برای هر ترکیب نماد+جهت وجود دارد.

زمان ورود (open_time) هم به همین دلیل این‌جاست: طبق مستند رسمی، پاسخ
پوزیشن‌های توبیت هیچ فیلد زمانی ندارد (نه createTime، نه openTime)، پس تنها
راه دانستن «کِی وارد شدیم» این است که خودمان لحظه‌ی باز کردن ثبتش کنیم.
پوزیشنی که ربات بازش نکرده باشد (دستی روی خود صرافی، یا از قبل باز بوده)
زمان ورود ندارد و در داشبورد خط تیره نشان داده می‌شود — عمداً، چون حدس‌زدن
زمان ورود بدتر از نگفتنش است.

این فقط یک «حافظه‌ی پشتیبان» است؛ منبع اصلی حقیقت خود صرافی است و اگر
پاسخ صرافی این مقادیر را داشته باشد، همان ترجیح داده می‌شود.
"""
import json
import os
import threading

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "position_targets.json")
TARGETS_PATH = os.getenv("POSITION_TARGETS_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()


def _key(symbol: str, side: str) -> str:
    return f"{symbol}|{side}"


def _load() -> dict:
    if not os.path.exists(TARGETS_PATH):
        return {}
    try:
        with open(TARGETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(TARGETS_PATH), exist_ok=True)
    tmp = TARGETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TARGETS_PATH)


def make_key(symbol: str, side: str) -> str:
    """کلید عمومی همان رکورد — برای وقتی که فراخوان کل دیکشنری حساب را یک‌بار
    خوانده و نمی‌خواهد به‌ازای هر پوزیشن دوباره از دیسک بخواند."""
    return _key(symbol, side)


def set_targets(account_id: str, symbol: str, side: str,
                stop_loss: float | None, take_profit: float | None,
                open_time: str | None = None):
    if stop_loss is None and take_profit is None and open_time is None:
        return
    with _lock:
        data = _load()
        data.setdefault(account_id, {})[_key(symbol, side)] = {
            "stop_loss": stop_loss, "take_profit": take_profit,
            "open_time": open_time,
        }
        _save(data)


def get_targets(account_id: str, symbol: str, side: str) -> dict | None:
    return _load().get(account_id, {}).get(_key(symbol, side))


def get_account(account_id: str) -> dict:
    return _load().get(account_id, {})


def clear_targets(account_id: str, symbol: str, side: str):
    with _lock:
        data = _load()
        acc = data.get(account_id)
        if not acc:
            return
        if acc.pop(_key(symbol, side), None) is None:
            return
        if not acc:
            data.pop(account_id, None)
        _save(data)


def clear_account(account_id: str):
    with _lock:
        data = _load()
        if data.pop(account_id, None) is not None:
            _save(data)
