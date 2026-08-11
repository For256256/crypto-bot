"""
تیکت‌های پشتیبانی — ذخیره در config/tickets.json (صفحه‌ی «پشتیبانی»).
هر تیکت متعلق به یک کاربر است؛ ادمین همه را می‌بیند و می‌تواند پاسخ بدهد.
"""
import json
import os
import threading
from datetime import datetime, timezone

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "tickets.json")
TICKETS_PATH = os.getenv("TICKETS_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()
# کلیدهای پایدار واحدها — رابط کاربری آن‌ها را به زبان کاربر ترجمه می‌کند.
# برچسب فارسی فقط برای سازگاری با تیکت‌های قدیمی (که unit متنی داشتند) نگه داشته شده است.
UNIT_KEYS = ("technical", "strategy", "billing", "general")
UNIT_LABELS_FA = {"technical": "فنی", "strategy": "استراتژی", "billing": "مالی", "general": "عمومی"}
UNITS = tuple(UNIT_LABELS_FA.values())
STATUSES = {"open": "در حال بررسی", "answered": "پاسخ داده شده", "closed": "بسته شده"}


def normalize_unit(unit: str) -> str:
    """هم کلید پایدار ('billing') و هم برچسب قدیمی فارسی ('مالی') را می‌پذیرد."""
    if unit in UNIT_KEYS:
        return unit
    for key, label in UNIT_LABELS_FA.items():
        if unit == label or unit == f"واحد {label}":
            return key
    return "general"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list:
    if not os.path.exists(TICKETS_PATH):
        return []
    try:
        with open(TICKETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(tickets: list):
    os.makedirs(os.path.dirname(TICKETS_PATH), exist_ok=True)
    tmp = TICKETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tickets, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TICKETS_PATH)


def list_tickets(user_id: str | None = None) -> list:
    with _lock:
        tickets = _load()
    if user_id is not None:
        tickets = [t for t in tickets if t.get("user_id") == user_id]
    return sorted(tickets, key=lambda t: t.get("created", ""), reverse=True)


def get_ticket(ticket_id: str) -> dict | None:
    for t in list_tickets():
        if t.get("id") == ticket_id:
            return t
    return None


def create_ticket(subject: str, body: str, unit: str, user_id: str, username: str) -> dict:
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise ValueError("موضوع و متن تیکت الزامی است.")
    unit_key = normalize_unit(unit)
    with _lock:
        tickets = _load()
        next_num = max((t.get("number", 8400) for t in tickets), default=8400) + 1
        ticket = {
            "id": f"TK-{next_num}",
            "number": next_num,
            "user_id": user_id,
            "username": username,
            "subject": subject,
            "body": body,
            "unit_key": unit_key,
            # برچسب فارسی برای سازگاری عقب‌رو با هر مصرف‌کننده‌ی قدیمی
            "unit": f"واحد {UNIT_LABELS_FA[unit_key]}",
            "status": "open",
            "status_label": STATUSES["open"],
            "created": _now_iso(),
            "replies": [],
        }
        tickets.append(ticket)
        _save(tickets)
        return ticket


def add_reply(ticket_id: str, message: str, by_user_id: str, by_username: str, by_role: str) -> dict | None:
    message = (message or "").strip()
    if not message:
        raise ValueError("متن پاسخ نمی‌تواند خالی باشد.")
    with _lock:
        tickets = _load()
        for t in tickets:
            if t.get("id") == ticket_id:
                t.setdefault("replies", []).append({
                    "by_user_id": by_user_id,
                    "by_username": by_username,
                    "by_role": by_role,
                    "message": message,
                    "time": _now_iso(),
                })
                if by_role == "admin":
                    t["status"] = "answered"
                    t["status_label"] = STATUSES["answered"]
                _save(tickets)
                return t
        return None


def close_ticket(ticket_id: str) -> dict | None:
    with _lock:
        tickets = _load()
        for t in tickets:
            if t.get("id") == ticket_id:
                t["status"] = "closed"
                t["status_label"] = STATUSES["closed"]
                _save(tickets)
                return t
        return None
