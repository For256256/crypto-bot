"""
درایور صرافی Toobit — فیوچرز USDT-M (پرپچوال).

نکات کلیدی API (طبق مستندات رسمی Toobit):
- امضا: HMAC-SHA256 روی totalParams (queryString و در صورت وجود بدنه، بدنه‌ی
  فرم-انکد شده) با secretKey؛ نتیجه به‌عنوان پارامتر signature اضافه می‌شود.
- کلید API در هدر X-BB-APIKEY (حساس به حروف بزرگ/کوچک) ارسال می‌شود.
- هر درخواست امضادار باید timestamp (میلی‌ثانیه) و ترجیحاً recvWindow داشته باشد.
- فرمت نماد فیوچرز: BTC-SWAP-USDT (فرمت اسپات BTCUSDT جواب خالی برمی‌گرداند).
- محدودیت نرخ: وزن ۳۰۰۰/دقیقه و سفارش ۶۰/دقیقه؛ خطای 429 (کد -1003) یعنی
  باید تا X-Api-Limit-Reset-Timestamp صبر کرد.
"""
import asyncio
import hashlib
import hmac
import time
import urllib.parse

import httpx
import pandas as pd

from app.core.exchanges.base import ExchangeDriver, ExchangeError

# نگاشت تایم‌فریم داخلی پروژه به interval کندل Toobit
INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

_KNOWN_QUOTES = ("USDT", "USDC", "USD")


def normalize_symbol(raw: str) -> str:
    """
    هر فرمت ورودی (BTCUSDT از تریدینگ‌ویو، BTCUSDT.P، btc-swap-usdt، BTC/USDT)
    را به فرمت فیوچرز Toobit یعنی BTC-SWAP-USDT تبدیل می‌کند.
    """
    s = raw.strip().upper().replace("/", "").replace(":", "")
    if s.endswith(".P"):  # فرمت پرپچوال تریدینگ‌ویو مثل BTCUSDT.P
        s = s[:-2]
    if "-SWAP-" in s:
        return s
    s = s.replace("-", "")
    for quote in _KNOWN_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return f"{base}-SWAP-{quote}"
    return s  # فرمت ناشناخته؛ همان‌طور که هست برگردان (خطای صرافی خودش گویا خواهد بود)


def to_tv_symbol(symbol: str) -> str:
    """BTC-SWAP-USDT → BTCUSDT (برای نمایش/تطبیق با پیام تریدینگ‌ویو)."""
    return symbol.replace("-SWAP-", "")


