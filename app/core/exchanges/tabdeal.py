"""
درایور صرافی تبدیل (Tabdeal) — اهرم حرفه‌ای (FAPI)، ساختار مشابه Binance Futures.

نکات کلیدی API (طبق مستندات رسمی docs.tabdeal.org):
- امضا: HMAC-SHA256 روی query string با api-secret؛ نتیجه به‌عنوان signature
  به انتهای پارامترها اضافه می‌شود (دقیقاً مثل الگوی توبیت).
- کلید API در هدر X-MBX-APIKEY ارسال می‌شود.
- هر درخواست امضادار باید timestamp (میلی‌ثانیه) و recvWindow داشته باشد.
- اندپوینت‌های خواندنی (GET) زیر مسیر /r/fapi/، نوشتنی‌ها (POST/DELETE) زیر
  مسیر /fapi/ هستند.
- فرمت نماد: BTCUSDT (بدون جداکننده، دقیقاً مثل TradingView/Binance).
- پاسخ خطا: {"code": ..., "msg": "..."}.

⚠️ محدودیت مهم و تأییدشده (از کل مستندات رسمی، هم بخش اسپات هم FAPI):
این API هیچ اندپوینت کندل تاریخی (klines) یا قیمت لحظه‌ای (ticker) ندارد —
تنها داده‌ی قیمتی موجود، اردربوک (bids/asks) است. در نتیجه:
- get_candles همیشه ExchangeError می‌دهد؛ استراتژی‌های داخلی ربات (که به
  تاریخچه‌ی کندل نیاز دارند) روی این صرافی کار نمی‌کنند — فقط از طریق
  وبهوک TradingView (با price/SL/TP صریح) می‌توان روی حساب‌های تبدیل
  معامله کرد.
- get_last_price از میانگین بهترین bid/ask اردربوک محاسبه می‌شود.
"""
import asyncio
import hashlib
import hmac
import time
import urllib.parse
import uuid

import httpx
import pandas as pd

from app.core.exchanges.base import ExchangeDriver, ExchangeError


def normalize_symbol(raw: str) -> str:
    """هر فرمت ورودی (BTCUSDT، BTC_USDT، BTC/USDT، BTCUSDT.P و ...) را به فرمت
    بدون جداکننده‌ی تبدیل (مثل BTCUSDT) تبدیل می‌کند."""
    s = raw.strip().upper().replace("/", "").replace("_", "").replace(":", "").replace("-", "")
    if s.endswith(".P"):
        s = s[:-2]
    return s


def to_tv_symbol(symbol: str) -> str:
    """نماد تبدیل از قبل با فرمت TradingView یکی است (BTCUSDT)."""
    return symbol


