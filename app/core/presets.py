"""
پیش‌فرض‌های پیشنهادی ادمین برای استراتژی/نماد — کاربران می‌توانند این‌ها را
هنگام افزودن/ویرایش نماد با یک کلیک روی فرم اعمال کنند. ذخیره در config/presets.json.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "presets.json")
PRESETS_PATH = os.getenv("PRESETS_CONFIG_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

PRESET_DEFAULTS = {
    "description": "",
    "strategy": "supertrend_ema_rsi",
    "strategy_params": {},
    "timeframe": "1h",
    "leverage": None,
    "risk_percent": None,
    "sl_tp_atr_mult": None,
}


def _load() -> list:
    if not os.path.exists(PRESETS_PATH):
        return []
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(presets: list):
    os.makedirs(os.path.dirname(PRESETS_PATH), exist_ok=True)
    tmp = PRESETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRESETS_PATH)


def list_presets() -> list:
    with _lock:
        return list(_load())


def get_preset(preset_id: str) -> dict | None:
    for p in list_presets():
        if p["id"] == preset_id:
            return p
    return None


def create_preset(data: dict, created_by: str) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("نام پیش‌فرض الزامی است.")
    with _lock:
        presets = _load()
        preset = {
            **PRESET_DEFAULTS,
            **{k: v for k, v in data.items() if k in PRESET_DEFAULTS},
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        presets.append(preset)
        _save(presets)
        return preset


def update_preset(preset_id: str, data: dict) -> dict | None:
    with _lock:
        presets = _load()
        for p in presets:
            if p["id"] == preset_id:
                patch = {k: v for k, v in data.items() if k in PRESET_DEFAULTS or k == "name"}
                p.update(patch)
                _save(presets)
                return p
        return None


def delete_preset(preset_id: str) -> bool:
    with _lock:
        presets = _load()
        before = len(presets)
        presets = [p for p in presets if p["id"] != preset_id]
        _save(presets)
        return len(presets) != before
