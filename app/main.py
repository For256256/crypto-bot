import json
import secrets
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from pydantic import BaseModel
from typing import Optional

from app.config import settings
from app.core.engine import bot_manager, normalize_symbol_for
from app.core import config_store
from app.core import history
from app.core import tickets
from app.core import app_settings
from app.core import users
from app.core import auth
from app.core.exchanges.toobit import normalize_symbol

app = FastAPI(title="کریپتو بات — Toobit Futures")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_session_secret = settings.SESSION_SECRET_KEY or secrets.token_urlsafe(32)
if not settings.SESSION_SECRET_KEY:
    print("[crypto-bot] هشدار: SESSION_SECRET_KEY تنظیم نشده — یک کلید موقت تصادفی ساخته شد "
          "(با هر ری‌استارت سرویس، همه‌ی کاربران باید دوباره لاگین کنند). "
          "SESSION_SECRET_KEY را در .env تنظیم کنید.")
app.add_middleware(SessionMiddleware, secret_key=_session_secret, session_cookie="cbot_session",
                   same_site="lax", max_age=2592000)


@app.exception_handler(auth.NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)


@app.on_event("startup")
async def on_startup():
    users.ensure_admin_seed(settings.DASHBOARD_USER, settings.DASHBOARD_PASSWORD)
    bot_manager.sync_from_config()


class AccountIn(BaseModel):
    name: str
    exchange: str = "toobit"
    trading_mode: str = "paper"          # paper | live
    api_key: str = ""
    api_secret: str = ""
    paper_balance: float = 10000.0
    risk_percent: float = 1.0
    default_leverage: int = 5
    sl_tp_atr_mult: float = 3.0
    max_margin_per_trade_pct: float = 25.0
    max_open_positions: int = 2
    max_daily_loss_percent: float = 5.0
    poll_interval_seconds: int = 60
    recycle_on_new_signal: bool = False
    accept_webhook: bool = True
    enabled: bool = True


class SymbolIn(BaseModel):
    symbol: str
    timeframe: str = "1h"
    enabled: bool = True
    strategy: str = "supertrend_ema_rsi"
    strategy_params: dict = {}
    leverage: Optional[int] = None       # خالی = اهرم پیش‌فرض حساب
    min_qty: Optional[float] = None      # خالی = خودکار از exchangeInfo صرافی
    qty_step: Optional[float] = None     # خالی = خودکار از exchangeInfo صرافی
    max_qty: Optional[float] = None


# ---------- ورود/ثبت‌نام/خروج ----------
class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.get_current_user(request) is not None:
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_submit(request: Request, payload: LoginIn):
    user = users.verify_login(payload.username, payload.password)
    if user is None:
        raise HTTPException(401, "نام کاربری یا رمز عبور اشتباه است")
    request.session["user_id"] = user["id"]
    return {"ok": True}


