"""
درایور شبیه‌سازی (حالت paper). داده‌ی کندل و قیمت واقعی را از درایور واقعی
(مثلاً Toobit) می‌گیرد، اما سفارش واقعی ارسال نمی‌کند؛ پوزیشن‌ها را داخل
حافظه شبیه‌سازی و سود/زیانشان را با قیمت لحظه‌ای واقعی به‌روز می‌کند.
حتی SL/TP هم شبیه‌سازی می‌شود: در هر بار خواندن پوزیشن‌ها، اگر قیمت از
حد رد شده باشد پوزیشن به‌صورت خودکار بسته و نتیجه در موجودی اعمال می‌شود.

برای اینکه حساب paper واقعاً شبیه حساب live باشد (و مقایسه‌شان منصفانه
باشد)، این موارد هم دقیقاً مثل صرافی شبیه‌سازی می‌شوند:
- کارمزد تیکر واقعی توبیت (٪۰.۰۶ هر سمت — چون سفارش‌ها همیشه مارکت هستند)
  روی باز و بسته شدن هر پوزیشن کسر می‌شود.
- contractMultiplier واقعی نماد (از همان exchangeInfo صرافی) در محاسبه‌ی
  ارزش/مارجین لحاظ می‌شود، دقیقاً مثل درایور live.
- مارجین پوزیشن‌های باز واقعاً از موجودی آزاد کم می‌شود (نه اینکه همیشه کل
  اکوییتی «آزاد» نشان داده شود)، تا همان سقف‌های مدیریت ریسک/مارجین که روی
  حساب live اعمال می‌شوند، عیناً روی paper هم اعمال شوند.
"""
import itertools
from datetime import datetime, timezone

import pandas as pd

from app.core.exchanges.base import ExchangeDriver, ExchangeError
from app.core.exchanges.toobit import TAKER_FEE_RATE

_id_counter = itertools.count(1)