class ToobitDriver(ExchangeDriver):
    RECV_WINDOW = 5000

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.toobit.com"):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._symbol_info_cache: dict[str, dict] = {}
        self._last_price: dict[str, float] = {}
        self._exchange_info_cache: dict | None = None
        self._exchange_info_time: float = 0.0

    # ---------- زیرساخت HTTP و امضا ----------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"X-BB-APIKEY": self.api_key} if self.api_key else {},
            )
        return self._client

    def _sign(self, params: dict) -> dict:
        """timestamp و recvWindow را اضافه و signature را محاسبه می‌کند."""
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

    async def _request(self, method: str, path: str, params: dict = None, signed: bool = False, retries: int = 2):
        client = self._ensure_client()
        params = params or {}
        last_error = None
        for attempt in range(retries + 1):
            try:
                if signed:
                    payload = self._sign(params)  # امضا هر بار تازه ساخته می‌شود (timestamp جدید)
                    if method == "GET":
                        resp = await client.get(path, params=payload)
                    else:
                        # مطابق مستندات Toobit، بدنه‌ی POST به‌صورت form-urlencoded ارسال
                        # می‌شود و امضا روی همان رشته محاسبه شده است.
                        resp = await client.request(
                            method, path,
                            content=urllib.parse.urlencode(payload),
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        )
                else:
                    resp = await client.request(method, path, params=params)
            except httpx.HTTPError as e:
                last_error = ExchangeError(f"خطای شبکه در ارتباط با Toobit: {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 429:
                # محدودیت نرخ — تا زمان ریست اعلام‌شده صبر کن و دوباره تلاش کن
                reset_ts = resp.headers.get("X-Api-Limit-Reset-Timestamp")
                wait = 3.0
                if reset_ts:
                    try:
                        wait = max(0.5, min(30.0, int(reset_ts) / 1000 - time.time()))
                    except ValueError:
                        pass
                last_error = ExchangeError("محدودیت نرخ درخواست Toobit (429)؛ کمی صبر لازم است.")
                await asyncio.sleep(wait)
                continue

            try:
                data = resp.json()
            except ValueError:
                raise ExchangeError(f"پاسخ غیر-JSON از Toobit (HTTP {resp.status_code}): {resp.text[:200]}")

            if resp.status_code != 200 or (isinstance(data, dict) and data.get("code") not in (None, 0, 200, "0")):
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg", str(data)[:200]) if isinstance(data, dict) else str(data)[:200]
                raise ExchangeError(f"خطای Toobit (HTTP {resp.status_code}, code={code}): {msg}")

            return data

        raise last_error or ExchangeError("ارتباط با Toobit پس از چند تلاش برقرار نشد.")

    # ---------- رابط ExchangeDriver ----------
    async def connect_public(self):
        """فقط دسترسی به داده‌های عمومی (کندل/قیمت) را تست می‌کند — بدون نیاز به کلید API.
        حالت paper از همین استفاده می‌کند تا بدون کلید هم کار کند."""
        await self._request("GET", "/api/v1/time")

    async def connect(self):
        # ۱) تست دسترسی عمومی، ۲) تست کلیدها با یک درخواست امضادار
        await self.connect_public()
        if not self.api_key or not self.api_secret:
            raise ExchangeError("کلید API و Secret برای حالت واقعی (live) الزامی است.")
        await self.get_account_info()

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        interval = INTERVAL_MAP.get(timeframe, "1h")
        data = await self._request("GET", "/quote/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": min(count, 1000),
        })
        if not data or not isinstance(data, list):
            raise ExchangeError(
                f"داده کندلی برای {symbol} دریافت نشد. "
                "مطمئن شوید نماد به فرمت فیوچرز (مثل BTC-SWAP-USDT) است."
            )
        # فرمت هر ردیف مانند Binance: [openTime, open, high, low, close, volume, ...]
        rows = []
        for k in data:
            rows.append({
                "time": int(k[0]),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        if len(df) > 1:
            # کندل آخر هنوز بسته نشده؛ سیگنال باید روی کندل بسته‌شده محاسبه شود
            # تا با رفتار TradingView (سیگنال روی close کندل) یکسان باشد.
            df = df.iloc[:-1].reset_index(drop=True)
        if not df.empty:
            self._last_price[symbol] = float(df["close"].iat[-1])
        return df

    async def get_last_price(self, symbol: str) -> float:
        data = await self._request("GET", "/quote/v1/ticker/price", {"symbol": symbol})
        if isinstance(data, list) and data:
            data = data[0]
        try:
            price = float(data.get("price") or data.get("p"))
        except (TypeError, ValueError, AttributeError):
            raise ExchangeError(f"قیمت لحظه‌ای {symbol} دریافت نشد.")
        self._last_price[symbol] = price
        return price

    async def get_account_info(self) -> dict:
        data = await self._request("GET", "/api/v1/futures/balance", signed=True)
        # پاسخ ممکن است لیست دارایی‌ها باشد؛ دارایی USDT را پیدا می‌کنیم
        assets = data if isinstance(data, list) else data.get("assets", [data]) if isinstance(data, dict) else []
        usdt = None
        for a in assets:
            if isinstance(a, dict) and str(a.get("asset", a.get("coin", ""))).upper() in ("USDT", ""):
                usdt = a
                if str(a.get("asset", a.get("coin", ""))).upper() == "USDT":
                    break
        if usdt is None:
            raise ExchangeError(f"موجودی فیوچرز USDT در پاسخ Toobit پیدا نشد: {str(data)[:200]}")

        def _f(*keys, default=0.0):
            for k in keys:
                v = usdt.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return default

        balance = _f("balance", "walletBalance", "total")
        available = _f("availableMargin", "availableBalance", "available", "free", default=balance)
        unrealized = _f("unrealizedPnL", "unrealisedPnl", "crossUnRealizedPnl", default=0.0)
        margin = _f("positionMargin", "usedMargin", "margin", default=max(0.0, balance - available))
        return {
            "balance": balance,
            "equity": balance + unrealized,
            "currency": "USDT",
            "margin": margin,
            "free_margin": available,
        }

    async def get_open_positions(self, symbol: str = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        data = await self._request("GET", "/api/v1/futures/positions", params, signed=True)
        raw_list = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else []
        positions = []
        for p in raw_list:
            if not isinstance(p, dict):
                continue
            try:
                qty = abs(float(p.get("position", p.get("positionAmt", p.get("qty", 0))) or 0))
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            side_raw = str(p.get("side", "")).upper()
            side = "long" if side_raw in ("LONG", "BUY") else ("short" if side_raw in ("SHORT", "SELL") else "long")

            def _pf(*keys, default=0.0):
                for k in keys:
                    v = p.get(k)
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return default

            positions.append({
                "id": str(p.get("positionId", p.get("id", f"{p.get('symbol')}-{side}"))),
                "symbol": p.get("symbol", symbol),
                "side": side,
                "qty": qty,
                "entry_price": _pf("avgPrice", "entryPrice", "avgEntryPrice"),
                "mark_price": _pf("markPrice", "lastPrice"),
                "leverage": _pf("leverage", default=1.0),
                "profit": _pf("unrealizedPnL", "unrealisedPnl", "profit"),
                "margin": _pf("margin", "positionMargin", "isolatedMargin"),
            })
        return positions

    async def _exchange_info(self) -> dict:
        """پاسخ خام exchangeInfo با کش یک‌ساعته (لیست نمادها و فیلترها)."""
        now = time.time()
        if self._exchange_info_cache is not None and now - self._exchange_info_time < 3600:
            return self._exchange_info_cache
        data = await self._request("GET", "/api/v1/exchangeInfo")
        if isinstance(data, dict):
            self._exchange_info_cache = data
            self._exchange_info_time = now
        return data if isinstance(data, dict) else {}

    def _iter_contracts(self, data: dict):
        for group_key in ("contracts", "symbols"):
            for s in (data.get(group_key, []) if isinstance(data, dict) else []):
                if isinstance(s, dict):
                    yield s

    async def list_symbols(self) -> list:
        """لیست همه‌ی نمادهای قابل معامله در فیوچرز، مرتب‌شده بر اساس نام.
        خروجی: [{symbol, base, quote}] با فرمت پرپچوال (…-SWAP-…)."""
        data = await self._exchange_info()
        result = []
        seen = set()
        for s in self._iter_contracts(data):
            sym = str(s.get("symbol", "")).upper()
            if not sym:
                continue
            # وضعیت غیرقابل معامله (اگر اعلام شود) حذف می‌شود
            status = str(s.get("status", s.get("state", "TRADING"))).upper()
            if status not in ("TRADING", "ONLINE", "OPEN", ""):
                continue
            # اگر فرمت -SWAP- نبود (مثلاً BTCUSDT برگشت)، به فرمت پرپچوال تبدیل کن؛
            # اگر قابل تبدیل نبود (نماد اسپات/ناشناخته)، رد کن
            if "-SWAP-" not in sym:
                converted = normalize_symbol(sym)
                if "-SWAP-" not in converted:
                    continue
                sym = converted
            if sym in seen:
                continue
            seen.add(sym)
            base = str(s.get("baseAsset", s.get("underlying", sym.split("-SWAP-")[0]))).upper()
            quote = str(s.get("quoteAsset", sym.split("-SWAP-")[-1])).upper()
            result.append({"symbol": sym, "base": base, "quote": quote})
        result.sort(key=lambda x: x["symbol"])
        return result

    async def get_symbol_info(self, symbol: str) -> dict:
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        data = await self._exchange_info()
        found = None
        for s in self._iter_contracts(data):
            if s.get("symbol") == symbol:
                found = s
                break
        info = {"min_qty": 0.0, "qty_step": 0.0, "price_step": 0.0, "contract_multiplier": 1.0}
        if found:
            try:
                info["contract_multiplier"] = float(found.get("contractMultiplier", 1.0) or 1.0)
            except (TypeError, ValueError):
                pass
            for f in found.get("filters", []):
                ft = f.get("filterType", "")
                try:
                    if ft == "LOT_SIZE":
                        info["min_qty"] = float(f.get("minQty", 0) or 0)
                        info["qty_step"] = float(f.get("stepSize", 0) or 0)
                    elif ft == "PRICE_FILTER":
                        info["price_step"] = float(f.get("tickSize", 0) or 0)
                except (TypeError, ValueError):
                    continue
        self._symbol_info_cache[symbol] = info
        return info

    async def set_leverage(self, symbol: str, leverage: int):
        await self._request("POST", "/api/v1/futures/leverage", {
            "symbol": symbol, "leverage": int(leverage),
        }, signed=True)

    async def place_order(self, side: str, symbol: str, qty: float,
                          stop_loss: float = None, take_profit: float = None) -> dict:
        if side not in ("buy", "sell"):
            raise ExchangeError(f"سمت سفارش نامعتبر: {side}")
        order_side = "BUY_OPEN" if side == "buy" else "SELL_OPEN"
        params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "priceType": "MARKET",   # اجرای مارکت (مطابق مدل سفارش Toobit: type=LIMIT + priceType=MARKET)
            "quantity": self._fmt_num(qty),
        }
        order = await self._request("POST", "/api/v1/futures/order", params, signed=True)

        result = {"orderId": str(order.get("orderId", order.get("id", ""))), "raw": order}

        # ست کردن TP/SL روی پوزیشن — جدا از سفارش ورود. اگر شکست بخورد، پوزیشن
        # باز مانده است؛ پس خطا raise نمی‌شود بلکه tp_sl_set=False برمی‌گردد تا
        # engine هشدار واضح در لاگ داشبورد ثبت کند و کاربر دستی اقدام کند.
        if stop_loss or take_profit:
            pos_side = "LONG" if side == "buy" else "SHORT"
            tp_sl_params = {"symbol": symbol, "side": pos_side}
            if take_profit:
                tp_sl_params["takeProfit"] = self._fmt_num(take_profit)
            if stop_loss:
                tp_sl_params["stopLoss"] = self._fmt_num(stop_loss)
            try:
                await self._request("POST", "/api/v1/futures/position/trading-stop", tp_sl_params, signed=True)
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
        side = position.get("side", "long")
        qty = position.get("qty", 0)
        if qty <= 0:
            raise ExchangeError("حجم پوزیشن برای بستن نامعتبر است.")
        order_side = "SELL_CLOSE" if side == "long" else "BUY_CLOSE"
        params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "priceType": "MARKET",
            "quantity": self._fmt_num(qty),
        }
        order = await self._request("POST", "/api/v1/futures/order", params, signed=True)
        return {"orderId": str(order.get("orderId", order.get("id", ""))), "closed": True}

    @staticmethod
    def _fmt_num(v) -> str:
        """عدد را بدون نماد علمی و بدون صفرهای اضافه به رشته تبدیل می‌کند."""
        s = f"{float(v):.10f}".rstrip("0").rstrip(".")
        return s if s else "0"
