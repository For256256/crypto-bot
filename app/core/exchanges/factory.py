"""
کارخانه‌ی ساخت درایور صرافی برای هر حساب.
برای افزودن صرافی جدید: درایور را import و در EXCHANGES ثبت کن — همین.
"""
from app.core.exchanges.base import ExchangeError
from app.core.exchanges.toobit import ToobitDriver
from app.core.exchanges.tabdeal import TabdealDriver, TabdealViaToobitDataDriver
from app.core.exchanges.paper import PaperDriver
from app.config import settings

EXCHANGES = {
    "toobit": lambda cfg: ToobitDriver(
        api_key=cfg.get("api_key", ""),
        api_secret=cfg.get("api_secret", ""),
        base_url=settings.TOOBIT_BASE_URL,
    ),
    # کندل/قیمت مرجع از API عمومی توبیت گرفته می‌شود (چون تبدیل اندپوینت
    # کندل/ticker ندارد)؛ حساب/پوزیشن/سفارش واقعی همیشه روی تبدیل است —
    # یعنی سیگنال از توبیت، اجرا روی تبدیل.
    "tabdeal": lambda cfg: TabdealViaToobitDataDriver(
        TabdealDriver(
            api_key=cfg.get("api_key", ""),
            api_secret=cfg.get("api_secret", ""),
            base_url=settings.TABDEAL_BASE_URL,
        ),
        toobit_base_url=settings.TOOBIT_BASE_URL,
    ),
}


def build_driver(trading_mode: str, cfg: dict):
    """
    برای هر حساب یک درایور می‌سازد.
    - live: درایور واقعی صرافی (کلید API الزامی).
    - paper: همان درایور واقعی برای داده‌ی قیمت/کندل + لایه‌ی شبیه‌ساز PaperDriver
      روی آن، تا هیچ سفارش واقعی ارسال نشود.
    """
    exchange = cfg.get("exchange", "toobit")
    builder = EXCHANGES.get(exchange)
    if builder is None:
        raise ExchangeError(f"صرافی پشتیبانی‌نشده: {exchange}")
    real_driver = builder(cfg)
    if trading_mode == "live":
        return real_driver
    return PaperDriver(real_driver, starting_balance=float(cfg.get("paper_balance", 10000.0)))