class PaperDriver(ExchangeDriver):
    def __init__(self, data_source: ExchangeDriver, starting_balance: float = 10000.0):
        self._source = data_source
        self.balance = starting_balance
        self.positions: list[dict] = []
        self.closed_trades: list[dict] = []
        self._leverage: dict[str, int] = {}

    def drain_closed_trades(self) -> list[dict]:
        """معاملات بسته‌شده‌ی جدید را برمی‌گرداند و بافر را خالی می‌کند (برای ثبت در تاریخچه)."""
        out = self.closed_trades
        self.closed_trades = []
        return out

    async def connect(self):
        # در حالت paper فقط داده‌های عمومی لازم است؛ کلید API اجباری نیست.
        connect_public = getattr(self._source, "connect_public", None)
        if connect_public is not None:
            await connect_public()
        else:
            await self._source.connect()

    async def close(self):
        await self._source.close()

    async def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        return await self._source.get_candles(symbol, timeframe, count)

    async def get_symbol_info(self, symbol: str) -> dict:
        return await self._source.get_symbol_info(symbol)

    async def get_last_price(self, symbol: str) -> float:
        return await self._source.get_last_price(symbol)

    async def set_leverage(self, symbol: str, leverage: int):
        # برخلاف قبل، این مقدار واقعاً ذخیره می‌شود تا محاسبه‌ی مارجین paper
        # هم مثل live از اهرم واقعی تنظیم‌شده استفاده کند.
        self._leverage[symbol] = int(leverage)

    async def _contract_multiplier(self, symbol: str) -> float:
        try:
            info = await self._source.get_symbol_info(symbol)
        except ExchangeError:
            return 1.0
        return float(info.get("contract_multiplier", 1.0) or 1.0)

    async def _refresh_positions(self):
        """سود/زیان پوزیشن‌های paper را با قیمت واقعی به‌روز و SL/TP را شبیه‌سازی می‌کند."""
        still_open = []
        for p in self.positions:
            try:
                price = await self._source.get_last_price(p["symbol"])
            except ExchangeError:
                still_open.append(p)
                continue
            p["mark_price"] = price
            direction = 1 if p["side"] == "long" else -1
            p["profit"] = (price - p["entry_price"]) * direction * p["qty"] * p.get("contract_multiplier", 1.0)

            hit = None
            if p.get("stop_loss") and (
                (p["side"] == "long" and price <= p["stop_loss"]) or
                (p["side"] == "short" and price >= p["stop_loss"])
            ):
                hit = ("SL", p["stop_loss"])
            elif p.get("take_profit") and (
                (p["side"] == "long" and price >= p["take_profit"]) or
                (p["side"] == "short" and price <= p["take_profit"])
            ):
                hit = ("TP", p["take_profit"])

            if hit:
                label, hit_price = hit
                cm = p.get("contract_multiplier", 1.0)
                gross = (hit_price - p["entry_price"]) * direction * p["qty"] * cm
                exit_fee = hit_price * p["qty"] * cm * TAKER_FEE_RATE
                # کارمزد ورود همان لحظه‌ی باز شدن پوزیشن از موجودی کم شده؛ اینجا
                # فقط سود/زیان خام منهای کارمزد خروج به موجودی اضافه می‌شود تا
                # کارمزد ورود دوباره کم نشود. مقدار «realized» گزارش‌شده اما هر
                # دو کارمزد را برای نمایش سود/زیان خالص واقعی هر معامله دارد.
                fee = p.get("entry_fee", 0.0) + exit_fee
                realized = gross - fee
                self.balance += gross - exit_fee
                self.closed_trades.append({**p, "close_price": hit_price, "realized": realized, "fee": fee,
                                           "closed_by": label,
                                           "close_time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            else:
                still_open.append(p)
        self.positions = still_open

    async def get_account_info(self) -> dict:
        await self._refresh_positions()
        unrealized = sum(p.get("profit", 0.0) for p in self.positions)
        margin = sum(p.get("margin", 0.0) for p in self.positions)
        equity = self.balance + unrealized
        return {
            "balance": self.balance,
            "equity": equity,
            "currency": "USDT",
            "margin": margin,
            "free_margin": max(0.0, equity - margin),
        }

    async def get_open_positions(self, symbol: str = None) -> list:
        await self._refresh_positions()
        if symbol:
            return [p for p in self.positions if p["symbol"] == symbol]
        return list(self.positions)

    async def place_order(self, side: str, symbol: str, qty: float,
                          stop_loss: float = None, take_profit: float = None) -> dict:
        price = await self._source.get_last_price(symbol)
        contract_multiplier = await self._contract_multiplier(symbol)
        leverage = self._leverage.get(symbol, 1) or 1
        notional = qty * price * contract_multiplier
        entry_fee = notional * TAKER_FEE_RATE
        self.balance -= entry_fee
        position = {
            "id": f"paper-{next(_id_counter)}",
            "symbol": symbol,
            "side": "long" if side == "buy" else "short",
            "qty": qty,
            "entry_price": price,
            "mark_price": price,
            "leverage": float(leverage),
            "profit": 0.0,
            "margin": notional / leverage,
            "contract_multiplier": contract_multiplier,
            "entry_fee": entry_fee,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "open_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.positions.append(position)
        return {"orderId": position["id"], "tp_sl_set": True}

    async def close_position(self, position: dict) -> dict:
        pos_id = position.get("id")
        target = next((p for p in self.positions if p["id"] == pos_id), None)
        if target is None:
            raise ExchangeError("پوزیشن paper برای بستن پیدا نشد.")
        price = await self._source.get_last_price(target["symbol"])
        direction = 1 if target["side"] == "long" else -1
        cm = target.get("contract_multiplier", 1.0)
        gross = (price - target["entry_price"]) * direction * target["qty"] * cm
        exit_fee = price * target["qty"] * cm * TAKER_FEE_RATE
        # کارمزد ورود همان لحظه‌ی باز شدن پوزیشن از موجودی کم شده؛ اینجا فقط
        # سود/زیان خام منهای کارمزد خروج به موجودی اضافه می‌شود (توضیح کامل در
        # _refresh_positions).
        fee = target.get("entry_fee", 0.0) + exit_fee
        realized = gross - fee
        self.balance += gross - exit_fee
        self.positions = [p for p in self.positions if p["id"] != pos_id]
        self.closed_trades.append({**target, "close_price": price, "realized": realized, "fee": fee,
                                   "closed_by": "manual",
                                   "close_time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return {"orderId": pos_id, "closed": True, "realized": realized}