@app.post("/logout")
async def logout_submit(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if auth.get_current_user(request) is not None:
        return RedirectResponse("/")
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register_submit(request: Request, payload: RegisterIn):
    try:
        user = users.create_user(payload.username, payload.password, role="user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["user_id"] = user["id"]
    return {"ok": True}


# ---------- صفحات داشبورد ----------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = auth.get_current_user(request)
    if user is None:
        return templates.TemplateResponse("landing.html", {"request": request})
    return templates.TemplateResponse("dashboard.html", {"request": request, "active": "dashboard", "user": user})


@app.get("/strategies", response_class=HTMLResponse)
async def strategies_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return templates.TemplateResponse("strategies.html", {"request": request, "active": "strategies", "user": user})


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return templates.TemplateResponse("reports.html", {"request": request, "active": "reports", "user": user})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return templates.TemplateResponse("logs.html", {"request": request, "active": "logs", "user": user})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return templates.TemplateResponse("settings.html", {"request": request, "active": "settings", "user": user})


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return templates.TemplateResponse("support.html", {"request": request, "active": "support", "user": user})


# ---------- بک‌تست استراتژی ----------
class BacktestIn(BaseModel):
    symbol: str
    timeframe: str = "1h"
    strategy: str = "supertrend_ema_rsi"
    strategy_params: dict = {}
    candles: int = 500


@app.post("/api/backtest")
async def run_backtest_api(payload: BacktestIn, _: dict = Depends(auth.require_user)):
    from app.core.backtest import run_backtest
    from app.core.exchanges.toobit import ToobitDriver
    from app.core.exchanges.base import ExchangeError
    from app.core.strategies.registry import STRATEGIES

    if payload.strategy not in STRATEGIES:
        raise HTTPException(400, "استراتژی ناشناخته است")
    driver = ToobitDriver(api_key="", api_secret="", base_url=settings.TOOBIT_BASE_URL)
    try:
        df = await driver.get_candles(normalize_symbol(payload.symbol), payload.timeframe,
                                      min(max(payload.candles, 100), 1000))
        await driver.close()
    except ExchangeError as e:
        await driver.close()
        raise HTTPException(502, f"دریافت کندل از Toobit ناموفق: {e}")
    try:
        return run_backtest(df, payload.strategy, payload.strategy_params)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- تیکت‌های پشتیبانی ----------
class TicketIn(BaseModel):
    subject: str
    body: str
    unit: str = "عمومی"


@app.get("/api/tickets")
async def get_tickets(_: dict = Depends(auth.require_user)):
    return tickets.list_tickets()


@app.post("/api/tickets")
async def create_ticket(payload: TicketIn, _: dict = Depends(auth.require_user)):
    try:
        return tickets.create_ticket(payload.subject, payload.body, payload.unit)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- تنظیمات کلی برنامه (اعلان‌ها) ----------
@app.get("/api/app-settings")
async def get_app_settings(_: dict = Depends(auth.require_user)):
    return app_settings.get_settings()


@app.put("/api/app-settings")
async def put_app_settings(payload: dict, _: dict = Depends(auth.require_user)):
    return app_settings.update_settings(payload)


# ---------- وضعیت کلی ----------
@app.get("/api/status")
async def api_status(_: dict = Depends(auth.require_user)):
    return await bot_manager.get_status()


@app.get("/api/health")
async def health():
    return {"status": "ok", "app": "crypto-bot", "port": settings.DASHBOARD_PORT}


@app.get("/api/strategies")
async def get_strategies(_: dict = Depends(auth.require_user)):
    from app.core.strategies.registry import list_strategies
    return list_strategies()


# کش لیست نمادهای فیوچرز، به تفکیک صرافی (یک ساعت)
_symbols_cache: dict = {}


@app.get("/api/futures-symbols")
async def futures_symbols(exchange: str = "toobit", force: bool = False, _: dict = Depends(auth.require_user)):
    """لیست همه‌ی نمادهای قابل معامله در فیوچرز صرافی انتخاب‌شده — برای لیست کشویی داشبورد.
    endpoint عمومی صرافی است و نیازی به کلید API ندارد."""
    import time as _time
    from app.core.exchanges.base import ExchangeError
    from app.core.exchanges.toobit import ToobitDriver
    from app.core.exchanges.tabdeal import TabdealDriver

    cache = _symbols_cache.setdefault(exchange, {"time": 0.0, "data": []})
    now = _time.time()
    if not force and cache["data"] and now - cache["time"] < 3600:
        return {"symbols": cache["data"], "cached": True}

    if exchange == "tabdeal":
        driver = TabdealDriver(api_key="", api_secret="", base_url=settings.TABDEAL_BASE_URL)
        label = "تبدیل"
    elif exchange == "toobit":
        driver = ToobitDriver(api_key="", api_secret="", base_url=settings.TOOBIT_BASE_URL)
        label = "Toobit"
    else:
        raise HTTPException(400, f"صرافی پشتیبانی‌نشده: {exchange}")

    try:
        symbols = await driver.list_symbols()
        await driver.close()
    except ExchangeError as e:
        await driver.close()
        # اگر صرافی در دسترس نبود، کش قبلی (حتی قدیمی) را برگردان تا داشبورد از کار نیفتد
        if cache["data"]:
            return {"symbols": cache["data"], "cached": True, "warning": str(e)}
        raise HTTPException(502, f"دریافت لیست نمادها از {label} ناموفق: {e}")
    except Exception as e:
        await driver.close()
        if cache["data"]:
            return {"symbols": cache["data"], "cached": True, "warning": str(e)}
        raise HTTPException(502, f"خطای غیرمنتظره در دریافت لیست نمادها: {e}")

    cache["data"] = symbols
    cache["time"] = now
    return {"symbols": symbols, "cached": False}


@app.get("/api/webhook-info")
async def webhook_info(request: Request, _: dict = Depends(auth.require_user)):
    """آدرس Webhook و نمونه‌ی پیام Alert در TradingView را برمی‌گرداند."""
    host = request.headers.get("host", f"YOUR-SERVER-IP:{settings.DASHBOARD_PORT}")
    url = f"http://{host}/webhook/tradingview"
    sample = {
        "token": settings.WEBHOOK_TOKEN or "<WEBHOOK_TOKEN از فایل .env>",
        "symbol": "{{ticker}}",
        "signal": "buy",            # buy | sell | close
        "price": "{{close}}",
        # اختیاری: اگر ندهید، از روی ATR خودکار محاسبه می‌شود
        # "sl": 60000, "tp": 70000,
        # اختیاری: فقط به یک حساب مشخص ارسال شود
        # "account_id": "xxxxxxxx",
    }
    return {"url": url, "sample": sample, "token_set": bool(settings.WEBHOOK_TOKEN)}


@app.post("/api/webhook-token/rotate")
async def rotate_webhook_token(request: Request, _: dict = Depends(auth.require_user)):
    """تولید توکن جدید وبهوک و ذخیره در .env (توکن قبلی بلافاصله باطل می‌شود)."""
    import os
    new_token = secrets.token_hex(24)
    settings.WEBHOOK_TOKEN = new_token
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith("WEBHOOK_TOKEN="):
                lines[i] = f"WEBHOOK_TOKEN={new_token}"
                replaced = True
                break
        if not replaced:
            lines.append(f"WEBHOOK_TOKEN={new_token}")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass  # در حافظه اعمال شده؛ در صورت خطای دیسک فقط تا ری‌استارت معتبر است
    host = request.headers.get("host", f"YOUR-SERVER-IP:{settings.DASHBOARD_PORT}")
    return {"token": new_token, "url": f"http://{host}/webhook/tradingview"}


# ---------- Webhook تریدینگ‌ویو ----------
@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    """
    نقطه‌ی دریافت سیگنال از TradingView (یا هر منبع خارجی دیگر).
    امنیت: با WEBHOOK_TOKEN در .env محافظت می‌شود، نه با Basic Auth
    (چون TradingView نمی‌تواند هدر Authorization بفرستد).
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "بدنه‌ی درخواست JSON معتبر نیست. پیام Alert را دقیقاً مطابق نمونه‌ی /api/webhook-info تنظیم کنید.")

    if not settings.WEBHOOK_TOKEN:
        raise HTTPException(503, "WEBHOOK_TOKEN در فایل .env تنظیم نشده است؛ وبهوک غیرفعال است.")
    if not secrets.compare_digest(str(payload.get("token", "")), settings.WEBHOOK_TOKEN):
        raise HTTPException(401, "توکن وبهوک اشتباه است.")

    symbol_raw = str(payload.get("symbol", "")).strip()
    signal = str(payload.get("signal", "")).strip().lower()
    if signal in ("long",):
        signal = "buy"
    if signal in ("short",):
        signal = "sell"
    if not symbol_raw or signal not in ("buy", "sell", "close"):
        raise HTTPException(400, "فیلدهای symbol و signal (buy/sell/close) الزامی هستند.")

    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    result = await bot_manager.handle_webhook_signal(
        symbol_raw=symbol_raw,
        signal=signal,
        price=_to_float(payload.get("price")),
        stop_loss=_to_float(payload.get("sl")),
        take_profit=_to_float(payload.get("tp")),
        account_id=payload.get("account_id"),
    )
    return result


# ---------- مدیریت حساب‌ها ----------
@app.get("/api/accounts")
async def get_accounts(_: dict = Depends(auth.require_user)):
    return config_store.list_accounts()


@app.post("/api/accounts")
async def create_account(payload: AccountIn, _: dict = Depends(auth.require_user)):
    account = config_store.add_account(payload.dict())
    bot_manager.sync_from_config()
    return account


@app.post("/api/accounts/{account_id}/duplicate")
async def duplicate_account(account_id: str, _: dict = Depends(auth.require_user)):
    """ساخت بات جدید با کپی کامل تنظیمات و نمادهای یک حساب."""
    try:
        account = config_store.duplicate_account(account_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.sync_from_config()
    return account


@app.put("/api/accounts/{account_id}")
async def edit_account(account_id: str, payload: AccountIn, _: dict = Depends(auth.require_user)):
    try:
        account = config_store.update_account(account_id, payload.dict())
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.sync_from_config()
    return account


@app.post("/api/accounts/{account_id}/trading-mode")
async def set_trading_mode(account_id: str, mode: str, _: dict = Depends(auth.require_user)):
    """سوییچ paper/live از داشبورد. برای اعمال، حساب باید متوقف و دوباره شروع شود."""
    if mode not in ("paper", "live"):
        raise HTTPException(400, "حالت باید paper یا live باشد")
    try:
        account = config_store.update_account(account_id, {"trading_mode": mode})
    except KeyError as e:
        raise HTTPException(404, str(e))
    await bot_manager.stop_account(account_id)
    bot_manager.sync_from_config()
    return account


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str, _: dict = Depends(auth.require_user)):
    await bot_manager.stop_account(account_id)
    config_store.delete_account(account_id)
    bot_manager.sync_from_config()
    return {"deleted": account_id}


@app.post("/api/accounts/{account_id}/test-connection")
async def test_connection(account_id: str, _: dict = Depends(auth.require_user)):
    accounts = {a["id"]: a for a in config_store.list_accounts()}
    cfg = accounts.get(account_id)
    if not cfg:
        raise HTTPException(404, "حساب پیدا نشد")

    from app.core.exchanges.factory import build_driver
    from app.core.exchanges.base import ExchangeError
    # برای تست همیشه درایور واقعی ساخته می‌شود (نه شبیه‌ساز paper) تا کلیدهای API واقعاً چک شوند
    driver = build_driver("live", cfg)
    try:
        await driver.connect()
        info = await driver.get_account_info()
        await driver.close()
        return {"ok": True, "message": f"اتصال موفق. موجودی فیوچرز: {info.get('balance', 0):.2f} {info.get('currency', 'USDT')}", "account": info}
    except ExchangeError as e:
        await driver.close()
        return {"ok": False, "message": str(e)}
    except Exception as e:
        await driver.close()
        return {"ok": False, "message": f"خطای غیرمنتظره: {e}"}


@app.post("/api/accounts/{account_id}/start")
async def start_account(account_id: str, _: dict = Depends(auth.require_user)):
    try:
        await bot_manager.start_account(account_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, f"شروع ربات ناموفق: {e}")
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/accounts/{account_id}/stop")
async def stop_account(account_id: str, _: dict = Depends(auth.require_user)):
    await bot_manager.stop_account(account_id)
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/start-all")
async def start_all(_: dict = Depends(auth.require_user)):
    await bot_manager.start_all()
    return await bot_manager.get_status()


@app.post("/api/stop-all")
async def stop_all(_: dict = Depends(auth.require_user)):
    await bot_manager.stop_all()
    return await bot_manager.get_status()


# ---------- بستن دستی پوزیشن ----------
@app.post("/api/accounts/{account_id}/close-position")
async def close_position(account_id: str, payload: dict, _: dict = Depends(auth.require_user)):
    result = await bot_manager.close_position_manual(account_id, payload)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "بستن پوزیشن ناموفق بود"))
    return result


# ---------- گزارش‌گیری و سود/زیان ----------
@app.get("/api/accounts/{account_id}/report")
async def account_report(account_id: str, days: int = 30, mode: str | None = None,
                         _: dict = Depends(auth.require_user)):
    account = config_store.get_account(account_id)
    if account is None:
        raise HTTPException(404, "حساب پیدا نشد")
    if mode not in (None, "paper", "live"):
        raise HTTPException(400, "mode باید paper یا live باشد")
    report = history.get_report(account_id, days=days, mode=mode)
    # آمار «تعداد معاملات/نرخ برد/کل سود» فقط معاملات بسته‌شده را می‌شمارد؛
    # وقتی هنوز هیچ معامله‌ای بسته نشده (فقط پوزیشن باز دارد) این اعداد صفرند
    # ولی منحنی اکوییتی (که سود/زیان لحظه‌ای پوزیشن باز را هم دارد) در همان
    # حال نوسان نشان می‌دهد — که گیج‌کننده به‌نظر می‌رسید. برای رفع این تناقض،
    # سود/زیان باز (unrealized) پوزیشن‌های فعلی هم به گزارش اضافه می‌شود.
    runner = bot_manager.runners.get(account_id)
    unrealized = 0.0
    open_positions = 0
    live_account_info = None
    account_stats = None
    if runner is not None and (mode is None or mode == account.get("trading_mode", "paper")):
        unrealized = sum(float(p.get("profit", 0) or 0) for p in (runner.positions or []))
        open_positions = len(runner.positions or [])
        live_account_info = runner.account_info
        equity = float(live_account_info.get("equity", 0) or 0) if live_account_info else None
        balance = float(live_account_info.get("balance", 0) or 0) if live_account_info else None
        account_stats = history.get_account_stats(account_id, account.get("trading_mode", "paper"),
                                                   equity, balance)
    report["summary"]["unrealized_pnl"] = unrealized
    report["summary"]["open_positions"] = open_positions
    report["summary"]["total_pnl_with_open"] = report["summary"]["total_pnl"] + unrealized
    report["account_info"] = live_account_info
    report["account_stats"] = account_stats
    return report


@app.post("/api/accounts/{account_id}/report/reset")
async def reset_account_report(account_id: str, _: dict = Depends(auth.require_user)):
    """پاکسازی و ریست کامل گزارش‌های یک حساب — از این لحظه مثل حساب خام ثبت می‌شود."""
    if config_store.get_account(account_id) is None:
        raise HTTPException(404, "حساب پیدا نشد")
    return history.reset_account(account_id)


# ---------- مدیریت نمادها در هر حساب ----------
@app.post("/api/accounts/{account_id}/symbols/bulk")
async def bulk_update_symbols(account_id: str, payload: dict, _: dict = Depends(auth.require_user)):
    """اعمال دسته‌جمعی تنظیمات (تایم‌فریم، استراتژی، اهرم، حجم، وضعیت) روی همه نمادهای حساب."""
    try:
        count = config_store.bulk_update_symbols(account_id, payload or {})
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    bot_manager.sync_from_config()
    return {"updated": count}


@app.post("/api/accounts/{account_id}/symbols")
async def add_symbol(account_id: str, payload: SymbolIn, _: dict = Depends(auth.require_user)):
    account = config_store.get_account(account_id)
    if account is None:
        raise HTTPException(404, "حساب پیدا نشد")
    data = payload.dict()
    data["symbol"] = normalize_symbol_for(account.get("exchange", "toobit"), data["symbol"])
    try:
        symbol_cfg = config_store.add_symbol(account_id, data)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return symbol_cfg


@app.put("/api/accounts/{account_id}/symbols/{symbol}")
async def edit_symbol(account_id: str, symbol: str, payload: SymbolIn, _: dict = Depends(auth.require_user)):
    account = config_store.get_account(account_id)
    if account is None:
        raise HTTPException(404, "حساب پیدا نشد")
    data = payload.dict()
    data["symbol"] = normalize_symbol_for(account.get("exchange", "toobit"), data["symbol"])
    updated = config_store.update_symbol(account_id, symbol, data)
    if updated is None:
        raise HTTPException(404, "حساب یا نماد پیدا نشد")
    bot_manager.refresh_symbols(account_id)
    return updated


@app.delete("/api/accounts/{account_id}/symbols/{symbol}")
async def delete_symbol(account_id: str, symbol: str, _: dict = Depends(auth.require_user)):
    try:
        config_store.remove_symbol(account_id, symbol)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return {"deleted": symbol}


@app.post("/api/accounts/{account_id}/symbols/{symbol}/toggle")
async def toggle_symbol(account_id: str, symbol: str, enabled: bool, _: dict = Depends(auth.require_user)):
    try:
        config_store.toggle_symbol(account_id, symbol, enabled)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return {"symbol": symbol, "enabled": enabled}
