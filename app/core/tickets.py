"""
تیکت‌های پشتیبانی — ذخیره در config/tickets.json (صفحه‌ی «پشتیبانی»).
"""
import json
import os
import threading
from datetime import datetime, timezone

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "tickets.json")
TICKETS_PATH = os.getenv("TICKETS_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()
UNITS = ("فنی", "استراتژی", "مالی", "عمومی")
STATUSES = {"open": "در حال بررسی", "answered": "پاسخ داده شده", "closed": "بسته شده"}


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


def list_tickets() -> list:
    with _lock:
        return sorted(_load(), key=lambda t: t.get("created", ""), reverse=True)


def create_ticket(subject: str, body: str, unit: str = "عمومی") -> dict:
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise ValueError("موضوع و متن تیکت الزامی است.")
    if unit not in UNITS:
        unit = "عمومی"
    with _lock:
        tickets = _load()
        next_num = max((t.get("number", 8400) for t in tickets), default=8400) + 1
        ticket = {
            "id": f"TK-{next_num}",
            "number": next_num,
            "subject": subject,
            "body": body,
            "unit": f"واحد {unit}",
            "status": "open",
            "status_label": STATUSES["open"],
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tickets.append(ticket)
        _save(tickets)
        return ticket
