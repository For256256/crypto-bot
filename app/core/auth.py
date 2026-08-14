"""
احراز هویت مبتنی بر سشن کوکی (جایگزین HTTPBasic قدیمی). هر route فقط یکی
از این Dependency ها را می‌گیرد — منطق تشخیص کاربر یک‌جا همین‌جاست.
"""
from fastapi import Depends, status
from starlette.requests import Request

from app.core import users
from app.core.errors import ApiError


class NotAuthenticated(Exception):
    """برای route های صفحه (HTML) — exception handler در main.py به ریدایرکت /login تبدیلش می‌کند."""


class EmailNotVerified(Exception):
    """مثل بالا، ولی به /verify-email ریدایرکت می‌شود."""


def email_verification_required() -> bool:
    """تأیید ایمیل فقط وقتی اجباری است که SMTP تنظیم شده باشد. اگر سروری
    هنوز SMTP ندارد، اجباری‌کردنش یعنی هیچ کاربر تازه‌ای نمی‌تواند وارد شود،
    چون کد هیچ‌وقت به دستش نمی‌رسد."""
    from app.core import mailer
    return mailer.is_configured()


def is_verified(user: dict) -> bool:
    # ادمین هیچ‌وقت پشت این دیوار قفل نمی‌شود: تنظیم SMTP خودش کار ادمین است
    # و اگر او هم قفل شود، راه بازکردنش از داخل محصول وجود ندارد.
    if user.get("role") == "admin":
        return True
    if user.get("email_verified"):
        return True
    return not email_verification_required()


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = users.get_user(user_id)
    if user is None or not user.get("enabled", True):
        return None
    return user


def require_session(request: Request) -> dict:
    """فقط سشن معتبر می‌خواهد و کاری به تأیید ایمیل ندارد — برای خودِ
    endpoint های تأیید ایمیل، وگرنه کاربر تأییدنشده حتی نمی‌تواند کد بزند."""
    user = get_current_user(request)
    if user is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "err.please_login", "لطفاً وارد شوید.")
    return user


def require_user(request: Request) -> dict:
    """برای /api/* — نبود سشن معتبر یعنی ۴۰۱، ایمیل تأییدنشده یعنی ۴۰۳."""
    user = require_session(request)
    if not is_verified(user):
        raise ApiError(status.HTTP_403_FORBIDDEN, "err.email_unverified",
                       "ابتدا ایمیل خود را تأیید کنید.")
    return user


def require_user_page(request: Request) -> dict:
    """برای صفحات HTML — نبود سشن معتبر یعنی ریدایرکت به /login."""
    user = get_current_user(request)
    if user is None:
        raise NotAuthenticated()
    if not is_verified(user):
        raise EmailNotVerified()
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise ApiError(status.HTTP_403_FORBIDDEN, "err.admin_only", "این بخش فقط برای ادمین است.")
    return user


def require_admin_page(user: dict = Depends(require_user_page)) -> dict:
    if user.get("role") != "admin":
        raise ApiError(status.HTTP_403_FORBIDDEN, "err.admin_only", "این بخش فقط برای ادمین است.")
    return user
