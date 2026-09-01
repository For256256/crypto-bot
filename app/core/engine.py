"""
موتور اجرای ربات — برای هر حساب یک AccountRunner با حلقه‌ی asyncio مستقل.

مسئولیت‌ها:
- خواندن کندل‌ها و اجرای استراتژی هر نماد در بازه‌ی poll_interval_seconds
- باز/بستن پوزیشن (paper یا live) با مدیریت ریسک: حجم بر اساس ٪ریسک و فاصله‌ی SL
- ساخت خودکار SL/TP از ATR اگر وبهوک مقدار ندهد (فاصله‌ی هر دو یکسان و برابر
  sl_tp_atr_mult×ATR هر حساب — پیش‌فرض ۳×ATR — تا نسبت ریسک:پاداش ۱:۱ باشد)
- سقف ضرر روزانه (UTC): با عبور از آن، ورودی جدید تا فردا ممنوع می‌شود
- سیاست recycle: با پر بودن ظرفیت، سودده‌ترین پوزیشن بسته می‌شود تا جای سیگنال جدید باز شود
- دریافت سیگنال وبهوک TradingView و توزیع آن بین حساب‌های فعال
- ثبت تاریخچه‌ی معاملات و نقاط اکوییتی (هر ۵ دقیقه) برای گزارش داشبورد
"""
import asyncio
import math
from collections import deque
from datetime import datetime, timezone

from app.core import config_store, history, position_targets, tokens, users
from app.core.exchanges.base import ExchangeError
from app.core.exchanges.factory import build_driver
from app.core.exchanges.paper import PaperDriver
from app.core.exchanges.toobit import TAKER_FEE_RATE as TOOBIT_TAKER_FEE_RATE
from app.core.exchanges.toobit import normalize_symbol as _toobit_normalize_symbol
from app.core.exchanges.tabdeal import normalize_symbol as _tabdeal_normalize_symbol

# نگاشت صرافی → تابع نرمال‌سازی نماد آن (فرمت هر صرافی متفاوت است، مثلاً
# BTC-SWAP-USDT در توبیت در برابر BTCUSDT در تبدیل)
_NORMALIZE_SYMBOL = {
    "toobit": _toobit_normalize_symbol,
    "tabdeal": _tabdeal_normalize_symbol,
}


def normalize_symbol_for(exchange: str, raw: str) -> str:
    fn = _NORMALIZE_SYMBOL.get(exchange, _toobit_normalize_symbol)
    return fn(raw)
from app.core.strategies.registry import run_strategy, STRATEGIES
from app.core.strategies import trend as trend_filter

EQUITY_SNAPSHOT_SECONDS = 300
# فاصله‌ی بررسی انقضای توکن فعال‌سازی معاملات واقعی حساب‌های کاربران عادی
TOKEN_CHECK_INTERVAL_SECONDS = 300
# فاصله‌ی پیش‌فرض SL/TP از قیمت ورود، به ضریب ATR — قابل تنظیم در هر حساب
# (sl_tp_atr_mult). هر دو یک ضریب مشترک دارند تا نسبت ریسک:پاداش ۱:۱ بماند و
# فاصله‌ی بیشتری تا حد ضرر باشد، برای کاهش برخورد زودهنگام به SL.
DEFAULT_SL_TP_ATR_MULT = 3.0
# سقف مارجین هر معامله از کل اکوییتی حساب — مستقل از فاصله‌ی SL. بدون این سقف،
# وقتی حد ضرر (خودکار از ATR) خیلی به قیمت نزدیک باشد، فرمول ریسک‌محور می‌تواند
# حجمی بسازد که تقریباً کل حساب را فقط برای یک پوزیشن به مارجین قفل کند.
# قابل تنظیم در هر حساب (max_margin_per_trade_pct)؛ این فقط مقدار پیش‌فرض است.
DEFAULT_MAX_MARGIN_PER_TRADE_PCT = 25.0