class TabdealDriver(ExchangeDriver):
    RECV_WINDOW = 5000

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api1.tabdeal.org"):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._symbol_info_cache: dict[str, dict] = {}
        self._leverage: dict[str, int] = {}
        self._last_price: dict[str, float] = {}

    # ---------- زیرساخت HTTP و امضا ----------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"X-MBX-APIKEY": self.api_key} if self.api_key else {},
            )
        return self._client

    def _sign(self, params: dict) -> dict:
        if not self.api_key or not self.api_secret:
            raise ExchangeError("کلید API یا Secret تنظیم نشده است.")
        p = {k: v for k, v in params.items() if v is not None}
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = self.RECV_WINDOW
        query = urllib.parse.urlencode(p)
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        p["signature"] = signature
        return p

    async def _request(self, method: str, path: str, params: dict = None, signed: bool = False,
                       read: bool = False, retries: int = 2):
        """path مثل 'v1/order' — پیشوند /r/fapi/ (خواندنی) یا /fapi/ (نوشتنی)
        بر اساس read اضافه می‌شود، دقیقاً مطابق قرارداد مستندات تبدیل."""
        client = self._ensure_client()
        url = ("/r/fapi/" if read else "/fapi/") + path.lstrip("/")
        params = params or {}
        last_error = None
        for attempt in range(retries + 1):
            try:
                if signed:
                    payload = self._sign(params)
                    if method in ("GET", "DELETE"):
                        resp = await client.request(method, url, params=payload)
                    else:
                        resp = await client.request(
                            method, url,
                            content=urllib.parse.urlencode(payload),
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        )
                else:
                    resp = await client.request(method, url, params=params)
            except httpx.HTTPError as e:
                last_error = ExchangeError(f"خطای شبکه در ارتباط با تبدیل: {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            try:
                data = resp.json()
            except ValueError:
                data = None

            if resp.status_code == 429 or (isinstance(data, dict) and data.get("code") == 1216):
                last_error = ExchangeError("محدودیت نرخ درخواست تبدیل؛ کمی صبر لازم است.")
                await asyncio.sleep(3.0)
                continue

            if data is None:
                raise ExchangeError(f"پاسخ غیر-JSON از تبدیل (HTTP {resp.status_code}): {resp.text[:200]}")

            if resp.status_code != 200 or (isinstance(data, dict) and "code" in data and "msg" in data
                                           and data.get("code") not in (0, None)):
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg", str(data)[:200]) if isinstance(data, dict) else str(data)[:200]
                raise ExchangeError(f"خطای تبدیل (HTTP {resp.status_code}, code={code}): {msg}")

            return data

        raise last_error or ExchangeError("ارتباط با تبدیل پس از چند تلاش برقرار نشد.")

    # ---------- رابط ExchangeDriver ----------
    async def connect_public(self):
        await self._request("GET", "v1/ping", read=True)

    async def connect(self):
        await self.connect_public()
        if not self.api_key or not self.api_secret:
            raise ExchangeError("کلید API و Secret برای حالت واقعی (live) الزامی است.")
        await self.get_account_info()

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        raise ExchangeError(
            "صرافی تبدیل (Tabdeal) اندپوینت کندل تاریخی (klines) ندارد؛ استراتژی‌های داخلی "
            "روی این صرافی کار نمی‌کنند. برای این حساب فقط از وبهوک TradingView "
            "(با price/SL/TP صریح در پیام Alert) استفاده کنید."
        )

    async def get_last_price(self, symbol: str) -> float:
        """چون تبدیل اندپوینت ticker ندارد، از میانگین بهترین bid/ask اردربوک استفاده می‌شود."""
        data = await self._request("GET", "v1/depth", {"symbol": symbol, "limit": 5}, read=True)
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(f"اردربوک {symbol} خالی است؛ قیمت لحظه‌ای در دسترس نیست.")
        try:
            best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            raise ExchangeError(f"قیمت لحظه‌ای {symbol} از اردربوک قابل استخراج نبود.")
        price = (best_bid + best_ask) / 2
        self._last_price[symbol] = price
        return price

    async def get_account_info(self) -> dict:
        data = await self._request("GET", "v3/balance", signed=True, read=True)
        rows = data if isinstance(data, list) else []
        usdt = next((r for r in rows if str(r.get("asset", "")).upper() == "USDT"), None)
        if usdt is None:
            raise ExchangeError(f"موجودی اهرم حرفه‌ای USDT در پاسخ تبدیل پیدا نشد: {str(data)[:200]}")

        def _f(*keys, default=0.0):
            for k in keys:
                v = usdt.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return default

        balance = _f("walletBalance")
        unrealized = _f("crossUnPnl", default=0.0)
        available = _f("availableBalance", default=balance)
        return {
            "balance": balance,
            "equity": balance + unrealized,
            "currency": "USDT",
            "margin": max(0.0, balance - available),
            "free_margin": available,
        }

    async def get_open_positions(self, symbol: str = None) -> list:
        params = {"isActive": 1}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "v1/position", params, signed=True, read=True)
        raw_list = data if isinstance(data, list) else []
        positions = []
        for p in raw_list:
            if not isinstance(p, dict):
                continue
            try:
                qty = abs(float(p.get("positionAmt", 0) or 0))
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            sym = p.get("symbol", symbol)
            side = "long" if str(p.get("side", "")).upper() == "BUY" else "short"
            entry_price = float(p.get("entryPrice", 0) or 0)
            try:
                mark_price = await self.get_last_price(sym)
            except ExchangeError:
                mark_price = entry_price
            direction = 1 if side == "long" else -1
            positions.append({
                "id": str(p.get("id", f"{sym}-{side}")),
                "symbol": sym,
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "leverage": float(self._leverage.get(sym, 1)),
                # تبدیل در این اندپوینت سود/زیان لحظه‌ای برنمی‌گرداند؛ محلی محاسبه می‌شود
                # (مثل PaperDriver) — contractMultiplier در FAPI تبدیل مطرح نیست (همیشه ۱).
                "profit": (mark_price - entry_price) * direction * qty,
                "margin": 0.0,
            })
        return positions

    async def _exchange_info(self) -> dict:
        now = time.time()
        cache = getattr(self, "_exchange_info_cache", None)
        cache_time = getattr(self, "_exchange_info_time", 0.0)
        if cache is not None and now - cache_time < 3600:
            return cache
        data = await self._request("GET", "v1/exchangeInfo", read=True)
        self._exchange_info_cache = data if isinstance(data, dict) else {}
        self._exchange_info_time = now
        return self._exchange_info_cache

    async def list_symbols(self) -> list:
        data = await self._exchange_info()
        result = []
        for s in data.get("symbols", []) if isinstance(data, dict) else []:
            if not isinstance(s, dict):
                continue
            sym = str(s.get("symbol", "")).upper()
            if not sym or str(s.get("status", "TRADING")).upper() not in ("TRADING", "ONLINE", ""):
                continue
            base = str(s.get("baseAsset", "")).upper()
            quote = str(s.get("quoteAsset", "")).upper()
            result.append({"symbol": sym, "base": base, "quote": quote})
        result.sort(key=lambda x: x["symbol"])
        return result

    async def get_symbol_info(self, symbol: str) -> dict:
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        data = await self._request("GET", "v1/exchangeInfo", {"symbol": symbol}, read=True)
        found = None
        for s in (data.get("symbols", []) if isinstance(data, dict) else []):
            if s.get("symbol") == symbol:
                found = s
                break
        info = {"min_qty": 0.0, "qty_step": 0.0, "price_step": 0.0, "contract_multiplier": 1.0}
        if found:
            try:
                qty_prec = int(found.get("quantityPrecision", 8) or 8)
                price_prec = int(found.get("pricePrecision", 8) or 8)
                info["qty_step"] = round(10 ** (-qty_prec), qty_prec)
                info["min_qty"] = info["qty_step"]
                info["price_step"] = round(10 ** (-price_prec), price_prec)
            except (TypeError, ValueError):
                pass
        self._symbol_info_cache[symbol] = info
        return info

    async def set_leverage(self, symbol: str, leverage: int):
        await self._request("POST", "v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)
        self._leverage[symbol] = int(leverage)

    async def _find_position_id(self, symbol: str) -> str | None:
        data = await self._request("GET", "v1/position", {"symbol": symbol, "isActive": 1},
                                   signed=True, read=True)
        rows = [p for p in (data if isinstance(data, list) else []) if isinstance(p, dict)]
        if not rows:
            return None
        rows.sort(key=lambda p: p.get("updateTime") or p.get("createdTime") or 0, reverse=True)
        return rows[0].get("id")

    async def place_order(self, side: str, symbol: str, qty: float,
                          stop_loss: float = None, take_profit: float = None) -> dict:
        if side not in ("buy", "sell"):
            raise ExchangeError(f"سمت سفارش نامعتبر: {side}")
        params = {
            "symbol": symbol,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": self._fmt_num(qty),
            "newClientOrderId": self._gen_client_order_id(),
        }
        order = await self._request("POST", "v1/order", params, signed=True)
        result = {"orderId": str(order.get("orderId", "")), "raw": order}

        if stop_loss or take_profit:
            try:
                position_id = await self._find_position_id(symbol)
            except ExchangeError:
                position_id = None
            if position_id is None:
                result["tp_sl_set"] = False
                result["tp_sl_error"] = (
                    "⚠️ پوزیشن باز شد ولی شناسه‌ی پوزیشن برای ثبت TP/SL پیدا نشد — "
                    "حد ضرر/سود را دستی در صرافی تنظیم کنید."
                )
            else:
                tp_sl_params = {"positionId": position_id, "symbol": symbol}
                if take_profit:
                    tp_sl_params["tpPrice"] = self._fmt_num(take_profit)
                if stop_loss:
                    tp_sl_params["slPrice"] = self._fmt_num(stop_loss)
                try:
                    await self._request("POST", "v1/positionSlTp", tp_sl_params, signed=True)
                    result["tp_sl_set"] = True
                except ExchangeError as e:
                    result["tp_sl_set"] = False
                    result["tp_sl_error"] = (
                        f"⚠️ پوزیشن باز شد ولی ست کردن TP/SL روی صرافی ناموفق بود: {e} — "
                        "حد ضرر/سود را دستی در صرافی تنظیم کنید."
                    )
        return result

    async def close_position(self, position: dict) -> dict:
        symbol = position["symbol"]
        await self._request("DELETE", "v1/position", {"symbol": symbol}, signed=True)
        return {"orderId": "", "closed": True}

    @staticmethod
    def _fmt_num(v) -> str:
        s = f"{float(v):.10f}".rstrip("0").rstrip(".")
        return s if s else "0"

    @staticmethod
    def _gen_client_order_id() -> str:
        return f"cb{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"
