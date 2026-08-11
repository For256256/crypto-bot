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


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = users.get_user(user_id)
    if user is None or not user.get("enabled", True):
        return None
    return user


def require_user(request: Request) -> dict:
    """برای /api/* — نبود سشن معتبر یعنی ۴۰۱."""
    user = get_current_user(request)
    if user is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "err.please_login", "لطفاً وارد شوید.")
    return user


def require_user_page(request: Request) -> dict:
    """برای صفحات HTML — نبود سشن معتبر یعنی ریدایرکت به /login."""
    user = get_current_user(request)
    if user is None:
        raise NotAuthenticated()
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise ApiError(status.HTTP_403_FORBIDDEN, "err.admin_only", "این بخش فقط برای ادمین است.")
    return user


def require_admin_page(user: dict = Depends(require_user_page)) -> dict:
    if user.get("role") != "admin":
        raise ApiError(status.HTTP_403_FORBIDDEN, "err.admin_only", "این بخش فقط برای ادمین است.")
    return user