def _num_or_none(v):
    """float یا None — رکورد ناقص صرافی نباید حلقه را بشکند."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # NaN را هم رد می‌کند


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> str:
    return _utc_now().strftime("%Y-%m-%d")


class AccountRunner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.account_id = cfg["id"]
        self.driver = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.status, self.status_key = "متوقف", "stopped"
        self.logs: deque = deque(maxlen=100)
        self.last_signals: dict = {}
        # رویدادهای باز/بسته شدن معامله برای اعلان مرورگر. در حافظه است و با
        # ری‌استارت پاک می‌شود — که همان رفتار درست است: نباید بعد از بالا آمدن
        # دوباره، اعلان معامله‌های دیروز پشت سر هم بیاید.
        self.notify_events: deque = deque(maxlen=20)
        self._event_seq = 0
        # مجموع خالص واریز/برداشت خوانده‌شده از دفتر صرافی، با زمان آخرین خواندن.
        # هر tick خوانده نمی‌شود چون یک درخواست امضاشده‌ی اضافه است و این عدد
        # فقط وقتی عوض می‌شود که کاربر پول جابه‌جا کند.
        self._net_transfers: float | None = None
        self._net_transfers_at: float = 0.0
        self.account_info: dict | None = None
        self.positions: list = []
        self._last_equity_snapshot = 0.0
        self._daily = {"date": _today_utc(), "start_equity": None, "blocked": False}
        # شناسایی پوزیشن‌های live که خارج از ربات (دستی/SL صرافی) بسته شده‌اند
        self._known_live_positions: dict = {}
        # حد ضرر/سود هر پوزیشن live باز — چون اندپوینت پوزیشن‌های توبیت این
        # مقادیر را برنمی‌گرداند، خودمان لحظه‌ی باز کردن نگه می‌داریم تا وقتی
        # پوزیشن در سمت صرافی بسته شد بتوانیم بفهمیم SL خورده یا TP.
        self._live_position_targets: dict = {}
        # فقط یک‌بار درباره‌ی نبودن حد ضرر/سود هشدار می‌دهیم، وگرنه هر tick
        # لاگ را پر می‌کند.
        self._tpsl_diag_logged = False
        # ---- فیلتر روند تایم‌فریم بالاتر ----
        # کش کندل‌های تایم‌فریم روند. کندل ۴ ساعته هر ۴ ساعت عوض می‌شود ولی
        # حلقه‌ی ربات هر دقیقه اجرا می‌شود؛ بدون کش، هر tick یک درخواست اضافه
        # به صرافی می‌رفت که نه لازم است نه مؤدبانه.
        self._trend_cache: dict = {}
        # اطلاعات کپی‌ترید هر پوزیشن (چند فالوور، چه سرمایه‌ای). در _tick
        # تازه می‌شود نه در status_dict — چون status_dict همگام است و
        # داشبورد آن را با هر پول‌کردن صدا می‌زند.
        self._lead_orders: dict = {}
        self._lead_orders_at: float = 0.0
        # فقط یک‌بار درباره‌ی کوتاه بودن تاریخچه‌ی تایم‌فریم روند هشدار بدهیم
        self._trend_history_warned: set = set()

    # ---------- لاگ ----------
    def log(self, message: str, level: str = "info"):
        self.logs.append({
            "time": _utc_now().isoformat(timespec="seconds"),
            "level": level,
            "message": message,
        })

    def _notify_owner(self, text: str, kind: str = "alert", symbol: str = ""):
        """گزارش به مالک این حساب.

        kind="trade" یعنی باز/بسته شدن معامله؛ این‌ها با سوییچ‌های همین حساب
        قابل خاموش‌کردن‌اند. kind="alert" برای خطای صرافی و سقف ضرر روزانه است
        و عمداً از سوییچ‌ها عبور می‌کند — خاموش‌کردن گزارش معاملات نباید یعنی
        بی‌خبر ماندن از خراب‌شدن ربات.
        """
        is_trade = (kind == "trade")

        # ---- اعلان مرورگر: در یک صف کوچک می‌ماند تا داشبورد آن را بردارد ----
        if is_trade and self.cfg.get("notify_browser", True):
            self._event_seq += 1
            self.notify_events.append({
                "id": self._event_seq,
                "symbol": symbol,
                "text": text,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })

        # ---- تلگرام ----
        if is_trade and not self.cfg.get("notify_telegram", True):
            return
        owner_id = self.cfg.get("owner_id")
        if not owner_id:
            return
        from app.core import telegram
        try:
            asyncio.create_task(telegram.notify_user(owner_id, f"[{self.cfg.get('name', '')}] {text}"))
        except RuntimeError:
            pass

    # ---------- چرخه‌ی حیات ----------
    async def start(self):
        if self.running:
            return
        self.driver = build_driver(self.cfg.get("trading_mode", "paper"), self.cfg)
        await self.driver.connect()
        self.running = True
        self.status, self.status_key = "فعال", "running"
        mode_fa = "کاغذی (paper)" if self.cfg.get("trading_mode") == "paper" else "⚠️ واقعی (LIVE)"
        self.log(f"ربات در حالت {mode_fa} شروع شد.")
        await self._check_copy_trading_symbols()
        self.task = asyncio.create_task(self._loop())

    async def _check_copy_trading_symbols(self):
        """روی حساب کپی‌ترید، نمادهایی که صرافی اجازه‌ی لیدری رویشان نداده را
        همان اول هشدار می‌دهد.

        بدون این، اولین سیگنال با یک خطای مبهم صرافی رد می‌شد و کاربر تازه بعد
        از از دست دادن آن سیگنال می‌فهمید نماد اصلاً مجاز نبوده.
        عمداً ربات را متوقف نمی‌کند: ممکن است بقیه‌ی نمادهای همین حساب سالم باشند.
        """
        if self.cfg.get("account_type") != "copy_trading":
            return
        getter = getattr(self.driver, "get_leader_symbols", None)
        if getter is None:
            return
        try:
            allowed = await getter()
        except Exception as e:
            self.log(f"خواندن نمادهای مجاز کپی‌ترید ناموفق بود: {e}", "warn")
            return
        tradable = {s["symbol"] for s in allowed if s.get("is_lead")}
        if not tradable:
            self.log("هیچ نمادی روی حساب کپی‌ترید فعال نیست — در اپ توبیت نمادهای لیدری را روشن کنید.", "warn")
            return
        for sym_cfg in self.cfg.get("symbols", []):
            if not sym_cfg.get("enabled", True):
                continue
            symbol = sym_cfg["symbol"]
            if symbol not in tradable:
                self.log(
                    f"{symbol}: روی حساب کپی‌ترید مجاز نیست (مجازها: {'، '.join(sorted(tradable))}).",
                    "warn",
                )

    async def stop(self):
        self.running = False
        self.status, self.status_key = "متوقف", "stopped"
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        if self.driver is not None:
            try:
                await self.driver.close()
            except Exception:
                pass
            self.driver = None
        self.account_info = None
        self.positions = []

    # ---------- حلقه‌ی اصلی ----------
    async def _loop(self):
        interval = max(10, int(self.cfg.get("poll_interval_seconds", 60)))
        while self.running:
            was_error = self.status in ("خطای صرافی", "خطا")
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except ExchangeError as e:
                self.status, self.status_key = "خطای صرافی", "exchange_error"
                self.log(f"خطای صرافی: {e}", "error")
                if not was_error:
                    self._notify_owner(f"⚠️ خطای صرافی: {e}")
            except Exception as e:
                self.status, self.status_key = "خطا", "error"
                self.log(f"خطای غیرمنتظره: {e}", "error")
                if not was_error:
                    self._notify_owner(f"⚠️ خطای غیرمنتظره: {e}")
            await asyncio.sleep(interval)

    async def _tick(self):
        loop = asyncio.get_event_loop()

        # ۱) وضعیت حساب و پوزیشن‌ها
        self.account_info = await self.driver.get_account_info()
        self.positions = await self.driver.get_open_positions()
        self.status, self.status_key = "فعال", "running"
        self._diagnose_missing_targets()
        await self._refresh_net_transfers()
        await self._refresh_lead_orders()
        await self._update_trailing_stops()

        # ۲) ثبت نقطه‌ی اکوییتی (هر ۵ دقیقه)
        now_ts = loop.time()
        if now_ts - self._last_equity_snapshot >= EQUITY_SNAPSHOT_SECONDS:
            self._last_equity_snapshot = now_ts
            history.record_equity(
                self.account_id, self.cfg.get("trading_mode", "paper"),
                float(self.account_info.get("equity", 0)), float(self.account_info.get("balance", 0)),
            )

        # ۳) ثبت معاملات بسته‌شده
        await self._collect_closed_trades()

        # ۴) کنترل سقف ضرر روزانه
        self._check_daily_loss()

        # ۵) اجرای استراتژی نمادها
        for sym_cfg in list(self.cfg.get("symbols", [])):
            if not sym_cfg.get("enabled", True):
                continue
            await self._process_symbol(sym_cfg)

    def _diagnose_missing_targets(self):
        """اگر پوزیشن باز داریم ولی هیچ حد ضرر/سودی از هیچ منبعی پیدا نشد،
        یک‌بار در لاگ می‌گوییم — وگرنه کاربر فقط خط تیره می‌بیند و نمی‌داند چرا."""
        if self._tpsl_diag_logged or isinstance(self.driver, PaperDriver):
            return
        if not self.positions:
            return
        missing = []
        for p in self.positions:
            if p.get("stop_loss") or p.get("take_profit"):
                continue
            if position_targets.get_targets(self.account_id, p.get("symbol"), p.get("side")):
                continue
            missing.append(p.get("symbol"))
        if not missing:
            return
        self._tpsl_diag_logged = True
        self.log(
            "حد ضرر/سود این پوزیشن‌ها پیدا نشد: " + "، ".join(m for m in missing if m) + ". "
            "اگر روی خود صرافی برایشان حد ضرر/سود تنظیم نشده، همین درست است. "
            "اگر تنظیم شده ولی اینجا خالی است، یعنی صرافی آن را در سفارش‌های "
            "شرطی باز برنمی‌گرداند — این پیام را به پشتیبانی بدهید.",
            "warn",
        )

    # ---------- ثبت معاملات ----------
    async def _collect_closed_trades(self):
        mode = self.cfg.get("trading_mode", "paper")
        if isinstance(self.driver, PaperDriver):
            for trade in self.driver.drain_closed_trades():
                history.record_trade(self.account_id, mode, trade)
                realized = trade.get("realized", 0)
                self.log(
                    f"معامله‌ی {trade['symbol']} بسته شد ({trade.get('closed_by')}) — "
                    f"سود/زیان خالص از کارمزد: {realized:+.2f} USDT",
                    "info" if realized >= 0 else "warn",
                )
                emoji = "✅" if realized >= 0 else "🔴"
                self._notify_owner(
                    f"{emoji} معامله‌ی {trade['symbol']} بسته شد ({trade.get('closed_by')}) — "
                    f"سود/زیان خالص: {realized:+.2f} USDT",
                    kind="trade", symbol=trade.get("symbol", ""),
                )
        else:
            # live: پوزیشنی که قبلاً می‌دیدیم و الان نیست یعنی در صرافی بسته شده
            current_ids = {p["id"]: p for p in self.positions}
            for pid, prev in list(self._known_live_positions.items()):
                if pid not in current_ids:
                    targets = (self._live_position_targets.pop(pid, None)
                               or position_targets.get_targets(self.account_id,
                                                               prev.get("symbol"), prev.get("side")))
                    position_targets.clear_targets(self.account_id, prev.get("symbol"), prev.get("side"))
                    closed_by = self._infer_closed_by(prev, targets)
                    gross = prev.get("profit") or 0.0
                    fee = await self._estimate_live_fee(prev["symbol"], prev.get("entry_price"),
                                                        prev.get("mark_price"), prev.get("qty"))
                    realized = gross - fee
                    history.record_trade(self.account_id, mode, {
                        **prev,
                        # صفحه‌ی گزارشات زمان ورود را نشان می‌دهد؛ در حالت live
                        # تنها منبعش همین رکورد است و بعد از پاک‌شدن از بین می‌رود.
                        "open_time": prev.get("open_time") or (targets or {}).get("open_time"),
                        "close_price": prev.get("mark_price"),
                        "realized": realized,
                        "fee": fee,
                        "closed_by": closed_by,
                        "estimated": True,
                        "close_time": _utc_now().isoformat(timespec="seconds"),
                    })
                    label = {"SL": "حد ضرر", "TP": "حد سود"}.get(closed_by, closed_by)
                    self.log(
                        f"پوزیشن {prev['symbol']} در سمت صرافی بسته شد ({label}) "
                        f"(سود/زیان تقریبی خالص از کارمزد: {realized:+.2f} USDT — کارمزد برآوردی: {fee:.2f})"
                    )
                    emoji = "✅" if realized >= 0 else "🔴"
                    self._notify_owner(
                        f"{emoji} پوزیشن {prev['symbol']} در سمت صرافی بسته شد ({label}) — "
                        f"سود/زیان تقریبی خالص: {realized:+.2f} USDT",
                        kind="trade", symbol=prev.get("symbol", ""),
                    )
            self._known_live_positions = current_ids

    async def _estimate_live_fee(self, symbol: str, entry_price: float | None,
                                 close_price: float | None, qty: float | None) -> float:
        """کارمزد تیکر رفت‌وبرگشت (ورود+خروج) یک پوزیشن live را تخمین می‌زند —
        چون توبیت کارمزد واقعی هر پوزیشن را در اندپوینت پوزیشن‌ها برنمی‌گرداند
        و سود/زیانِ گزارش‌شده‌ی آن (unrealizedPnL) قبل از کسر کارمزد است. با
        همین تخمین، معامله‌ای که به‌ظاهر سود کمی داشته ولی کارمزدش بیشتر از
        آن سود است، در گزارش به‌درستی زیان‌ده نشان داده می‌شود."""
        if not entry_price or not close_price or not qty:
            return 0.0
        try:
            info = await self.driver.get_symbol_info(symbol)
        except ExchangeError:
            info = {}
        cm = float(info.get("contract_multiplier", 1.0) or 1.0)
        return (float(entry_price) + float(close_price)) * float(qty) * cm * TOOBIT_TAKER_FEE_RATE

    @staticmethod
    def _infer_closed_by(prev: dict, targets: dict | None) -> str:
        """چون اندپوینت پوزیشن‌های توبیت مقدار SL/TP را برنمی‌گرداند، با آخرین
        قیمت شناخته‌شده (mark_price) و جهت پوزیشن حدس می‌زنیم کدام حد خورده
        است — نه با «کدام نزدیک‌تره» (که با یک حاشیه‌ی ثابت روی نمادهای
        کم‌قیمت مثل DASH که فاصله‌ی SLشان کوچک است، عملاً هر قیمتی را غلط SL
        لیبل می‌زد)، بلکه با اینکه آیا قیمت واقعاً از آن سطح، در همان جهتِ
        درست، رد شده یا نه. اگر هدفی ذخیره نشده باشد (مثلاً ربات بین باز و
        بسته شدن پوزیشن ری‌استارت شده) یا قیمت از هیچ‌کدام رد نشده باشد
        (بستن دستی از اپ صرافی، لیکویید شدن و ...)، «exchange» برمی‌گردد که
        در گزارش به‌صورت «صرافی» نمایش داده می‌شود."""
        price = prev.get("mark_price")
        side = prev.get("side")
        if not targets or price is None or side not in ("long", "short"):
            return "exchange"
        sl, tp = targets.get("stop_loss"), targets.get("take_profit")
        if side == "long":
            if sl and price <= sl:
                return "SL"
            if tp and price >= tp:
                return "TP"
        else:
            if sl and price >= sl:
                return "SL"
            if tp and price <= tp:
                return "TP"
        return "exchange"

    async def record_live_close(self, position: dict, closed_by: str):
        """ثبت معامله‌ی live که خودِ ربات بسته است (سود/زیان آخرین مقدار شناخته‌شده،
        منهای کارمزد برآوردی رفت‌وبرگشت)."""
        gross = position.get("profit") or 0.0
        fee = await self._estimate_live_fee(position["symbol"], position.get("entry_price"),
                                            position.get("mark_price"), position.get("qty"))
        realized = gross - fee
        stored = position_targets.get_targets(self.account_id, position.get("symbol"),
                                              position.get("side")) or {}
        history.record_trade(self.account_id, self.cfg.get("trading_mode", "paper"), {
            **position,
            "open_time": position.get("open_time") or stored.get("open_time"),
            "close_price": position.get("mark_price"),
            "realized": realized,
            "fee": fee,
            "closed_by": closed_by,
            "estimated": True,
            "close_time": _utc_now().isoformat(timespec="seconds"),
        })
        self._known_live_positions.pop(position.get("id"), None)
        self._live_position_targets.pop(position.get("id"), None)
        position_targets.clear_targets(self.account_id, position.get("symbol"),
                                       position.get("side"))
        emoji = "✅" if realized >= 0 else "🔴"
        label = {"SL": "حد ضرر", "TP": "حد سود", "manual": "دستی", "reversal": "تغییر جهت"}.get(closed_by, closed_by)
        self._notify_owner(f"{emoji} پوزیشن {position['symbol']} بسته شد ({label}) — سود/زیان: {realized:+.2f} USDT",
                           kind="trade", symbol=position.get("symbol", ""))

    # ---------- سقف ضرر روزانه ----------
    def _check_daily_loss(self):
        today = _today_utc()
        if self._daily["date"] != today:
            self._daily = {"date": today, "start_equity": None, "blocked": False}
        equity = float((self.account_info or {}).get("equity", 0) or 0)
        if equity <= 0:
            return
        if self._daily["start_equity"] is None:
            self._daily["start_equity"] = equity
            return
        max_loss = float(self.cfg.get("max_daily_loss_percent", 5.0))
        dd_pct = (self._daily["start_equity"] - equity) / self._daily["start_equity"] * 100
        if dd_pct >= max_loss and not self._daily["blocked"]:
            self._daily["blocked"] = True
            self.log(
                f"⛔ سقف ضرر روزانه ({max_loss}٪) رسید — تا فردا (UTC) ورودی جدید ممنوع است.",
                "error",
            )
            self._notify_owner(f"⛔ سقف ضرر روزانه ({max_loss}٪) رسید — تا فردا ورودی جدید ممنوع است.")
        elif self._daily["blocked"]:
            self.status, self.status_key = "متوقف (سقف ضرر روزانه)", "stopped_daily_loss"

    # ---------- فیلتر روند تایم‌فریم بالاتر ----------
    TREND_CACHE_TTL = 300      # ثانیه

    def _trend_settings(self) -> tuple[str, str, int]:
        tf = str(self.cfg.get("trend_filter_timeframe") or trend_filter.DEFAULT_TIMEFRAME)
        method = str(self.cfg.get("trend_filter_method") or trend_filter.DEFAULT_METHOD)
        length = int(self.cfg.get("trend_filter_ema_length") or trend_filter.DEFAULT_EMA_LENGTH)
        return tf, method, max(length, 2)

    async def _get_trend(self, symbol: str) -> dict | None:
        """جهت روند کلی این نماد در تایم‌فریم فیلتر. None یعنی فیلتر خاموش است.

        اگر خواندن کندل شکست بخورد، آخرین جهت شناخته‌شده برگردانده می‌شود
        (با پرچم stale) — روند ۴ ساعته با یک قطعی شبکه‌ی چنددقیقه‌ای عوض
        نمی‌شود و بستن کل ربات به‌خاطر یک خطای گذرا بدتر از استفاده از عدد
        کمی قدیمی است.
        """
        if not self.cfg.get("trend_filter_enabled"):
            return None
        tf, method, length = self._trend_settings()
        key = (symbol, tf, method, length)
        now = _utc_now().timestamp()
        cached = self._trend_cache.get(key)
        if cached and (now - cached[0]) < self.TREND_CACHE_TTL:
            return cached[1]

        # حاشیه‌ی گرم‌کردن اندیکاتور؛ سقف ۱۰۰۰ چون صرافی‌ها بیشتر نمی‌دهند
        need = min(max(length + 200, 300), 1000)
        try:
            df = await self.driver.get_candles(symbol, tf, need)
        except ExchangeError as e:
            if cached:
                self.log(f"{symbol}: خواندن کندل روند {tf} ناموفق ({e}) — از آخرین روند شناخته‌شده استفاده شد.", "warn")
                return {**cached[1], "stale": True}
            self.log(f"{symbol}: خواندن کندل روند {tf} ناموفق: {e}", "error")
            return {"direction": "unknown", "timeframe": tf, "method": method,
                    "ema_length": length, "error": str(e)}

        info = trend_filter.detect_trend(df, method, length)
        info["timeframe"] = tf
        info["stale"] = False
        if info.get("insufficient_history") and key not in self._trend_history_warned:
            self._trend_history_warned.add(key)
            self.log(
                f"{symbol}: تاریخچه‌ی تایم‌فریم {tf} کمتر از دوره‌ی اندیکاتور است "
                f"({len(df)} کندل) — جهت روند تخمینی است.",
                "warn",
            )
        self._trend_cache[key] = (now, info)
        return info

    async def _trend_allows(self, symbol: str, side: str) -> tuple[bool, str]:
        """آیا این جهت ورود با روند تایم‌فریم بالاتر هم‌سوست؟

        خروجی (اجازه، دلیل رد). وقتی فیلتر خاموش است همیشه اجازه می‌دهد.
        """
        info = await self._get_trend(symbol)
        if info is None:
            return True, ""
        direction = info.get("direction", "unknown")
        if trend_filter.side_matches(direction, side):
            return True, ""
        tf = info.get("timeframe", "")
        if direction == "unknown":
            return False, f"روند تایم‌فریم {tf} تعیین نشد"
        if direction == "neutral":
            return False, f"روند تایم‌فریم {tf} خنثی است (اندیکاتورها موافق نیستند)"
        arrow = "صعودی" if direction == "up" else "نزولی"
        return False, f"روند تایم‌فریم {tf} {arrow} است"

    # ---------- پردازش سیگنال یک نماد ----------
    async def _process_symbol(self, sym_cfg: dict):
        symbol = sym_cfg["symbol"]
        try:
            df = await self.driver.get_candles(symbol, sym_cfg.get("timeframe", "1h"), 500)
        except ExchangeError as e:
            self.log(f"{symbol}: {e}", "error")
            return
        if df is None or len(df) < 50:
            self.log(f"{symbol}: داده‌ی کندل کافی نیست ({0 if df is None else len(df)} کندل)", "warn")
            return

        try:
            sig = run_strategy(sym_cfg.get("strategy", "supertrend_ema_rsi"),
                               df, sym_cfg.get("strategy_params") or {})
        except KeyError as e:
            self.log(f"{symbol}: {e}", "error")
            return
        except Exception as e:
            self.log(f"{symbol}: خطا در اجرای استراتژی: {e}", "error")
            return

        sig["spark"] = [float(x) for x in df["close"].tail(30).tolist()]
        sig["timeframe"] = sym_cfg.get("timeframe", "1h")
        self.last_signals[symbol] = sig

        # جهت روند حتی وقتی سیگنالی نیست هم خوانده و نشان داده می‌شود، وگرنه
        # کاربر فقط لحظه‌ی رد شدن یک سیگنال می‌فهمد فیلتر چه فکری می‌کند.
        if self.cfg.get("trend_filter_enabled"):
            tinfo = await self._get_trend(symbol)
            if tinfo:
                sig["trend"] = {
                    "direction": tinfo.get("direction", "unknown"),
                    "timeframe": tinfo.get("timeframe", ""),
                    "stale": bool(tinfo.get("stale")),
                }

        if sig["signal"] in ("buy", "sell"):
            raw = sig["signal"]
            result = await self._handle_entry_signal(sym_cfg, raw, sig["close"], None, None, sig.get("atr"))
            if result == "trend_blocked":
                sig["trend_blocked"] = True
            if self.cfg.get("invert_signals"):
                # چیپ سیگنال در داشبورد باید همان جهتی را نشان دهد که واقعاً
                # اجرا شده، نه خروجی خام استراتژی. جهت خام هم نگه داشته می‌شود.
                sig["raw_signal"] = raw
                sig["inverted"] = True
                sig["signal"] = "sell" if raw == "buy" else "buy"

    # ---------- باز کردن پوزیشن ----------
    async def _handle_entry_signal(self, sym_cfg: dict, side: str, price: float | None,
                                   stop_loss: float | None, take_profit: float | None,
                                   atr: float | None):
        symbol = sym_cfg["symbol"]

        # حالت معکوس: هم سیگنال استراتژی داخلی و هم سیگنال وبهوک از همین‌جا رد
        # می‌شوند، پس وارونه‌کردن در یک نقطه انجام می‌شود.
        if self.cfg.get("invert_signals") and side in ("buy", "sell"):
            original_side = side
            side = "sell" if side == "buy" else "buy"
            # SL/TP ای که همراه سیگنال آمده برای جهت اصلی حساب شده؛ حول قیمت
            # ورود آینه می‌شود تا برای جهت معکوس معنا داشته باشد. اگر آینه‌کردن
            # عدد نامعتبر بدهد، خالی می‌شود تا موتور خودش از ATR حساب کند.
            if price and price > 0:
                if stop_loss:
                    mirrored = 2 * price - stop_loss
                    stop_loss = mirrored if mirrored > 0 else None
                if take_profit:
                    mirrored = 2 * price - take_profit
                    take_profit = mirrored if mirrored > 0 else None
            else:
                stop_loss = take_profit = None
            self.log(f"{symbol}: حالت معکوس فعال است — سیگنال {original_side} به {side} تبدیل شد.")

        # فیلتر روند تایم‌فریم بالاتر — بعد از حالت معکوس بررسی می‌شود، چون
        # چیزی که باید با روند هم‌سو باشد جهتِ واقعاً اجراشده است نه خروجی خام
        # استراتژی. فقط جلوی «باز کردن» را می‌گیرد؛ سیگنال close هیچ‌وقت به
        # این تابع نمی‌رسد، پس خروج از پوزیشن هرگز فیلتر نمی‌شود.
        allowed, reason = await self._trend_allows(symbol, side)
        if not allowed:
            side_fa = "خرید" if side == "buy" else "فروش"
            self.log(f"{symbol}: سیگنال {side_fa} خلاف روند نادیده گرفته شد — {reason}.", "warn")
            return "trend_blocked"

        wanted = "long" if side == "buy" else "short"

        # پوزیشن روی همین نماد داریم؟
        same_symbol = [p for p in self.positions if p["symbol"] == symbol]
        policy = str(self.cfg.get("reversal_policy", "none") or "none")
        for p in same_symbol:
            if p["side"] == wanted:
                return  # هم‌جهت باز است؛ کاری نکن

            # سیاست برخورد با سیگنال جهت مخالف. حالت پیش‌فرض none است چون
            # حالت قدیمی (profitable) نامتقارن بود: پوزیشن سودده را زود
            # می‌بست ولی ضررده را تا حد ضرر کامل رها می‌کرد، پس میانگین باخت
            # سیستماتیک بزرگ‌تر از میانگین برد می‌شد.
            if policy == "none":
                self.log(
                    f"{symbol}: سیگنال معکوس نادیده گرفته شد — پوزیشن {p['side']} "
                    "به حد ضرر/حد سود خودش سپرده می‌شود.",
                )
                return
            if policy == "profitable" and p.get("profit", 0) <= 0:
                self.log(
                    f"{symbol}: سیگنال معکوس نادیده گرفته شد — پوزیشن {p['side']} در ضرر است "
                    "و به حد ضرر خودش سپرده می‌شود.",
                )
                return
            try:
                await self.driver.close_position(p)
                if not isinstance(self.driver, PaperDriver):
                    await self.record_live_close(p, "reversal")
                self.log(f"{symbol}: پوزیشن {p['side']} (سودده) به‌خاطر سیگنال معکوس بسته شد؛ پوزیشن معکوس باز می‌شود.")
            except ExchangeError as e:
                self.log(f"{symbol}: بستن پوزیشن معکوس ناموفق: {e}", "error")
                return
        self.positions = [p for p in self.positions if p["symbol"] != symbol]

        if self._daily["blocked"]:
            self.log(f"{symbol}: سیگنال {side} به‌خاطر سقف ضرر روزانه نادیده گرفته شد.", "warn")
            return

        # ظرفیت پوزیشن باز
        max_open = int(self.cfg.get("max_open_positions", 2))
        if len(self.positions) >= max_open:
            if self.cfg.get("recycle_on_new_signal") and self.positions:
                victim = max(self.positions, key=lambda p: p.get("profit", 0))
                try:
                    await self.driver.close_position(victim)
                    if not isinstance(self.driver, PaperDriver):
                        await self.record_live_close(victim, "reversal")
                    self.log(f"recycle: پوزیشن سودده {victim['symbol']} بسته شد تا جا برای {symbol} باز شود.")
                    self.positions.remove(victim)
                except ExchangeError as e:
                    self.log(f"recycle ناموفق ({victim['symbol']}): {e}", "error")
                    return
            else:
                self.log(f"{symbol}: ظرفیت پوزیشن باز ({max_open}) پر است؛ سیگنال {side} رد شد.", "warn")
                return

        # قیمت مرجع
        if price is None:
            try:
                price = await self.driver.get_last_price(symbol)
            except ExchangeError as e:
                self.log(f"{symbol}: قیمت لحظه‌ای دریافت نشد: {e}", "error")
                return

        # SL/TP: از وبهوک، وگرنه ATR-based با فاصله‌ی یکسان (نسبت ریسک:پاداش ۱:۱)
        if (not stop_loss or not take_profit) and atr:
            atr_mult = float(self.cfg.get("sl_tp_atr_mult", DEFAULT_SL_TP_ATR_MULT) or DEFAULT_SL_TP_ATR_MULT)
            if not stop_loss:
                stop_loss = price - atr_mult * atr if side == "buy" else price + atr_mult * atr
            if not take_profit:
                take_profit = price + atr_mult * atr if side == "buy" else price - atr_mult * atr
        if not stop_loss or not take_profit:
            self.log(f"{symbol}: SL/TP مشخص نیست و ATR هم در دسترس نیست؛ ورود لغو شد.", "warn")
            return

        # اهرم — قبل از محاسبه‌ی حجم لازم است تا سقف حجم بر اساس مارجین آزاد درست دربیاید
        leverage = int(sym_cfg.get("leverage") or self.cfg.get("default_leverage", 5))

        # محاسبه‌ی حجم بر اساس ریسک (و سقف آن بر اساس مارجین آزاد واقعی)
        qty = await self._calc_qty(sym_cfg, price, stop_loss, leverage)
        if qty is None or qty <= 0:
            return

        # گرد کردن SL/TP به گام قیمتی مجاز نماد (PRICE_FILTER.tickSize) — بدون
        # این کار، صرافی می‌تواند مقدار غیرمجاز را رد کند (که به‌صورت خطای
        # tp_sl_set=False لاگ می‌شود) یا خودش آن را گرد کند، به‌شکلی که لزوماً
        # با قیمت ورودِ ثبت‌شده در لاگ ما یکی نباشد.
        try:
            price_step = float((await self.driver.get_symbol_info(symbol)).get("price_step", 0) or 0)
        except ExchangeError:
            price_step = 0
        if price_step > 0:
            stop_loss = round(round(stop_loss / price_step) * price_step, 10)
            take_profit = round(round(take_profit / price_step) * price_step, 10)

        # تنظیم اهرم روی صرافی (خطای آن غیرحیاتی است)
        try:
            await self.driver.set_leverage(symbol, leverage)
        except Exception as e:
            self.log(f"{symbol}: تنظیم اهرم ناموفق (ادامه می‌دهیم): {e}", "warn")

        try:
            result = await self.driver.place_order(side, symbol, qty, stop_loss=stop_loss, take_profit=take_profit)
        except ExchangeError as e:
            self.log(f"{symbol}: سفارش {side} ناموفق (حجم محاسبه‌شده: {qty!r}): {e}", "error")
            return

        self.log(
            f"✅ ورود {('Long' if side == 'buy' else 'Short')} {symbol} | حجم: {qty:g} @ ~{price:g} | "
            f"SL: {stop_loss:g} | TP: {take_profit:g} | اهرم: {leverage}x"
        )
        self._notify_owner(
            f"📈 ورود {('Long' if side == 'buy' else 'Short')} {symbol} | حجم: {qty:g} @ ~{price:g} | "
            f"SL: {stop_loss:g} | TP: {take_profit:g}",
            kind="trade", symbol=symbol,
        )
        if result.get("tp_sl_set") is False:
            self.log(result.get("tp_sl_error", "ست کردن TP/SL ناموفق بود"), "error")
        self.positions = await self.driver.get_open_positions()

        if not isinstance(self.driver, PaperDriver):
            # تازه‌ترین پوزیشن همین نماد را پیدا و SL/TP آن را برای تشخیص بعدیِ
            # «SL خورد یا TP» (وقتی در سمت صرافی بسته شود) ذخیره می‌کنیم.
            # زمان ورود هم همین‌جا ثبت می‌شود: پاسخ پوزیشن‌های صرافی هیچ فیلد
            # زمانی ندارد، پس اگر الان ننویسیمش دیگر جایی پیدا نمی‌شود.
            opened = next((p for p in self.positions if p["symbol"] == symbol), None)
            if opened is not None:
                open_time = _utc_now().isoformat(timespec="seconds")
                self._live_position_targets[opened["id"]] = {
                    "stop_loss": stop_loss, "take_profit": take_profit,
                    "open_time": open_time,
                }
                # روی دیسک هم می‌ماند تا با توقف/شروع حساب یا ری‌استارت سرویس
                # از بین نرود؛ کلید «نماد|جهت» است چون شناسه‌ی پوزیشن پایدار نیست.
                position_targets.set_targets(self.account_id, symbol, opened["side"],
                                             stop_loss, take_profit, open_time)

    async def _calc_qty(self, sym_cfg: dict, price: float, stop_loss: float, leverage: int = 1):
        """حجم = (اکوییتی × ٪ریسک) ÷ فاصله‌ی SL — سپس روی گام حجم صرافی گرد می‌شود
        و در نهایت به سقف مارجین آزاد واقعی (با احتساب اهرم) محدود می‌شود تا خطای
        «Balance insufficient» رخ ندهد، مخصوصاً وقتی پوزیشن‌های دیگری هم باز هستند."""
        symbol = sym_cfg["symbol"]
        equity = float((self.account_info or {}).get("equity", 0) or 0)
        if equity <= 0:
            self.log(f"{symbol}: اکوییتی نامعتبر است؛ ورود لغو شد.", "warn")
            return None
        sl_dist = abs(price - stop_loss)
        if sl_dist <= 0:
            self.log(f"{symbol}: فاصله‌ی SL صفر است؛ ورود لغو شد.", "warn")
            return None

        # محدودیت‌های حجم: تنظیمات کاربر، وگرنه exchangeInfo صرافی — زودتر گرفته
        # می‌شود چون contract_multiplier خود فرمول ریسک را هم تصحیح می‌کند.
        try:
            info = await self.driver.get_symbol_info(symbol)
        except ExchangeError:
            info = {}
        min_qty = sym_cfg.get("min_qty") if sym_cfg.get("min_qty") is not None else info.get("min_qty", 0)
        qty_step = sym_cfg.get("qty_step") if sym_cfg.get("qty_step") is not None else info.get("qty_step", 0)
        max_qty = sym_cfg.get("max_qty")
        # برخی نمادها (مثل SPX500-SWAP-USDT، DASH-SWAP-USDT، حتی BTC-SWAP-USDT)
        # contractMultiplier غیر از ۱ دارند (اینجا ۰.۰۰۱) — یعنی ارزش دلاری هر
        # واحد «quantity» برابر price×این‌ضریب است، نه خود price (با حجم معاملات
        # واقعی ۲۴ ساعته‌ی توبیت هم تأیید شد: qv/v ≈ price×contractMultiplier).
        contract_multiplier = float(info.get("contract_multiplier", 1.0) or 1.0)

        # ریسک دلاری واقعیِ نگه‌داشتن qty واحد در برابر حرکت sl_dist برابر است با
        # qty × sl_dist × contract_multiplier — نه qty × sl_dist. نادیده گرفتن
        # ضریب قرارداد در این فرمول (که قبل از این فیکس وجود داشت) باعث می‌شد
        # ریسک واقعی این نمادها هزار برابر کمتر از ٪ریسک تنظیم‌شده باشد و در
        # نتیجه حجم/ارزش دلاری سفارش آن‌قدر ناچیز شود که صرافی آن را با خطای
        # «quantity too small» رد کند، حتی وقتی qty عددی به‌ظاهر بزرگ بود.
        risk_amount = equity * float(self.cfg.get("risk_percent", 1.0)) / 100
        qty = risk_amount / (sl_dist * contract_multiplier)

        # سقف حجم بر اساس مارجین آزاد واقعی (نه کل اکوییتی) — با ۵٪ حاشیه‌ی
        # اطمینان برای کارمزد/لغزش قیمت، تا با وجود پوزیشن‌های باز دیگر هم به
        # خطای «Balance insufficient» صرافی نخوریم.
        free_margin = float((self.account_info or {}).get("free_margin", equity) or 0)
        unit_value = price * contract_multiplier
        if unit_value > 0 and leverage > 0:
            max_qty_by_margin = (free_margin * leverage * 0.95) / unit_value
            if max_qty_by_margin < qty:
                qty = max_qty_by_margin
                self.log(
                    f"{symbol}: حجم به‌خاطر محدودیت مارجین آزاد ({free_margin:g} USDT) کاهش یافت.",
                    "warn",
                )

            # سقف مستقل از فاصله‌ی SL: هیچ معامله‌ای بیش از max_margin_per_trade_pct
            # (قابل‌تنظیم در هر حساب) از کل اکوییتی را به مارجین قفل نکند — حتی اگر
            # حد ضرر (ATR-based) خیلی نزدیک به قیمت باشد و فرمول ریسک‌محور بخواهد
            # حجم بسیار بزرگی بسازد.
            margin_cap_pct = float(self.cfg.get("max_margin_per_trade_pct", DEFAULT_MAX_MARGIN_PER_TRADE_PCT)
                                   or DEFAULT_MAX_MARGIN_PER_TRADE_PCT)
            max_qty_by_cap = (equity * margin_cap_pct / 100 * leverage) / unit_value
            if max_qty_by_cap < qty:
                qty = max_qty_by_cap
                self.log(
                    f"{symbol}: حجم به‌خاطر سقف {margin_cap_pct:g}٪ مارجین هر معامله کاهش یافت.",
                    "warn",
                )

        if qty_step and qty_step > 0:
            # +epsilon قبل از floor تا خطای اعشاری شناور (مثلاً 0.0001 که در
            # باینری دقیقاً قابل نمایش نیست) یک گام کامل را اشتباهی حذف نکند.
            qty = math.floor(qty / qty_step + 1e-9) * qty_step
            qty = round(qty, 10)
        if max_qty:
            qty = min(qty, float(max_qty))
        if min_qty and qty < float(min_qty):
            self.log(
                f"{symbol}: حجم محاسبه‌شده ({qty!r}) کمتر از حداقل ({float(min_qty)!r}) است؛ ورود لغو شد.",
                "warn",
            )
            return None
        return qty

    # ---------- وبهوک ----------
    async def handle_signal(self, symbol: str, signal: str, price: float | None,
                            stop_loss: float | None, take_profit: float | None) -> str:
        """یک سیگنال وبهوک روی این حساب اعمال می‌شود. خروجی: توضیح نتیجه."""
        sym_cfg = next((s for s in self.cfg.get("symbols", [])
                        if s["symbol"] == symbol and s.get("enabled", True)), None)
        if sym_cfg is None:
            return "نماد در این حساب فعال نیست"

        if signal == "close":
            targets = [p for p in self.positions if p["symbol"] == symbol]
            if not targets:
                return "پوزیشن بازی روی این نماد نیست"
            for p in targets:
                try:
                    await self.driver.close_position(p)
                    if not isinstance(self.driver, PaperDriver):
                        await self.record_live_close(p, "manual")
                    self.log(f"{symbol}: پوزیشن با سیگنال close وبهوک بسته شد.")
                except ExchangeError as e:
                    self.log(f"{symbol}: بستن با وبهوک ناموفق: {e}", "error")
                    return f"خطا در بستن: {e}"
            self.positions = [p for p in self.positions if p["symbol"] != symbol]
            return f"{len(targets)} پوزیشن بسته شد"

        atr = None
        try:
            df = await self.driver.get_candles(symbol, sym_cfg.get("timeframe", "1h"), 100)
            if df is not None and len(df) > 20:
                from app.core.strategies import indicators as ind
                atr_val = ind.atr(df, 14).iat[-1]
                if atr_val == atr_val:  # NaN check
                    atr = float(atr_val)
        except Exception:
            pass

        result = await self._handle_entry_signal(sym_cfg, signal, price, stop_loss, take_profit, atr)
        if result == "trend_blocked":
            return "خلاف روند تایم‌فریم بالاتر بود — نادیده گرفته شد"
        return "سیگنال اعمال شد"

    # ---------- بستن دستی ----------
    async def close_position_manual(self, position_id: str | None, symbol: str | None) -> dict:
        if not self.running or self.driver is None:
            return {"ok": False, "detail": "ربات این حساب فعال نیست؛ ابتدا آن را شروع کنید."}
        self.positions = await self.driver.get_open_positions()
        target = None
        for p in self.positions:
            if position_id and str(p["id"]) == str(position_id):
                target = p
                break
            if symbol and p["symbol"] == symbol:
                target = p
                break
        if target is None:
            return {"ok": False, "detail": "پوزیشن پیدا نشد."}
        try:
            await self.driver.close_position(target)
        except ExchangeError as e:
            return {"ok": False, "detail": str(e)}
        if not isinstance(self.driver, PaperDriver):
            await self.record_live_close(target, "manual")
        self.log(f"پوزیشن {target['symbol']} به‌صورت دستی بسته شد.")
        self.positions = await self.driver.get_open_positions()
        return {"ok": True, "closed": target["symbol"]}

    NET_TRANSFERS_TTL = 600      # ثانیه

    async def _refresh_net_transfers(self):
        """خواندن دوره‌ای مجموع واریز/برداشت از دفتر صرافی (فقط حساب واقعی).

        در حالت کاغذی لازم نیست: آنجا هر تغییر موجودی از معامله‌ای می‌آید که
        خودمان ثبتش کرده‌ایم، پس محاسبه‌ی «موجودی منهای سود» دقیقاً درست است.
        روی حساب واقعی این‌طور نیست — کارمزد فاندینگ و اختلاف کارمزد تخمینی
        عدد را می‌لغزانند.
        """
        if self.cfg.get("trading_mode") != "live":
            return
        getter = getattr(self.driver, "get_net_transfers", None)
        if getter is None:
            return
        now = _utc_now().timestamp()
        if self._net_transfers is not None and (now - self._net_transfers_at) < self.NET_TRANSFERS_TTL:
            return
        try:
            value = await getter()
        except Exception:
            return                     # عدد قبلی نگه داشته می‌شود
        if value is not None:
            self._net_transfers = value
            self._net_transfers_at = now

    # ---------- حد ضرر دنبال‌کننده ----------
    # کمینه‌ی جابه‌جایی، به‌صورت کسری از فاصله‌ی تریلینگ. بدون این، در یک روند
    # آرام هر tick یک درخواست به صرافی می‌رفت بدون اینکه عملاً چیزی عوض شود.
    TRAIL_MIN_STEP_RATIO = 0.1

    def _known_stop_loss(self, p: dict) -> float | None:
        """حد ضرر فعلی این پوزیشن، از پاسخ صرافی یا حافظه‌ی خودمان."""
        sl = p.get("stop_loss")
        if sl:
            return float(sl)
        record = (self._live_position_targets.get(p.get("id"))
                  or position_targets.get_targets(self.account_id, p.get("symbol"), p.get("side")))
        sl = (record or {}).get("stop_loss")
        return float(sl) if sl else None

    async def _update_trailing_stops(self):
        """حد ضرر پوزیشن‌های سودده را پشت قیمت بازار می‌کشد.

        عمداً سمت خودمان پیاده شده و نه با تریلینگ داخلی صرافی: در آن حالت
        معلوم نیست حد ضرر ثابت باقی می‌ماند یا پاک می‌شود، و اگر پاک شود
        پوزیشن تا لحظه‌ی فعال‌شدن تریلینگ بی‌محافظ می‌ماند. این‌جا همان حد ضرر
        ثابت فقط جابه‌جا می‌شود، پس در هر لحظه یک حد ضرر واقعی روی صرافی هست
        و بدترین حالتِ خرابی این است که دیگر جلو نرود.
        """
        if not self.cfg.get("trailing_enabled"):
            return
        setter = getattr(self.driver, "update_stop_loss", None)
        if setter is None:
            return
        try:
            activation = float(self.cfg.get("trailing_activation_pct") or 0)
            distance = float(self.cfg.get("trailing_distance_pct") or 0)
        except (TypeError, ValueError):
            return
        if distance <= 0:
            return

        for p in list(self.positions):
            entry = _num_or_none(p.get("entry_price"))
            mark = _num_or_none(p.get("mark_price"))
            side = p.get("side")
            if not entry or not mark or side not in ("long", "short"):
                continue

            # چقدر قیمت به نفع پوزیشن حرکت کرده (درصدِ قیمت ورود)
            move_pct = ((mark - entry) / entry * 100) if side == "long" else ((entry - mark) / entry * 100)
            if move_pct < activation:
                continue

            current = self._known_stop_loss(p)
            if current is None:
                # حد ضرر فعلی را نمی‌دانیم؛ جابه‌جا کردنش می‌تواند حد ضرر
                # بهتری را که روی صرافی هست خراب کند. عمداً کاری نمی‌کنیم.
                continue

            candidate = mark * (1 - distance / 100) if side == "long" else mark * (1 + distance / 100)
            improvement = (candidate - current) if side == "long" else (current - candidate)
            if improvement <= 0:
                continue                      # هرگز عقب نمی‌رود
            if improvement < mark * (distance / 100) * self.TRAIL_MIN_STEP_RATIO:
                continue                      # جابه‌جایی ناچیز، درخواست هدر ندهیم

            take_profit = p.get("take_profit")
            if not take_profit:
                record = (self._live_position_targets.get(p.get("id"))
                          or position_targets.get_targets(self.account_id, p.get("symbol"), p.get("side")))
                take_profit = (record or {}).get("take_profit")
            try:
                await setter(p, candidate, take_profit)
            except Exception as e:
                self.log(f"{p.get('symbol')}: جابه‌جایی حد ضرر دنبال‌کننده ناموفق: {e}", "warn")
                continue

            p["stop_loss"] = candidate
            if p.get("id") in self._live_position_targets:
                self._live_position_targets[p["id"]]["stop_loss"] = candidate
            record = position_targets.get_targets(self.account_id, p.get("symbol"), p.get("side")) or {}
            position_targets.set_targets(self.account_id, p.get("symbol"), p.get("side"),
                                         candidate, take_profit, record.get("open_time"))
            self.log(
                f"{p.get('symbol')}: حد ضرر دنبال‌کننده جابه‌جا شد → {candidate:g} "
                f"(قیمت {mark:g}، سود {move_pct:.2f}٪)"
            )

    LEAD_ORDERS_TTL = 60         # ثانیه

    async def _refresh_lead_orders(self):
        """تعداد فالوور و سرمایه‌ی آن‌ها روی هر پوزیشن باز (فقط حساب کپی‌ترید).

        شکست این فراخوانی عمداً بی‌صدا است: این داده تزئینی است و نباید
        نبودنش جلوی به‌روزرسانی وضعیت حساب و مدیریت پوزیشن را بگیرد. مقدار
        قبلی هم پاک نمی‌شود تا یک خطای گذرا ستون داشبورد را خالی نکند.
        """
        if self.cfg.get("account_type") != "copy_trading":
            return
        if self.cfg.get("trading_mode") != "live":
            return
        getter = getattr(self.driver, "get_leader_orders_current", None)
        if getter is None:
            return
        now = _utc_now().timestamp()
        if self._lead_orders and (now - self._lead_orders_at) < self.LEAD_ORDERS_TTL:
            return
        try:
            self._lead_orders = await getter()
            self._lead_orders_at = now
        except Exception:
            return

    # ---------- وضعیت برای داشبورد ----------
    def status_dict(self) -> dict:
        equity = float(self.account_info.get("equity", 0) or 0) if self.account_info else None
        balance = float(self.account_info.get("balance", 0) or 0) if self.account_info else None
        account_stats = history.get_account_stats(
            self.account_id, self.cfg.get("trading_mode", "paper"), equity, balance,
            contributed=self._net_transfers,
        )
        # پوزیشن‌های paper از قبل stop_loss/take_profit دارند؛ درایورهای live
        # (توبیت/تبدیل) این مقادیر را در اندپوینت پوزیشن‌ها برنمی‌گردانند، پس
        # از همان _live_position_targets که برای تشخیص «SL خورد یا TP» نگه
        # می‌داریم، برای نمایش در داشبورد هم استفاده می‌شود.
        positions = []
        # کل رکوردهای این حساب یک‌بار از دیسک خوانده می‌شود، نه یک‌بار به‌ازای
        # هر پوزیشن — این تابع با هر بار پول‌کردن داشبورد صدا زده می‌شود.
        stored = position_targets.get_account(self.account_id) if self.positions else {}
        for p in self.positions:
            # زنجیره‌ی جست‌وجو: پاسخ صرافی (اگر داشته باشد) → حافظه‌ی همین اجرا
            # → حافظه‌ی ماندگار روی دیسک. مورد سوم همان چیزی است که بعد از
            # توقف/شروع حساب یا ری‌استارت سرویس نجات‌دهنده است.
            record = (self._live_position_targets.get(p.get("id"))
                      or stored.get(position_targets.make_key(p.get("symbol"), p.get("side"))))
            if record:
                patch = {}
                if not p.get("stop_loss") and not p.get("take_profit"):
                    patch["stop_loss"] = record.get("stop_loss")
                    patch["take_profit"] = record.get("take_profit")
                # درایور paper خودش open_time دارد؛ این فقط برای live است.
                if not p.get("open_time") and record.get("open_time"):
                    patch["open_time"] = record["open_time"]
                if patch:
                    p = {**p, **patch}
            # اطلاعات کپی‌ترید همین پوزیشن. زمان ورودِ صرافی بر رکورد خودمان
            # ترجیح دارد: مرجع اصلی خود صرافی است و این تنها جایی است که
            # زمان ورود را می‌دهد — حتی برای پوزیشنی که ربات بازش نکرده.
            lead = self._lead_orders.get(f"{p.get('symbol')}|{p.get('side')}")
            if lead:
                extra = {"followers": lead.get("followers"),
                         "follower_margin": lead.get("follower_margin")}
                if lead.get("open_time"):
                    extra["open_time"] = lead["open_time"]
                p = {**p, **extra}
            positions.append(p)

        # جدیدترین ورود بالای لیست. زمان‌ها همه ISO-8601 با فرمت یکسان‌اند
        # (UTC، دقت ثانیه)، پس مقایسه‌ی رشته‌ای همان مقایسه‌ی زمانی است.
        # پوزیشن بدون زمان ورود (باز شده روی خود صرافی، یا از قبلِ ثبت
        # زمان ورود) رشته‌ی خالی می‌گیرد و ته لیست می‌نشیند — نه بالای
        # لیست، چون احتمالاً قدیمی‌تر از همه است و ادعای «جدیدترین» درباره‌اش
        # بی‌پایه است.
        positions.sort(key=lambda p: str(p.get("open_time") or ""), reverse=True)
        return {
            "running": self.running,
            "status": self.status,
            # کلید پایدار وضعیت — رابط کاربری آن را ترجمه می‌کند و اگر کلیدی
            # نشناخت، به همان رشته‌ی status برمی‌گردد.
            "status_key": getattr(self, "status_key", None),
            "account_info": self.account_info,
            "account_stats": account_stats,
            "positions": positions,
            "logs": list(self.logs),
            "last_signals": self.last_signals,
            "notify_events": list(self.notify_events),
        }


class BotManager:
    """مدیریت چرخه‌ی حیات همه‌ی AccountRunnerها."""

    def __init__(self):
        self.runners: dict[str, AccountRunner] = {}
        self._token_watchdog_task: asyncio.Task | None = None
        self._token_warned: dict[str, str] = {}  # account_id -> token_id که قبلاً هشدار انقضا داده شده

    def start_background_tasks(self):
        """تسک‌های پس‌زمینه‌ی سطح‌برنامه (نه هر حساب) را یک‌بار در استارتاپ شروع می‌کند."""
        if self._token_watchdog_task is None:
            self._token_watchdog_task = asyncio.create_task(self._token_watchdog_loop())

    async def _token_watchdog_loop(self):
        while True:
            try:
                await self._check_live_token_expiry()
            except Exception:
                pass
            await asyncio.sleep(TOKEN_CHECK_INTERVAL_SECONDS)

    async def _check_live_token_expiry(self):
        """اگر توکن فعال‌سازی مالک یک حساب live (کاربر عادی) منقضی/باطل شده باشد،
        ربات آن حساب را خودکار متوقف می‌کند — نه فقط جلوی سوییچ جدید به live را
        می‌گیرد. اگر کمتر از ۲۴ ساعت به انقضا مانده، یک‌بار هشدار هم داده می‌شود."""
        for aid, runner in list(self.runners.items()):
            owner_id = runner.cfg.get("owner_id")
            if not owner_id:
                continue
            owner = users.get_user(owner_id)
            if owner is None or owner.get("role") == "admin":
                continue
            if runner.running and runner.cfg.get("trading_mode") == "live":
                if not tokens.has_active_token(owner_id):
                    msg = "⛔ توکن فعال‌سازی معاملات واقعی منقضی/باطل شده — ربات به‌صورت خودکار متوقف شد."
                    runner.log(msg, "error")
                    runner._notify_owner(msg)
                    await self.stop_account(aid)
                    continue
            active = tokens.get_active_token(owner_id)
            if active is None:
                continue
            expires_at = active.get("expires_at", "")
            try:
                remaining = datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
            except ValueError:
                continue
            if remaining.total_seconds() <= 86400 and self._token_warned.get(aid) != active["id"]:
                self._token_warned[aid] = active["id"]
                msg = "⏳ توکن فعال‌سازی معاملات واقعی کمتر از ۲۴ ساعت دیگر منقضی می‌شود."
                runner.log(msg, "warn")
                runner._notify_owner(msg)

    def sync_from_config(self):
        """runnerها را با فایل پیکربندی هم‌گام می‌کند (افزودن/به‌روزرسانی cfg).
        توقف حساب‌های حذف‌شده توسط endpointهای main.py انجام می‌شود."""
        for cfg in config_store.list_accounts():
            runner = self.runners.get(cfg["id"])
            if runner is None:
                self.runners[cfg["id"]] = AccountRunner(cfg)
            else:
                if not runner.running:
                    runner.cfg = cfg
                else:
                    # در حال اجرا: فقط نمادها/پارامترها به‌روز می‌شوند؛
                    # تغییر حساس (کلید/حالت) با توقف و شروع دوباره اعمال می‌شود.
                    sensitive = ("api_key", "api_secret", "exchange", "trading_mode", "paper_balance")
                    restart_needed = any(runner.cfg.get(k) != cfg.get(k) for k in sensitive)
                    runner.cfg = cfg
                    if restart_needed:
                        runner.log("تنظیمات حساس تغییر کرد؛ برای اعمال، ربات را متوقف و دوباره شروع کنید.", "warn")

    async def start_account(self, account_id: str):
        runner = self.runners.get(account_id)
        if runner is None:
            self.sync_from_config()
            runner = self.runners.get(account_id)
        if runner is None:
            raise ValueError("حساب پیدا نشد")
        if not runner.cfg.get("symbols"):
            runner.log("هیچ نمادی تعریف نشده؛ ربات شروع می‌شود ولی معامله‌ای انجام نمی‌دهد.", "warn")
        try:
            await runner.start()
        except Exception:
            runner.running = False
            runner.status = "خطا در شروع"
            if runner.driver is not None:
                try:
                    await runner.driver.close()
                except Exception:
                    pass
                runner.driver = None
            raise
        config_store.set_running_flag(account_id, True)

    async def stop_account(self, account_id: str):
        runner = self.runners.get(account_id)
        if runner is not None:
            await runner.stop()
        # توقف خودکار واچ‌داگ (انقضای توکن) هم همین‌جا رد می‌شود و فلگ را پاک
        # می‌کند — یعنی حساب واقعیِ متوقف‌شده خودش برنمی‌گردد و کاربر باید بعد
        # از تمدید توکن آگاهانه دوباره روشنش کند. برای پول واقعی همین درست است.
        config_store.set_running_flag(account_id, False)

    async def resume_previously_running(self):
        """ربات‌هایی که هنگام خاموش‌شدن سرویس در حال اجرا بودند را برمی‌گرداند.

        فقط همان‌ها — حسابی که کاربر خودش متوقف کرده بود متوقف می‌ماند.
        خطای یک حساب نباید جلوی بقیه را بگیرد، پس هر کدام جدا try می‌شود.
        """
        resumed, skipped = [], []
        for cfg in config_store.list_previously_running():
            aid = cfg["id"]
            if not cfg.get("enabled", True):
                continue
            # حساب واقعی بدون توکن فعال نباید خودکار برگردد؛ وگرنه واچ‌داگ چند
            # دقیقه بعد دوباره خاموشش می‌کند و فقط نویز تولید می‌شود.
            if cfg.get("trading_mode") == "live":
                owner_id = cfg.get("owner_id")
                owner = users.get_user(owner_id) if owner_id else None
                if owner is not None and owner.get("role") != "admin" \
                        and not tokens.has_active_token(owner_id):
                    config_store.set_running_flag(aid, False)
                    skipped.append(cfg.get("name") or aid)
                    continue
            try:
                await self.start_account(aid)
                resumed.append(cfg.get("name") or aid)
            except Exception as e:
                runner = self.runners.get(aid)
                if runner is not None:
                    runner.log(f"بازگردانی خودکار بعد از ری‌استارت ناموفق بود: {e}", "error")
        if resumed:
            print(f"[crypto-bot] ربات {len(resumed)} حساب بعد از ری‌استارت خودکار برگشت: {', '.join(resumed)}")
        if skipped:
            print(f"[crypto-bot] برنگشت (توکن فعال ندارد): {', '.join(skipped)}")
        return {"resumed": resumed, "skipped": skipped}

    async def start_all(self, owner_id: str | None = None):
        for cfg in config_store.list_accounts(owner_id):
            if not cfg.get("enabled", True):
                continue
            runner = self.runners.get(cfg["id"])
            if runner is not None and not runner.running:
                try:
                    await runner.start()
                    config_store.set_running_flag(cfg["id"], True)
                except Exception as e:
                    runner.log(f"شروع ناموفق: {e}", "error")

    async def stop_all(self, owner_id: str | None = None):
        for runner in self.runners.values():
            if owner_id is not None and runner.cfg.get("owner_id") != owner_id:
                continue
            if runner.running:
                await runner.stop()
                config_store.set_running_flag(runner.cfg["id"], False)

    def refresh_symbols(self, account_id: str):
        """بعد از تغییر نمادها از API، cfg رانِر را از فایل تازه می‌کند."""
        runner = self.runners.get(account_id)
        fresh = config_store.get_account(account_id)
        if runner is not None and fresh is not None:
            runner.cfg = fresh

    async def get_status(self, owner_id: str | None = None) -> dict:
        self.sync_from_config()
        items = self.runners.items()
        if owner_id is not None:
            items = [(aid, r) for aid, r in items if r.cfg.get("owner_id") == owner_id]
        return {"accounts": {aid: r.status_dict() for aid, r in items}}

    async def handle_webhook_signal(self, symbol_raw: str, signal: str, price: float | None,
                                    stop_loss: float | None, take_profit: float | None,
                                    account_id: str | None = None) -> dict:
        results = []
        for aid, runner in self.runners.items():
            if account_id and aid != account_id:
                continue
            if not runner.running:
                continue
            if not runner.cfg.get("accept_webhook", True):
                continue
            # هر صرافی فرمت نماد متفاوتی دارد (مثلاً BTC-SWAP-USDT در توبیت
            # در برابر BTCUSDT در تبدیل)، پس نرمال‌سازی باید بر اساس صرافی
            # همین حساب انجام شود، نه یک‌بار سراسری با فرض توبیت.
            symbol = normalize_symbol_for(runner.cfg.get("exchange", "toobit"), symbol_raw)
            has_symbol = any(s["symbol"] == symbol and s.get("enabled", True)
                             for s in runner.cfg.get("symbols", []))
            if not has_symbol:
                continue
            try:
                outcome = await runner.handle_signal(symbol, signal, price, stop_loss, take_profit)
                results.append({"account_id": aid, "account": runner.cfg.get("name"), "result": outcome})
            except Exception as e:
                runner.log(f"خطا در اعمال سیگنال وبهوک {signal} روی {symbol}: {e}", "error")
                results.append({"account_id": aid, "account": runner.cfg.get("name"), "error": str(e)})
        if not results:
            return {
                "ok": False,
                "detail": f"هیچ حساب فعالی با نماد {symbol_raw} و وبهوک روشن پیدا نشد"
                          + (f" (account_id={account_id})" if account_id else ""),
            }
        return {"ok": True, "symbol": symbol_raw, "signal": signal, "results": results}

    async def close_position_manual(self, account_id: str, payload: dict) -> dict:
        runner = self.runners.get(account_id)
        if runner is None:
            return {"ok": False, "detail": "حساب پیدا نشد."}
        return await runner.close_position_manual(payload.get("position_id"), payload.get("symbol"))


bot_manager = BotManager()
