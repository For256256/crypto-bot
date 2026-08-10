"""
تنظیمات ربات — همه مقادیر از فایل .env خوانده می‌شوند.
هیچ مقدار حساس (کلید API، پسورد، توکن وبهوک) نباید مستقیم در کد نوشته شود.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    # ---- سرور داشبورد ----
    DASHBOARD_PORT: int = _get_int("DASHBOARD_PORT", 8891)
    DASHBOARD_USER: str = os.getenv("DASHBOARD_USER", "admin")
    DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

    # ---- کلید امضای سشن (کوکی لاگین چندکاربره) ----
    SESSION_SECRET_KEY: str = os.getenv("SESSION_SECRET_KEY", "")

    # ---- توکن محافظ Webhook تریدینگ‌ویو ----
    WEBHOOK_TOKEN: str = os.getenv("WEBHOOK_TOKEN", "")

    # ---- آدرس پایه‌ی API صرافی Toobit ----
    TOOBIT_BASE_URL: str = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com")

    # ---- آدرس پایه‌ی API صرافی تبدیل (Tabdeal) ----
    TABDEAL_BASE_URL: str = os.getenv("TABDEAL_BASE_URL", "https://api1.tabdeal.org")


settings = Settings()
