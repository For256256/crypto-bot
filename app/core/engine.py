"""
موتور اجرای ربات — برای هر حساب یک AccountRunner با حلقه‌ی asyncio مستقل.

مسئولیت‌ها:
- خواندن کندل‌ها و اجرای استراتژی هر نماد در بازه‌ی poll_interval_seconds
- باز/بستن پوزیشن (paper یا live) با مدیریت ریسک: حجم بر اساس ٪ریسک و فاصله‌ی SL
- ساخت خودکار SL/TP از ATR اگر وبهوک مقدار ندهد (حد ضرر ۱.۵×ATR، حد سود ۳×ATR)
- سقف ضرر روزانه (UTC): با عبور از آن، ورودی جدید تا فردا ممنوع می‌شود
- سیاست recycle: با پر بودن ظرفیت، سودده‌ترین پوزیشن بسته می‌شود تا جای سیگنال جدید باز شود
- دریافت سیگنال وبهوک TradingView و توزیع آن بین حساب‌های فعال
- ثبت تاریخچه‌ی معاملات و نقاط اکوییتی (هر ۵ دقیقه) برای گزارش داشبورد
"""
import asyncio
import math
from collections import deque
from datetime import datetime, timezone

from app.core import config_store, history
from app.core.exchanges.base import ExchangeError
from app.core.exchanges.factory import build_driver
from app.core.exchanges.paper import PaperDriver
from app.core.strategies.registry import run_strategy, STRATEGIES

