"""
تنظیمات کلی برنامه (اعلان‌ها و ...) — config/app_settings.json.
در حال حاضر فقط وضعیت سوئیچ‌های اعلان صفحه‌ی تنظیمات را نگه می‌دارد.
"""
import json
import os
import threading

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "app_settings.json")
SETTINGS_PATH = os.getenv("APP_SETTINGS_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

DEFAULTS = {
    "notifications": {
        "telegram": False,
        "email": False,
        "sms": False,
    }
}


def _load() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, OSError, ValueError):
        data = {}
    merged = json.loads(json.dumps(DEFAULTS))
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def _save(data: dict):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def get_settings() -> dict:
    with _lock:
        return _load()


def update_settings(patch: dict) -> dict:
    with _lock:
        data = _load()
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
        _save(data)
        return data
