"""
ارسال ایمیل با smtplib استاندارد پایتون (بدون وابستگی جدید pip).
تنظیمات SMTP از پنل ادمین (app_settings.email) خوانده می‌شود.
"""
import asyncio
import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from app.core import app_settings

# آخرین نتیجه‌ی ارسال، برای نمایش در پنل ادمین. بدون این، تنها نشانه‌ی خرابی
# SMTP این بود که کدهای تأیید بی‌صدا نمی‌رسیدند و هیچ‌کس علتش را نمی‌دانست.
_last: dict = {"ok": None, "error": "", "at": None, "to": ""}


def last_status() -> dict:
    return dict(_last)


def _record(ok: bool, error: str = "", to: str = ""):
    _last.update({"ok": ok, "error": error[:300], "at": time.time(), "to": to})


def _describe(exc: Exception) -> str:
    """پیام خطای قابل‌فهم برای ادمین، به‌جای یک False خالی."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "نام کاربری یا رمز SMTP پذیرفته نشد."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "سرور گیرنده را نپذیرفت — آدرس مقصد را بررسی کنید."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "سرور آدرس فرستنده را نپذیرفت — from_address باید متعلق به همان دامنه/حساب باشد."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "سرور STARTTLS را پشتیبانی نمی‌کند — گزینه‌ی TLS را عوض کنید."
    if isinstance(exc, ssl.SSLError):
        return "خطای TLS — احتمالاً پورت با حالت TLS نمی‌خواند (۵۸۷ با STARTTLS، ۴۶۵ با SSL)."
    if isinstance(exc, TimeoutError):
        return "اتصال به سرور SMTP تمام‌وقت شد — میزبان/پورت یا فایروال سرور را بررسی کنید."
    if isinstance(exc, OSError):
        return f"اتصال برقرار نشد: {exc}"
    return str(exc) or exc.__class__.__name__


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
        _record(False, "SMTP تنظیم نشده است." if not host else "آدرس گیرنده خالی است.", to_address)
        return False
    port = int(cfg.get("smtp_port") or 587)
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    from_addr = (cfg.get("from_address") or user).strip()
    from_name = cfg.get("from_name") or "cpulsepro"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    # formataddr نام فرستنده را درست کدگذاری می‌کند؛ رشته‌ی دستی با نام غیرASCII
    # هدر خراب می‌ساخت. Date و Message-ID هم برای فیلترهای اسپم مهم‌اند —
    # نبودشان به‌تنهایی می‌تواند ایمیل را به Junk بفرستد.
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), from_addr))
    msg["To"] = to_address
    msg["Date"] = formatdate(localtime=True)
    domain = from_addr.split("@")[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)

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
        _record(True, "", to_address)
        return True
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        reason = _describe(e)
        _record(False, reason, to_address)
        print(f"[mailer] ارسال ایمیل به {to_address} ناموفق بود: {reason}")
        return False


def _test_sync(cfg: dict) -> tuple:
    """فقط اتصال/احراز هویت را چک می‌کند، بدون ارسال ایمیل واقعی.
    خروجی (ok, error) است تا ادمین علت شکست را ببیند نه فقط «ناموفق»."""
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return False, "میزبان SMTP وارد نشده است."
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
        return True, ""
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return False, _describe(e)


async def send_email(to_address: str, subject: str, body: str) -> bool:
    if not to_address:
        return False
    return await asyncio.to_thread(_send_sync, _settings(), to_address, subject, body)


async def test_connection(cfg: dict) -> tuple:
    """(ok, error)"""
    return await asyncio.to_thread(_test_sync, cfg)


async def notify_user_email(user_id: str | None, subject: str, body: str):
    if not user_id:
        return
    from app.core import users
    user = users.get_user(user_id)
    if user is None or not user.get("email"):
        return
    await send_email(user["email"], subject, body)
