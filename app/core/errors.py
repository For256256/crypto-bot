"""
خطای API قابل ترجمه.

در لحظه‌ی raise فقط کلید ترجمه و پارامترها نگه داشته می‌شوند؛ متن نهایی را
exception handler در main.py به زبان همان درخواست می‌سازد. متن فارسی به‌عنوان
default همراه خطا می‌ماند تا اگر کلیدی در کاتالوگ نبود، پیام قبلی از دست نرود.

این کلاس عمداً در app/core زندگی می‌کند (نه در main.py) تا ماژول‌هایی مثل
auth.py هم بتوانند بدون import چرخه‌ای از آن استفاده کنند.
"""
from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status_code: int, key: str, default: str = "", **params):
        super().__init__(status_code, default or key)
        self.key = key
        self.default = default or key
        self.params = params
