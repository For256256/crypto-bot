"""
درایور شبیه‌سازی (حالت paper). داده‌ی کندل و قیمت واقعی را از درایور واقعی
(مثلاً Toobit) می‌گیرد، اما سفارش واقعی ارسال نمی‌کند؛ پوزیشن‌ها را داخل
حافظه شبیه‌سازی و سود/زیانشان را با قیمت لحظه‌ای واقعی به‌روز می‌کند.
حتی SL/TP هم شبیه‌سازی می‌شود: در هر بار خواندن پوزیشن‌ها، اگر قیمت از
حد رد شده باشد پوزیشن به‌صورت خودکار بسته و نتیجه در موجودی اعمال می‌شود.
"""
import itertools
from datetime import datetime, timezone

import pandas as pd

from app.core.exchanges.base import ExchangeDriver, ExchangeError

_id_counter = itertools.count(1)


class PaperDriver(ExchangeDriver):
    def __init__(self, data_source: ExchangeDriver, starting_balance: float = 10000.0):
        self._source = data_source
        self.balance = starting_balance
        self.positions: list[dict] = []
        self.closed_trades: list[dict] = []

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
        return  # در شبیه‌سازی کاری لازم نیست؛ اهرم در سود/زیان paper نقشی در مارجین واقعی ندارد

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
            p["profit"] = (price - p["entry_price"]) * direction * p["qty"]

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
                realized = (hit_price - p["entry_price"]) * direction * p["qty"]
                self.balance += realized
                self.closed_trades.append({**p, "close_price": hit_price, "realized": realized, "closed_by": label,
                                           "close_time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            else:
                still_open.append(p)
        self.positions = still_open

    async def get_account_info(self) -> dict:
        await self._refresh_positions()
        unrealized = sum(p.get("profit", 0.0) for p in self.positions)
        return {
            "balance": self.balance,
            "equity": self.balance + unrealized,
            "currency": "USDT",
            "margin": 0.0,
            "free_margin": self.balance + unrealized,
        }

    async def get_open_positions(self, symbol: str = None) -> list:
        await self._refresh_positions()
        if symbol:
            return [p for p in self.positions if p["symbol"] == symbol]
        return list(self.positions)

    async def place_order(self, side: str, symbol: str, qty: float,
                          stop_loss: float = None, take_profit: float = None) -> dict:
        price = await self._source.get_last_price(symbol)
        position = {
            "id": f"paper-{next(_id_counter)}",
            "symbol": symbol,
            "side": "long" if side == "buy" else "short",
            "qty": qty,
            "entry_price": price,
            "mark_price": price,
            "leverage": 1.0,
            "profit": 0.0,
            "margin": 0.0,
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
        realized = (price - target["entry_price"]) * direction * target["qty"]
        self.balance += realized
        self.positions = [p for p in self.positions if p["id"] != pos_id]
        self.closed_trades.append({**target, "close_price": price, "realized": realized, "closed_by": "manual",
                                   "close_time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return {"orderId": pos_id, "closed": True, "realized": realized}
