"""
ارسال ایمیل با smtplib استاندارد پایتون (بدون وابستگی جدید pip).
تنظیمات SMTP از پنل ادمین (app_settings.email) خوانده می‌شود.
"""
import asyncio
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText

from app.core import app_settings


def _settings() -> dict:
    return app_settings.get_settings().get("email") or {}


def is_configured() -> bool:
    """آیا SMTP تنظیم شده است؟ تأیید ایمیل فقط وقتی اجباری می‌شود که پاسخ
    این تابع True باشد — وگرنه سروری که هنوز SMTP ندارد همه‌ی کاربران تازه
    را پشت صفحه‌ای قفل می‌کند که هیچ‌وقت کدش نمی‌رسد."""
    return bool((_settings().get("smtp_host") or "").strip())


def _send_sync(cfg: dict, to_address: str, subject: str, body: str) -> bool:
    host = (cfg.get("smtp_host") or "").strip()
    if not host or not to_address:
        return False
    port = int(cfg.get("smtp_port") or 587)
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    from_addr = (cfg.get("from_address") or user).strip()
    from_name = cfg.get("from_name") or "cplusepro"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_address

    try:
        if cfg.get("use_tls", True):
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if user:
                    server.login(user, password)
                server.sendmail(from_addr, [to_address], msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
                if user:
                    server.login(user, password)
                server.sendmail(from_addr, [to_address], msg.as_string())
        return True
    except (smtplib.SMTPException, OSError, ssl.SSLError):
        return False


def _test_sync(cfg: dict) -> bool:
    """فقط اتصال/احراز هویت را چک می‌کند، بدون ارسال ایمیل واقعی."""
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return False
    port = int(cfg.get("smtp_port") or 587)
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    try:
        if cfg.get("use_tls", True):
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.starttls(context=ssl.create_default_context())
                if user:
                    server.login(user, password)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context()) as server:
                if user:
                    server.login(user, password)
        return True
    except (smtplib.SMTPException, OSError, ssl.SSLError):
        return False


async def send_email(to_address: str, subject: str, body: str) -> bool:
    if not to_address:
        return False
    return await asyncio.to_thread(_send_sync, _settings(), to_address, subject, body)


async def test_connection(cfg: dict) -> bool:
    return await asyncio.to_thread(_test_sync, cfg)


async def notify_user_email(user_id: str | None, subject: str, body: str):
    if not user_id:
        return
    from app.core import users
    user = users.get_user(user_id)
    if user is None or not user.get("email"):
        return
    await send_email(user["email"], subject, body)
