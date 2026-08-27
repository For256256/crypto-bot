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
import uuid

import httpx
import pandas as pd

from app.core.exchanges.base import ExchangeDriver, ExchangeError

# کارمزد تیکر فیوچرز توبیت (VIP0، طبق toobit.com/support/fee-rate) — همه‌ی
# سفارش‌های ربات priceType=MARKET هستند، پس همیشه به‌عنوان تیکر پر می‌شوند.
TAKER_FEE_RATE = 0.0006

# پیشوند مسیرهای کپی‌ترید (نسخه‌ی ۲). طبق مستند رسمی، امضای این مسیرها همان
# امضای مشترک بقیه‌ی API است (HMAC-SHA256 روی کوئری + هدر X-BB-APIKEY)، پس
# _request بدون تغییر کار می‌کند؛ فقط پاسخ در پوشش {code, msg, data} می‌آید.
# شرط دسترسی: نوع کلید API باید COPY_TRADING باشد — کلید فیوچرز معمولی رد می‌شود.
COPY_TRADING_PREFIX = "/api/v2/copy-trading"


def _f(v, default: float | None = None):
    """تبدیل امن به float — اعداد این اندپوینت‌ها همه به‌صورت رشته می‌آیند."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

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
        # نوع سفارشی که TP/SL این حساب زیر آن برمی‌گردد؛ بعد از اولین پاسخ
        # موفق کش می‌شود تا هر tick فقط یک درخواست بزنیم.
        self._tpsl_order_type: str | None = None

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

    # ---------- کپی‌ترید (حساب لیدر) ----------
    @staticmethod
    def _v2_data(payload):
        """محتوای واقعی را از پوشش {code, msg, data} بیرون می‌کشد.

        بعضی اندپوینت‌ها مستقیم آرایه می‌دهند و بعضی آرایه را داخل کلیدی مثل
        list/items می‌گذارند؛ چون شکل دقیق پاسخ همه‌شان مستند نشده، محتاطانه
        هر دو حالت پوشش داده می‌شود.
        """
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            for key in ("list", "items", "rows", "records"):
                if isinstance(data.get(key), list):
                    return data[key]
        return data

    async def get_leader_config(self) -> dict:
        """تنظیمات حساب لیدر. اگر کلید از نوع COPY_TRADING نباشد ExchangeError
        می‌دهد — دقیقاً همین رفتار برای تشخیص نوع کلید استفاده می‌شود."""
        payload = await self._request("GET", f"{COPY_TRADING_PREFIX}/leader/config", signed=True)
        data = self._v2_data(payload)
        return data if isinstance(data, dict) else {}

    async def get_leader_symbols(self) -> list:
        """نمادهایی که این حساب لیدر اجازه‌ی کپی‌ترید رویشان دارد.

        خروجی: [{"symbol": "BTC-SWAP-USDT", "leverage": 20.0, "is_lead": True}]
        """
        payload = await self._request("GET", f"{COPY_TRADING_PREFIX}/leader/symbols", signed=True)
        data = self._v2_data(payload)
        out = []
        for row in (data if isinstance(data, list) else []):
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbolId") or row.get("symbol")
            if not symbol:
                continue
            try:
                leverage = float(row.get("leverage") or 0) or None
            except (TypeError, ValueError):
                leverage = None
            out.append({
                "symbol": symbol,
                "leverage": leverage,
                # isLead=0 یعنی نماد در لیست هست ولی کپی‌ترید رویش خاموش است
                "is_lead": str(row.get("isLead", 1)) not in ("0", "False", "false"),
            })
        return out

    async def get_leader_followers(self, page: int = 1, size: int = 50) -> dict:
        """فهرست دنبال‌کننده‌ها به‌همراه تعداد کل.

        پاسخ این اندپوینت صفحه‌بندی‌شده است ({pages, total, list}) و پیش‌فرض
        هر صفحه ۱۰ ردیف است. برای همین «تعداد فالوور» باید از فیلد total
        خوانده شود نه از طول لیست — وگرنه هر لیدری با بیش از یک صفحه
        دنبال‌کننده، عدد ناقص می‌دید.
        """
        payload = await self._request(
            "GET", f"{COPY_TRADING_PREFIX}/leader/followers",
            {"page": max(int(page), 1), "size": max(min(int(size), 100), 1)}, signed=True)
        data = payload.get("data") if isinstance(payload, dict) else payload
        rows, total = [], None
        if isinstance(data, dict):
            rows = data.get("list") if isinstance(data.get("list"), list) else []
            total = data.get("total")
        elif isinstance(data, list):
            rows = data
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "nickname": r.get("followerNickname") or "—",
                "amount_limit": _f(r.get("followAmountLimit")),
                "margin": _f(r.get("totalFollowMargin")),
                "profit": _f(r.get("totalProfit")),
                "running_ms": _f(r.get("followRunningMills")),
            })
        try:
            total = int(float(total)) if total is not None else len(out)
        except (TypeError, ValueError):
            total = len(out)
        return {"total": total, "list": out}

    async def get_leader_trade_data(self, period: int = 30) -> dict:
        """آمار رسمی لیدری از نگاه صرافی — همان اعدادی که دنبال‌کننده‌های
        بالقوه در پروفایل شما می‌بینند.

        بازه‌های مجاز صرافی ثابت‌اند (۷/۳۰/۹۰/۱۸۰/۳۶۵ روز)؛ نزدیک‌ترین مقدار
        به بازه‌ی انتخابی کاربر فرستاده می‌شود.
        """
        allowed = (7, 30, 90, 180, 365)
        period = min(allowed, key=lambda a: abs(a - int(period or 30)))
        payload = await self._request("GET", f"{COPY_TRADING_PREFIX}/leader/trade-data",
                                      {"type": period}, signed=True)
        data = self._v2_data(payload)
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return {}
        return {
            "period_days": period,
            "profit_rate": _f(data.get("profitRate")),
            "accumulated_profit": _f(data.get("accumulatedProfit")),
            "order_count": _f(data.get("orderCount")),
            "win_rate": _f(data.get("winRate")),
            "current_followers": _f(data.get("currentFollowerCount")),
            "total_followers": _f(data.get("totalFollowerCount")),
            "follower_total_profit": _f(data.get("followerTotalProfit")),
            "sharpe_ratio": _f(data.get("sharpeRatio")),
            "aum": _f(data.get("assetManagementScale")),
            # این یکی رشته‌ی نمایشی است (مثلاً «2.18:1»)، نه عدد
            "profit_loss_ratio": data.get("profitLossRatio"),
            "trading_frequency": _f(data.get("tradingFrequency")),
            "trading_days": _f(data.get("tradingDays")),
        }

    async def get_leader_profit_sharings(self) -> list:
        """تاریخچه‌ی هفتگی تقسیم سود با دنبال‌کننده‌ها.

        این درآمد دومِ یک حساب کپی‌ترید است و در سود/زیان معاملات خودِ حساب
        اصلاً دیده نمی‌شود؛ به همین دلیل گزارش کپی‌ترید باید جدا باشد.
        """
        payload = await self._request("GET", f"{COPY_TRADING_PREFIX}/leader/profit-sharings/history",
                                      signed=True)
        data = self._v2_data(payload)
        out = []
        for r in (data if isinstance(data, list) else []):
            if not isinstance(r, dict):
                continue
            out.append({
                "date": r.get("sharingDate") or "",
                "year": r.get("year"),
                "week": r.get("weekOfYear"),
                "total": _f(r.get("totalProfitSharing")),
                "referral_share": _f(r.get("recommendUserShare")),
                "net": _f(r.get("realProfitShare")),
            })
        out.sort(key=lambda x: str(x["date"]), reverse=True)
        return out

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

    # flowType های دفتر مالی توبیت (از مستندات رسمی):
    #   10 کارمزد · 28 سود/زیان محقق‌شده · 32 کارمزد فاندینگ
    #   51 انتقال بین حساب‌ها (همان واریز/برداشت کاربر) · 700 لیکویید · 701 ADL
    FLOW_TRANSFER = 51

    async def get_net_transfers(self) -> float | None:
        """جمع خالص واریز و برداشت کاربر روی حساب فیوچرز (USDT).

        چرا لازم است: «سرمایه‌ی واریزشده» را قبلاً از روی موجودی منهای مجموع
        سود معاملات حساب می‌کردیم. آن رابطه فقط وقتی دقیق است که هر تغییر
        موجودی، معامله‌ای ثبت‌شده پشتش باشد — ولی کارمزد فاندینگ (flowType 32)
        هر چند ساعت موجودی را تکان می‌دهد بدون هیچ معامله‌ای، و کارمزد واقعی
        معاملات live هم فقط تخمین زده می‌شود. نتیجه این بود که عدد سرمایه
        مدام می‌لغزید. اینجا خودِ دفتر صرافی خوانده می‌شود که مرجع است.

        None یعنی نشد خواند؛ در آن حالت لایه‌ی بالاتر به همان محاسبه‌ی تقریبی
        برمی‌گردد، نه اینکه عدد غلط نشان بدهد.
        """
        total = 0.0
        from_id = None
        try:
            for _ in range(20):        # سقف صفحه‌بندی، تا حلقه بی‌پایان نشود
                params = {"flowType": self.FLOW_TRANSFER, "limit": 200}
                if from_id:
                    params["fromId"] = from_id
                rows = await self._request("GET", "/api/v1/futures/balanceFlow",
                                           params, signed=True)
                if not isinstance(rows, list) or not rows:
                    break
                for r in rows:
                    if str(r.get("coin", "USDT")).upper() not in ("USDT", ""):
                        continue
                    # اگر صرافی فیلتر flowType را نادیده گرفت، اینجا دوباره چک می‌شود
                    ft = r.get("flowType")
                    if ft is not None and int(ft) != self.FLOW_TRANSFER:
                        continue
                    try:
                        total += float(r.get("change") or 0.0)
                    except (TypeError, ValueError):
                        continue
                if len(rows) < 200:
                    break
                from_id = rows[-1].get("id")
                if not from_id:
                    break
            return total
        except ExchangeError:
            return None

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
                # طبق مستند رسمی، پاسخ پوزیشن‌ها اصلاً فیلد حد ضرر/سود ندارد.
                # مقدارشان از سفارش‌های شرطیِ باز خوانده می‌شود (پایین‌تر).
                "stop_loss": None,
                "take_profit": None,
            })
        await self._attach_targets(positions)
        return positions

    async def _fetch_conditional_orders(self) -> list:
        """سفارش‌های شرطی باز (همان چیزی که trading-stop می‌سازد).

        نوع درست طبق مستند STOP_PROFIT_LOSS است، ولی بعضی حساب‌ها/نسخه‌ها
        همان سفارش را زیر STOP برمی‌گردانند. برای اینکه هر tick دو درخواست
        نزنیم، اولین نوعی که جواب داد روی همین درایور کش می‌شود.
        """
        types = [self._tpsl_order_type] if self._tpsl_order_type else ["STOP_PROFIT_LOSS", "STOP"]
        for otype in types:
            try:
                data = await self._request("GET", "/api/v1/futures/openOrders",
                                           {"type": otype, "limit": 500}, signed=True)
            except ExchangeError:
                continue
            orders = data if isinstance(data, list) else data.get("orders", []) if isinstance(data, dict) else []
            orders = [o for o in orders if isinstance(o, dict)]
            if orders:
                self._tpsl_order_type = otype
                return orders
        return []

    async def _attach_targets(self, positions: list):
        """حد ضرر/سود هر پوزیشن را از سفارش‌های شرطی باز پیدا و ضمیمه می‌کند.

        تفکیک SL از TP از روی جهت پوزیشن و مقایسه‌ی قیمت تریگر با قیمت ورود
        انجام می‌شود: برای Long، تریگرِ پایین‌تر از ورود حد ضرر است و بالاتر
        حد سود؛ برای Short برعکس. خودِ صرافی این دو را با برچسب جدا از هم
        متمایز نمی‌کند.
        """
        if not positions:
            return
        try:
            orders = await self._fetch_conditional_orders()
        except Exception:
            return
        if not orders:
            return
        by_symbol: dict = {}
        for o in orders:
            sym = o.get("symbol")
            try:
                trigger = float(o.get("stopPrice") or 0)
            except (TypeError, ValueError):
                continue
            if not sym or trigger <= 0:
                continue
            by_symbol.setdefault(sym, []).append((trigger, str(o.get("side", "")).upper()))

        for p in positions:
            triggers = by_symbol.get(p["symbol"])
            if not triggers:
                continue
            entry = p.get("entry_price") or 0
            if entry <= 0:
                continue
            is_long = p["side"] == "long"
            # سفارش‌های بستنِ همین جهت؛ اگر side نامشخص بود، همه را در نظر می‌گیریم
            wanted_side = "SELL_CLOSE" if is_long else "BUY_CLOSE"
            mine = [t for t, s in triggers if s in (wanted_side, "")] or [t for t, _ in triggers]
            below = [t for t in mine if t < entry]
            above = [t for t in mine if t > entry]
            if is_long:
                p["stop_loss"] = max(below) if below else None
                p["take_profit"] = min(above) if above else None
            else:
                p["stop_loss"] = min(above) if above else None
                p["take_profit"] = max(below) if below else None

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
            "newClientOrderId": self._gen_client_order_id(),
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
            "newClientOrderId": self._gen_client_order_id(),
        }
        order = await self._request("POST", "/api/v1/futures/order", params, signed=True)
        return {"orderId": str(order.get("orderId", order.get("id", ""))), "closed": True}

    @staticmethod
    def _fmt_num(v) -> str:
        """عدد را بدون نماد علمی و بدون صفرهای اضافه به رشته تبدیل می‌کند."""
        s = f"{float(v):.10f}".rstrip("0").rstrip(".")
        return s if s else "0"

    @staticmethod
    def _gen_client_order_id() -> str:
        """شناسه‌ی یکتای سفارش که Toobit اکنون برای ثبت سفارش الزامی کرده است."""
        return f"cb{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"