EQUITY_SNAPSHOT_SECONDS = 300
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0


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
        self.status = "متوقف"
        self.logs: deque = deque(maxlen=100)
        self.last_signals: dict = {}
        self.account_info: dict | None = None
        self.positions: list = []
        self._last_equity_snapshot = 0.0
        self._daily = {"date": _today_utc(), "start_equity": None, "blocked": False}
        # شناسایی پوزیشن‌های live که خارج از ربات (دستی/SL صرافی) بسته شده‌اند
        self._known_live_positions: dict = {}

    # ---------- لاگ ----------
    def log(self, message: str, level: str = "info"):
        self.logs.append({
            "time": _utc_now().isoformat(timespec="seconds"),
            "level": level,
            "message": message,
        })

    # ---------- چرخه‌ی حیات ----------
    async def start(self):
        if self.running:
            return
        self.driver = build_driver(self.cfg.get("trading_mode", "paper"), self.cfg)
        await self.driver.connect()
        self.running = True
        self.status = "فعال"
        mode_fa = "کاغذی (paper)" if self.cfg.get("trading_mode") == "paper" else "⚠️ واقعی (LIVE)"
        self.log(f"ربات در حالت {mode_fa} شروع شد.")
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        self.status = "متوقف"
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
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except ExchangeError as e:
                self.status = "خطای صرافی"
                self.log(f"خطای صرافی: {e}", "error")
            except Exception as e:
                self.status = "خطا"
                self.log(f"خطای غیرمنتظره: {e}", "error")
            await asyncio.sleep(interval)

    async def _tick(self):
        loop = asyncio.get_event_loop()

        # ۱) وضعیت حساب و پوزیشن‌ها
        self.account_info = await self.driver.get_account_info()
        self.positions = await self.driver.get_open_positions()
        self.status = "فعال"

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

    # ---------- ثبت معاملات ----------
    async def _collect_closed_trades(self):
        mode = self.cfg.get("trading_mode", "paper")
        if isinstance(self.driver, PaperDriver):
            for trade in self.driver.drain_closed_trades():
                history.record_trade(self.account_id, mode, trade)
                self.log(
                    f"معامله‌ی {trade['symbol']} بسته شد ({trade.get('closed_by')}) — "
                    f"سود/زیان: {trade.get('realized', 0):+.2f} USDT",
                    "info" if trade.get("realized", 0) >= 0 else "warn",
                )
        else:
            # live: پوزیشنی که قبلاً می‌دیدیم و الان نیست یعنی در صرافی بسته شده
            current_ids = {p["id"]: p for p in self.positions}
            for pid, prev in list(self._known_live_positions.items()):
                if pid not in current_ids:
                    history.record_trade(self.account_id, mode, {
                        **prev,
                        "close_price": prev.get("mark_price"),
                        "realized": prev.get("profit"),
                        "closed_by": "exchange",
                        "estimated": True,
                        "close_time": _utc_now().isoformat(timespec="seconds"),
                    })
                    self.log(
                        f"پوزیشن {prev['symbol']} در سمت صرافی بسته شد "
                        f"(سود/زیان تقریبی: {prev.get('profit', 0):+.2f} USDT)"
                    )
            self._known_live_positions = current_ids

    def record_live_close(self, position: dict, closed_by: str):
        """ثبت معامله‌ی live که خودِ ربات بسته است (سود/زیان آخرین مقدار شناخته‌شده)."""
        history.record_trade(self.account_id, self.cfg.get("trading_mode", "paper"), {
            **position,
            "close_price": position.get("mark_price"),
            "realized": position.get("profit"),
            "closed_by": closed_by,
            "estimated": True,
            "close_time": _utc_now().isoformat(timespec="seconds"),
        })
        self._known_live_positions.pop(position.get("id"), None)

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
        elif self._daily["blocked"]:
            self.status = "متوقف (سقف ضرر روزانه)"

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
        if sig["signal"] in ("buy", "sell"):
            await self._handle_entry_signal(sym_cfg, sig["signal"], sig["close"], None, None, sig.get("atr"))

    # ---------- باز کردن پوزیشن ----------
    async def _handle_entry_signal(self, sym_cfg: dict, side: str, price: float | None,
                                   stop_loss: float | None, take_profit: float | None,
                                   atr: float | None):
        symbol = sym_cfg["symbol"]
        wanted = "long" if side == "buy" else "short"

        # پوزیشن روی همین نماد داریم؟
        same_symbol = [p for p in self.positions if p["symbol"] == symbol]
        for p in same_symbol:
            if p["side"] == wanted:
                return  # هم‌جهت باز است؛ کاری نکن
            # تغییر جهت: پوزیشن مخالف بسته شود
            try:
                await self.driver.close_position(p)
                if not isinstance(self.driver, PaperDriver):
                    self.record_live_close(p, "reversal")
                self.log(f"{symbol}: پوزیشن {p['side']} به‌خاطر سیگنال معکوس بسته شد.")
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
                        self.record_live_close(victim, "reversal")
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

        # SL/TP: از وبهوک، وگرنه ATR-based
        if (not stop_loss or not take_profit) and atr:
            if not stop_loss:
                stop_loss = price - SL_ATR_MULT * atr if side == "buy" else price + SL_ATR_MULT * atr
            if not take_profit:
                take_profit = price + TP_ATR_MULT * atr if side == "buy" else price - TP_ATR_MULT * atr
        if not stop_loss or not take_profit:
            self.log(f"{symbol}: SL/TP مشخص نیست و ATR هم در دسترس نیست؛ ورود لغو شد.", "warn")
            return

        # محاسبه‌ی حجم بر اساس ریسک
        qty = await self._calc_qty(sym_cfg, price, stop_loss)
        if qty is None or qty <= 0:
            return

        # اهرم (خطای آن غیرحیاتی است)
        leverage = int(sym_cfg.get("leverage") or self.cfg.get("default_leverage", 5))
        try:
            await self.driver.set_leverage(symbol, leverage)
        except Exception as e:
            self.log(f"{symbol}: تنظیم اهرم ناموفق (ادامه می‌دهیم): {e}", "warn")

        try:
            result = await self.driver.place_order(side, symbol, qty, stop_loss=stop_loss, take_profit=take_profit)
        except ExchangeError as e:
            self.log(f"{symbol}: سفارش {side} ناموفق: {e}", "error")
            return

        self.log(
            f"✅ ورود {('Long' if side == 'buy' else 'Short')} {symbol} | حجم: {qty:g} @ ~{price:g} | "
            f"SL: {stop_loss:g} | TP: {take_profit:g} | اهرم: {leverage}x"
        )
        if result.get("tp_sl_set") is False:
            self.log(result.get("tp_sl_error", "ست کردن TP/SL ناموفق بود"), "error")
        self.positions = await self.driver.get_open_positions()

    async def _calc_qty(self, sym_cfg: dict, price: float, stop_loss: float):
        """حجم = (اکوییتی × ٪ریسک) ÷ فاصله‌ی SL — سپس روی گام حجم صرافی گرد می‌شود."""
        symbol = sym_cfg["symbol"]
        equity = float((self.account_info or {}).get("equity", 0) or 0)
        if equity <= 0:
            self.log(f"{symbol}: اکوییتی نامعتبر است؛ ورود لغو شد.", "warn")
            return None
        sl_dist = abs(price - stop_loss)
        if sl_dist <= 0:
            self.log(f"{symbol}: فاصله‌ی SL صفر است؛ ورود لغو شد.", "warn")
            return None

        risk_amount = equity * float(self.cfg.get("risk_percent", 1.0)) / 100
        qty = risk_amount / sl_dist

        # محدودیت‌های حجم: تنظیمات کاربر، وگرنه exchangeInfo صرافی
        try:
            info = await self.driver.get_symbol_info(symbol)
        except ExchangeError:
            info = {}
        min_qty = sym_cfg.get("min_qty") if sym_cfg.get("min_qty") is not None else info.get("min_qty", 0)
        qty_step = sym_cfg.get("qty_step") if sym_cfg.get("qty_step") is not None else info.get("qty_step", 0)
        max_qty = sym_cfg.get("max_qty")

        if qty_step and qty_step > 0:
            qty = math.floor(qty / qty_step) * qty_step
        if max_qty:
            qty = min(qty, float(max_qty))
        if min_qty and qty < float(min_qty):
            self.log(
                f"{symbol}: حجم محاسبه‌شده ({qty:g}) کمتر از حداقل ({float(min_qty):g}) است؛ ورود لغو شد.",
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
                        self.record_live_close(p, "manual")
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

        await self._handle_entry_signal(sym_cfg, signal, price, stop_loss, take_profit, atr)
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
            self.record_live_close(target, "manual")
        self.log(f"پوزیشن {target['symbol']} به‌صورت دستی بسته شد.")
        self.positions = await self.driver.get_open_positions()
        return {"ok": True, "closed": target["symbol"]}

    # ---------- وضعیت برای داشبورد ----------
    def status_dict(self) -> dict:
        equity = float(self.account_info.get("equity", 0) or 0) if self.account_info else None
        balance = float(self.account_info.get("balance", 0) or 0) if self.account_info else None
        account_stats = history.get_account_stats(
            self.account_id, self.cfg.get("trading_mode", "paper"), equity, balance,
        )
        return {
            "running": self.running,
            "status": self.status,
            "account_info": self.account_info,
            "account_stats": account_stats,
            "positions": self.positions,
            "logs": list(self.logs),
            "last_signals": self.last_signals,
        }


class BotManager:
    """مدیریت چرخه‌ی حیات همه‌ی AccountRunnerها."""

    def __init__(self):
        self.runners: dict[str, AccountRunner] = {}

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

    async def stop_account(self, account_id: str):
        runner = self.runners.get(account_id)
        if runner is not None:
            await runner.stop()

    async def start_all(self):
        for cfg in config_store.list_accounts():
            if not cfg.get("enabled", True):
                continue
            runner = self.runners.get(cfg["id"])
            if runner is not None and not runner.running:
                try:
                    await runner.start()
                except Exception as e:
                    runner.log(f"شروع ناموفق: {e}", "error")

    async def stop_all(self):
        for runner in self.runners.values():
            if runner.running:
                await runner.stop()

    def refresh_symbols(self, account_id: str):
        """بعد از تغییر نمادها از API، cfg رانِر را از فایل تازه می‌کند."""
        runner = self.runners.get(account_id)
        fresh = config_store.get_account(account_id)
        if runner is not None and fresh is not None:
            runner.cfg = fresh

    async def get_status(self) -> dict:
        self.sync_from_config()
        return {"accounts": {aid: r.status_dict() for aid, r in self.runners.items()}}

    async def handle_webhook_signal(self, symbol: str, signal: str, price: float | None,
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
                "detail": f"هیچ حساب فعالی با نماد {symbol} و وبهوک روشن پیدا نشد"
                          + (f" (account_id={account_id})" if account_id else ""),
            }
        return {"ok": True, "symbol": symbol, "signal": signal, "results": results}

    async def close_position_manual(self, account_id: str, payload: dict) -> dict:
        runner = self.runners.get(account_id)
        if runner is None:
            return {"ok": False, "detail": "حساب پیدا نشد."}
        return await runner.close_position_manual(payload.get("position_id"), payload.get("symbol"))


bot_manager = BotManager()
