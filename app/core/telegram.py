"""
اتصال به یک ربات تلگرام مشترک برای کل پنل. با همان httpx.AsyncClient موجود در
پروژه به Bot API تلگرام وصل می‌شود (بدون وابستگی جدید مثل python-telegram-bot).

مدل کار: ادمین یک‌بار توکن ربات (از BotFather) را از پنل ادمین تنظیم می‌کند.
هر کاربر (از جمله خود ادمین) از صفحه‌ی تنظیمات یک کد یک‌بارمصرف می‌گیرد، در
تلگرام به ربات می‌گوید /start <code>، و poll_updates_loop (حلقه‌ی
long-polling پس‌زمینه) آن پیام را می‌گیرد، کد را به کاربر متصل می‌کند و
chat_id او را ذخیره می‌کند. از آن به بعد notify_user/notify_admin پیام‌های
شخصی/ادمین را به همان chat_id ارسال می‌کنند.

عمداً وب‌هوک عمومی تلگرام استفاده نشده چون سرور کاربر لزوماً HTTPS/certificate
عمومی ندارد؛ long-polling نیازی به آن ندارد.
"""
import asyncio
import re

import httpx

from app.core import app_settings, users

API_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT_SECONDS = 25
_START_RE = re.compile(r"^/start\s+([A-Za-z0-9]+)")


def _bot_token() -> str:
    return (app_settings.get_settings().get("telegram") or {}).get("bot_token", "") or ""


async def get_me(bot_token: str) -> dict | None:
    """اعتبارسنجی توکن ربات — {id, username, ...} یا None اگر نامعتبر بود."""
    if not bot_token:
        return None
    url = API_BASE.format(token=bot_token) + "/getMe"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            data = resp.json()
        if data.get("ok"):
            return data.get("result")
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def send_message(chat_id: str, text: str) -> bool:
    token = _bot_token()
    if not token or not chat_id:
        return False
    url = API_BASE.format(token=token) + "/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def notify_user(user_id: str | None, text: str):
    if not user_id:
        return
    user = users.get_user(user_id)
    if user is None or not user.get("telegram_chat_id") or not user.get("notify_telegram", True):
        return
    await send_message(user["telegram_chat_id"], text)


async def notify_admin(text: str):
    chat_id = (app_settings.get_settings().get("telegram") or {}).get("admin_chat_id", "")
    if chat_id:
        await send_message(chat_id, text)


async def _handle_update(update: dict):
    message = update.get("message") or {}
    text = str(message.get("text") or "")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    m = _START_RE.match(text.strip())
    if not m:
        return
    code = m.group(1)
    user = users.find_by_link_code(code)
    if user is None:
        await send_message(str(chat_id), "کد نامعتبر یا منقضی‌شده است — یک کد جدید از صفحه‌ی تنظیمات بگیرید.")
        return
    users.set_telegram_chat_id(user["id"], str(chat_id))
    await send_message(str(chat_id), f"✅ حساب تلگرام شما به کاربر «{user['username']}» در کریپتو بات متصل شد.")


async def poll_updates_loop():
    """حلقه‌ی پس‌زمینه‌ی دائمی — هرگز نباید با استثنای بدون مدیریت متوقف شود،
    وگرنه اتصال /start و همه‌ی اعلان‌ها تا ری‌استارت بعدی سرویس خاموش می‌مانند."""
    backoff = 5
    while True:
        token = _bot_token()
        if not token:
            await asyncio.sleep(15)
            continue
        settings_data = app_settings.get_settings()
        offset = int((settings_data.get("telegram") or {}).get("last_update_id", 0) or 0)
        url = API_BASE.format(token=token) + "/getUpdates"
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS + 10) as client:
                resp = await client.get(url, params={
                    "offset": offset + 1, "timeout": POLL_TIMEOUT_SECONDS,
                })
                data = resp.json()
            if not data.get("ok"):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            updates = data.get("result") or []
            max_id = offset
            for u in updates:
                max_id = max(max_id, int(u.get("update_id", offset)))
                try:
                    await _handle_update(u)
                except Exception:
                    pass
            if max_id != offset:
                app_settings.update_settings({"telegram": {"last_update_id": max_id}})
        except httpx.HTTPError:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
