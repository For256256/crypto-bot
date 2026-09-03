"""
رابط مشترک همه‌ی درایورهای صرافی. engine.py فقط با همین رابط کار می‌کند و
هیچ‌چیز از جزئیات Toobit (یا صرافی‌های بعدی) نمی‌داند — برای افزودن صرافی
جدید فقط یک درایور جدید با همین متدها لازم است + یک خط در factory.py.
"""
from abc import ABC, abstractmethod
import pandas as pd


class ExchangeError(Exception):
    pass


class ExchangeDriver(ABC):
    """هر حساب یک نمونه‌ی مستقل از درایور دارد."""

    @abstractmethod
    async def connect(self):
        """اعتبارسنجی اتصال/کلیدها. در صورت مشکل ExchangeError بدهد."""

    @abstractmethod
    async def close(self):
        """آزادسازی منابع (سشن HTTP و ...). باید idempotent باشد."""

    @abstractmethod
    async def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        """DataFrame با ستون‌های time, open, high, low, close, volume (صعودی بر اساس زمان)."""

    @abstractmethod
    async def get_account_info(self) -> dict:
        """dict با کلیدهای balance, equity, currency, margin, free_margin."""

    @abstractmethod
    async def get_open_positions(self, symbol: str = None) -> list:
        """لیست پوزیشن‌های باز؛ هر پوزیشن dict با کلیدهای
        id, symbol, side ('long'|'short'), qty, entry_price, mark_price, leverage, profit."""

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict:
        """مشخصات نماد: min_qty, qty_step, price_step, contract_multiplier."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int):
        """تنظیم اهرم نماد. خطای غیرحیاتی نباید معامله را متوقف کند (فراخوان try می‌کند)."""

    @abstractmethod
    async def place_order(self, side: str, symbol: str, qty: float,
                          stop_loss: float = None, take_profit: float = None) -> dict:
        """سفارش مارکت باز کردن پوزیشن. side: 'buy' یا 'sell'.
        اگر SL/TP داده شود، بعد از باز شدن پوزیشن روی آن ست می‌شود.
        خروجی: dict حداقل با کلید orderId."""

    @abstractmethod
    async def close_position(self, position: dict) -> dict:
        """بستن کامل یک پوزیشن (همان dict خروجی get_open_positions)."""

    async def update_stop_loss(self, position: dict, stop_loss: float,
                               take_profit: float | None = None) -> None:
        """جابه‌جا کردن حد ضرر یک پوزیشن باز (برای حد ضرر دنبال‌کننده).

        عمداً abstract نیست تا درایورهای موجود بدون پیاده‌سازی نشکنند؛
        موتور قبل از صدا زدن، وجودش را چک می‌کند.
        """
        raise NotImplementedError

    async def set_trailing_stop(self, position: dict, callback_rate: float,
                                active_price: float | None = None) -> dict:
        """سپردن دنبال‌کردنِ حد ضرر به خود صرافی.

        callback_rate کسر است نه درصد (۰٫۰۱ یعنی ۱٪). خروجی، پاسخ خام صرافی
        است تا فراخوان بتواند تأیید کند که واقعاً تریلینگ ثبت شده. مثل
        update_stop_loss عمداً abstract نیست.
        """
        raise NotImplementedError
