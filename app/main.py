import asyncio
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
from app.core import tokens
from app.core import presets
from app.core import telegram
from app.core.exchanges.toobit import normalize_symbol

app = FastAPI(title="CryptoPulse — Toobit / Tabdeal Futures")
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
    admin = users.ensure_admin_seed(settings.DASHBOARD_USER, settings.DASHBOARD_PASSWORD)
    if admin is None:
        admin = next((u for u in users.list_users() if u.get("role") == "admin"), None)
    if admin is not None:
        config_store.migrate_owner_less_accounts(admin["id"])
    bot_manager.sync_from_config()
    bot_manager.start_background_tasks()
    asyncio.create_task(telegram.poll_updates_loop())


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
    asyncio.create_task(telegram.notify_admin(f"👤 کاربر جدید ثبت‌نام کرد: {user['username']}"))
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: dict = Depends(auth.require_admin_page)):
    return templates.TemplateResponse("admin.html", {"request": request, "active": "admin", "user": user})


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


class ReplyIn(BaseModel):
    message: str


@app.get("/api/tickets")
async def get_tickets(user: dict = Depends(auth.require_user)):
    return tickets.list_tickets(None if user["role"] == "admin" else user["id"])


@app.post("/api/tickets")
async def create_ticket(payload: TicketIn, user: dict = Depends(auth.require_user)):
    try:
        ticket = tickets.create_ticket(payload.subject, payload.body, payload.unit, user["id"], user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(telegram.notify_admin(
        f"🎫 تیکت جدید ({ticket['unit']}) از {user['username']}: {ticket['subject']}"
    ))
    return ticket


@app.post("/api/tickets/{ticket_id}/reply")
async def reply_ticket_as_user(ticket_id: str, payload: ReplyIn, user: dict = Depends(auth.require_user)):
    """ادامه دادن گفتگوی یک تیکت — هم صاحب تیکت و هم ادمین می‌توانند پاسخ اضافه کنند."""
    ticket = tickets.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "تیکت پیدا نشد")
    is_admin = user["role"] == "admin"
    if not is_admin and ticket.get("user_id") != user["id"]:
        raise HTTPException(403, "این تیکت متعلق به شما نیست")
    try:
        return tickets.add_reply(ticket_id, payload.message, user["id"], user["username"],
                                 "admin" if is_admin else "user")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/tickets/{ticket_id}/close")
async def close_ticket_api(ticket_id: str, _: dict = Depends(auth.require_admin)):
    ticket = tickets.close_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "تیکت پیدا نشد")
    return ticket


# ---------- توکن فعال‌سازی معاملات واقعی ----------
class TokenIssueIn(BaseModel):
    user_id: str
    duration_days: int
    note: str = ""


@app.get("/api/tokens/me")
async def my_token_status(user: dict = Depends(auth.require_user)):
    if user["role"] == "admin":
        return {"is_admin": True, "active": True, "token": None}
    token = tokens.get_active_token(user["id"])
    return {"is_admin": False, "active": token is not None, "token": token}


@app.get("/api/admin/tokens")
async def admin_list_tokens(_: dict = Depends(auth.require_admin)):
    return tokens.list_tokens()


@app.post("/api/admin/tokens")
async def admin_issue_token(payload: TokenIssueIn, user: dict = Depends(auth.require_admin)):
    if users.get_user(payload.user_id) is None:
        raise HTTPException(404, "کاربر پیدا نشد")
    try:
        token = tokens.issue_token(payload.user_id, payload.duration_days, payload.note, user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(telegram.notify_user(
        payload.user_id, f"✅ توکن معاملات واقعی شما فعال شد — {payload.duration_days} روز اعتبار دارد."
    ))
    return token


@app.post("/api/admin/tokens/{token_id}/revoke")
async def admin_revoke_token(token_id: str, _: dict = Depends(auth.require_admin)):
    token = tokens.revoke_token(token_id)
    if token is None:
        raise HTTPException(404, "توکن پیدا نشد")
    return token


# ---------- مدیریت کاربران (پنل ادمین) ----------
class ResetPasswordIn(BaseModel):
    new_password: str = ""


@app.get("/api/admin/users")
async def admin_list_users(_: dict = Depends(auth.require_admin)):
    all_accounts = config_store.list_accounts()
    counts: dict[str, int] = {}
    for a in all_accounts:
        oid = a.get("owner_id")
        if oid:
            counts[oid] = counts.get(oid, 0) + 1
    out = []
    for u in users.list_users():
        row = users.public_view(u)
        row["accounts_count"] = counts.get(u["id"], 0)
        row["has_active_token"] = tokens.has_active_token(u["id"])
        out.append(row)
    return out


@app.post("/api/admin/users/{user_id}/enable")
async def admin_set_user_enabled(user_id: str, enabled: bool, _: dict = Depends(auth.require_admin)):
    user = users.set_enabled(user_id, enabled)
    if user is None:
        raise HTTPException(404, "کاربر پیدا نشد")
    return users.public_view(user)


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, payload: ResetPasswordIn, _: dict = Depends(auth.require_admin)):
    """چون فعلاً ایمیل/فراموشی‌رمز نداریم، این تنها راه بازیابی رمز کاربر است."""
    new_password = payload.new_password.strip() or secrets.token_urlsafe(9)
    if len(new_password) < 6:
        raise HTTPException(400, "رمز عبور باید حداقل ۶ کاراکتر باشد.")
    user = users.set_password(user_id, new_password)
    if user is None:
        raise HTTPException(404, "کاربر پیدا نشد")
    return {"user_id": user_id, "new_password": new_password}


# ---------- پیش‌فرض‌های پیشنهادی استراتژی (پنل ادمین) ----------
class PresetIn(BaseModel):
    name: str
    description: str = ""
    strategy: str = "supertrend_ema_rsi"
    strategy_params: dict = {}
    timeframe: str = "1h"
    leverage: Optional[int] = None
    risk_percent: Optional[float] = None
    sl_tp_atr_mult: Optional[float] = None


@app.get("/api/presets")
async def get_presets(_: dict = Depends(auth.require_user)):
    return presets.list_presets()


@app.post("/api/admin/presets")
async def create_preset_api(payload: PresetIn, user: dict = Depends(auth.require_admin)):
    try:
        return presets.create_preset(payload.dict(), user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/admin/presets/{preset_id}")
async def update_preset_api(preset_id: str, payload: PresetIn, _: dict = Depends(auth.require_admin)):
    preset = presets.update_preset(preset_id, payload.dict())
    if preset is None:
        raise HTTPException(404, "پیش‌فرض پیدا نشد")
    return preset


@app.delete("/api/admin/presets/{preset_id}")
async def delete_preset_api(preset_id: str, _: dict = Depends(auth.require_admin)):
    if not presets.delete_preset(preset_id):
        raise HTTPException(404, "پیش‌فرض پیدا نشد")
    return {"deleted": preset_id}


# ---------- تنظیمات کلی برنامه (سراسری — فقط ادمین) ----------
@app.get("/api/app-settings")
async def get_app_settings(_: dict = Depends(auth.require_admin)):
    return app_settings.get_settings()


@app.put("/api/app-settings")
async def put_app_settings(payload: dict, _: dict = Depends(auth.require_admin)):
    return app_settings.update_settings(payload)


# ---------- ربات تلگرام ----------
class TelegramSettingsIn(BaseModel):
    bot_token: str
    admin_chat_id: str = ""


@app.get("/api/admin/telegram-settings")
async def get_telegram_settings(_: dict = Depends(auth.require_admin)):
    data = app_settings.get_settings().get("telegram") or {}
    return {"bot_token_set": bool(data.get("bot_token")), "bot_username": data.get("bot_username", ""),
            "admin_chat_id": data.get("admin_chat_id", "")}


@app.put("/api/admin/telegram-settings")
async def put_telegram_settings(payload: TelegramSettingsIn, _: dict = Depends(auth.require_admin)):
    bot_token = payload.bot_token.strip()
    me = await telegram.get_me(bot_token)
    if me is None:
        raise HTTPException(400, "توکن ربات نامعتبر است — از BotFather یک توکن معتبر بگیرید.")
    app_settings.update_settings({"telegram": {
        "bot_token": bot_token, "bot_username": me.get("username", ""),
        "admin_chat_id": payload.admin_chat_id.strip(),
    }})
    return {"ok": True, "bot_username": me.get("username", "")}


@app.get("/api/settings/telegram/link-code")
async def telegram_link_code(user: dict = Depends(auth.require_user)):
    bot_username = (app_settings.get_settings().get("telegram") or {}).get("bot_username", "")
    if not bot_username:
        raise HTTPException(503, "ربات تلگرام هنوز توسط ادمین تنظیم نشده است.")
    code = users.set_link_code(user["id"])
    return {"code": code, "bot_username": bot_username, "ttl_minutes": users.LINK_CODE_TTL_MINUTES}


@app.post("/api/settings/telegram/unlink")
async def telegram_unlink(user: dict = Depends(auth.require_user)):
    users.set_telegram_chat_id(user["id"], None)
    return {"ok": True}


@app.get("/api/settings/telegram/status")
async def telegram_status(user: dict = Depends(auth.require_user)):
    fresh = users.get_user(user["id"]) or user
    return {"linked": bool(fresh.get("telegram_chat_id")), "notify_telegram": fresh.get("notify_telegram", True)}


class NotifyPrefIn(BaseModel):
    notify_telegram: bool


@app.put("/api/settings/notifications")
async def put_notification_prefs(payload: NotifyPrefIn, user: dict = Depends(auth.require_user)):
    users.set_notify_telegram(user["id"], payload.notify_telegram)
    return {"ok": True}


# ---------- وضعیت کلی ----------
@app.get("/api/status")
async def api_status(user: dict = Depends(auth.require_user)):
    return await bot_manager.get_status(None if user["role"] == "admin" else user["id"])


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


# ---------- Webhook تریدینگ‌ویو ----------
def _parse_webhook_payload(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "بدنه‌ی درخواست JSON معتبر نیست. پیام Alert را دقیقاً مطابق نمونه‌ی صفحه‌ی تنظیمات تنظیم کنید.")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_signal(signal: str) -> str:
    signal = signal.strip().lower()
    if signal == "long":
        return "buy"
    if signal == "short":
        return "sell"
    return signal


@app.post("/webhook/tradingview/{account_id}")
async def tradingview_webhook_account(account_id: str, request: Request):
    """نقطه‌ی دریافت سیگنال TradingView مخصوص یک حساب — هر حساب توکن اختصاصی خودش را دارد
    (آدرس/توکن از صفحه‌ی تنظیمات همان حساب قابل کپی است)، پس این سیگنال فقط روی همین یک
    حساب اعمال می‌شود و به حساب‌های سایر کاربران نشتی ندارد."""
    account = config_store.get_account(account_id)
    if account is None:
        raise HTTPException(404, "حساب پیدا نشد")
    payload = _parse_webhook_payload(await request.body())
    token = account.get("webhook_token") or ""
    if not token or not secrets.compare_digest(str(payload.get("token", "")), token):
        raise HTTPException(401, "توکن وبهوک اشتباه است.")

    symbol_raw = str(payload.get("symbol", "")).strip()
    signal = _normalize_signal(str(payload.get("signal", "")))
    if not symbol_raw or signal not in ("buy", "sell", "close"):
        raise HTTPException(400, "فیلدهای symbol و signal (buy/sell/close) الزامی هستند.")

    return await bot_manager.handle_webhook_signal(
        symbol_raw=symbol_raw,
        signal=signal,
        price=_to_float(payload.get("price")),
        stop_loss=_to_float(payload.get("sl")),
        take_profit=_to_float(payload.get("tp")),
        account_id=account_id,
    )


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    """
    نسخه‌ی قدیمی/سراسری (منسوخ) — فقط برای سازگاری با Alert هایی که قبل از معرفی
    وبهوک اختصاصی هر حساب (/webhook/tradingview/{account_id}) تنظیم شده‌اند نگه داشته
    شده. با توکن سراسری WEBHOOK_TOKEN در .env کار می‌کند و اگر account_id در پیام
    نباشد، ممکن است روی چند حساب هم‌نماد اعمال شود — لطفاً Alert های خود را به آدرس
    اختصاصی هر حساب (از صفحه‌ی تنظیمات) به‌روزرسانی کنید.
    """
    payload = _parse_webhook_payload(await request.body())

    if not settings.WEBHOOK_TOKEN:
        raise HTTPException(503, "WEBHOOK_TOKEN در فایل .env تنظیم نشده است؛ وبهوک غیرفعال است.")
    if not secrets.compare_digest(str(payload.get("token", "")), settings.WEBHOOK_TOKEN):
        raise HTTPException(401, "توکن وبهوک اشتباه است.")

    symbol_raw = str(payload.get("symbol", "")).strip()
    signal = _normalize_signal(str(payload.get("signal", "")))
    if not symbol_raw or signal not in ("buy", "sell", "close"):
        raise HTTPException(400, "فیلدهای symbol و signal (buy/sell/close) الزامی هستند.")

    return await bot_manager.handle_webhook_signal(
        symbol_raw=symbol_raw,
        signal=signal,
        price=_to_float(payload.get("price")),
        stop_loss=_to_float(payload.get("sl")),
        take_profit=_to_float(payload.get("tp")),
        account_id=payload.get("account_id"),
    )


# ---------- مدیریت حساب‌ها ----------
def _owned_account(account_id: str, user: dict) -> dict:
    """حساب را برمی‌گرداند؛ 404 اگر نبود، 403 اگر متعلق به این کاربر نباشد (ادمین همه را می‌بیند)."""
    account = config_store.get_account(account_id)
    if account is None:
        raise HTTPException(404, "حساب پیدا نشد")
    if user.get("role") != "admin" and account.get("owner_id") != user["id"]:
        raise HTTPException(403, "این حساب متعلق به شما نیست")
    return account


def _assert_can_go_live(user: dict):
    """ادمین محدودیت ندارد؛ کاربر عادی بدون توکن فعال نمی‌تواند حساب را live کند."""
    if user.get("role") == "admin":
        return
    if not tokens.has_active_token(user["id"]):
        raise HTTPException(
            403,
            "برای معامله‌ی واقعی (live) نیاز به توکن فعال‌سازی دارید — "
            "از صفحه‌ی پشتیبانی (واحد مالی) درخواست خرید توکن کنید.",
        )


@app.get("/api/accounts")
async def get_accounts(user: dict = Depends(auth.require_user)):
    return config_store.list_accounts(None if user["role"] == "admin" else user["id"])


@app.post("/api/accounts")
async def create_account(payload: AccountIn, user: dict = Depends(auth.require_user)):
    if payload.trading_mode == "live":
        _assert_can_go_live(user)
    account = config_store.add_account(payload.dict(), owner_id=user["id"])
    bot_manager.sync_from_config()
    return account


@app.post("/api/accounts/{account_id}/duplicate")
async def duplicate_account(account_id: str, user: dict = Depends(auth.require_user)):
    """ساخت بات جدید با کپی کامل تنظیمات و نمادهای یک حساب."""
    _owned_account(account_id, user)
    try:
        account = config_store.duplicate_account(account_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    # اگر منبع live بود ولی کاربر دیگر توکن فعال ندارد (مثلاً منقضی شده)، کپی paper می‌شود
    if account.get("trading_mode") == "live" and user.get("role") != "admin" and not tokens.has_active_token(user["id"]):
        account = config_store.update_account(account["id"], {"trading_mode": "paper"})
    bot_manager.sync_from_config()
    return account


@app.put("/api/accounts/{account_id}")
async def edit_account(account_id: str, payload: AccountIn, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    if payload.trading_mode == "live":
        _assert_can_go_live(user)
    try:
        account = config_store.update_account(account_id, payload.dict())
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.sync_from_config()
    return account


@app.post("/api/accounts/{account_id}/trading-mode")
async def set_trading_mode(account_id: str, mode: str, user: dict = Depends(auth.require_user)):
    """سوییچ paper/live از داشبورد. برای اعمال، حساب باید متوقف و دوباره شروع شود."""
    _owned_account(account_id, user)
    if mode not in ("paper", "live"):
        raise HTTPException(400, "حالت باید paper یا live باشد")
    if mode == "live":
        _assert_can_go_live(user)
    try:
        account = config_store.update_account(account_id, {"trading_mode": mode})
    except KeyError as e:
        raise HTTPException(404, str(e))
    await bot_manager.stop_account(account_id)
    bot_manager.sync_from_config()
    return account


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    await bot_manager.stop_account(account_id)
    config_store.delete_account(account_id)
    bot_manager.sync_from_config()
    return {"deleted": account_id}


@app.post("/api/accounts/{account_id}/test-connection")
async def test_connection(account_id: str, user: dict = Depends(auth.require_user)):
    cfg = _owned_account(account_id, user)

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
async def start_account(account_id: str, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    try:
        await bot_manager.start_account(account_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, f"شروع ربات ناموفق: {e}")
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/accounts/{account_id}/stop")
async def stop_account(account_id: str, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    await bot_manager.stop_account(account_id)
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/start-all")
async def start_all(user: dict = Depends(auth.require_user)):
    owner_id = None if user["role"] == "admin" else user["id"]
    await bot_manager.start_all(owner_id)
    return await bot_manager.get_status(owner_id)


@app.post("/api/stop-all")
async def stop_all(user: dict = Depends(auth.require_user)):
    owner_id = None if user["role"] == "admin" else user["id"]
    await bot_manager.stop_all(owner_id)
    return await bot_manager.get_status(owner_id)


# ---------- بستن دستی پوزیشن ----------
@app.post("/api/accounts/{account_id}/close-position")
async def close_position(account_id: str, payload: dict, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    result = await bot_manager.close_position_manual(account_id, payload)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "بستن پوزیشن ناموفق بود"))
    return result


# ---------- گزارش‌گیری و سود/زیان ----------
@app.get("/api/accounts/{account_id}/report")
async def account_report(account_id: str, days: int = 30, mode: str | None = None,
                         user: dict = Depends(auth.require_user)):
    account = _owned_account(account_id, user)
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
async def reset_account_report(account_id: str, user: dict = Depends(auth.require_user)):
    """پاکسازی و ریست کامل گزارش‌های یک حساب — از این لحظه مثل حساب خام ثبت می‌شود."""
    _owned_account(account_id, user)
    return history.reset_account(account_id)


# ---------- وبهوک اختصاصی هر حساب ----------
@app.get("/api/accounts/{account_id}/webhook-info")
async def account_webhook_info(account_id: str, request: Request, user: dict = Depends(auth.require_user)):
    """آدرس Webhook اختصاصی و نمونه‌ی پیام Alert در TradingView برای همین حساب."""
    account = _owned_account(account_id, user)
    host = request.headers.get("host", f"YOUR-SERVER-IP:{settings.DASHBOARD_PORT}")
    url = f"http://{host}/webhook/tradingview/{account_id}"
    sample = {
        "token": account.get("webhook_token", ""),
        "symbol": "{{ticker}}",
        "signal": "buy",            # buy | sell | close
        "price": "{{close}}",
        # اختیاری: اگر ندهید، از روی ATR خودکار محاسبه می‌شود
        # "sl": 60000, "tp": 70000,
    }
    return {"url": url, "sample": sample, "token_set": bool(account.get("webhook_token"))}


@app.post("/api/accounts/{account_id}/webhook-token/rotate")
async def account_rotate_webhook_token(account_id: str, request: Request, user: dict = Depends(auth.require_user)):
    """تولید توکن وبهوک جدید برای همین حساب (توکن قبلی بلافاصله باطل می‌شود)."""
    _owned_account(account_id, user)
    new_token = config_store.rotate_webhook_token(account_id)
    host = request.headers.get("host", f"YOUR-SERVER-IP:{settings.DASHBOARD_PORT}")
    return {"token": new_token, "url": f"http://{host}/webhook/tradingview/{account_id}"}


# ---------- مدیریت نمادها در هر حساب ----------
@app.post("/api/accounts/{account_id}/symbols/bulk")
async def bulk_update_symbols(account_id: str, payload: dict, user: dict = Depends(auth.require_user)):
    """اعمال دسته‌جمعی تنظیمات (تایم‌فریم، استراتژی، اهرم، حجم، وضعیت) روی همه نمادهای حساب."""
    _owned_account(account_id, user)
    try:
        count = config_store.bulk_update_symbols(account_id, payload or {})
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    bot_manager.sync_from_config()
    return {"updated": count}


@app.post("/api/accounts/{account_id}/symbols")
async def add_symbol(account_id: str, payload: SymbolIn, user: dict = Depends(auth.require_user)):
    account = _owned_account(account_id, user)
    data = payload.dict()
    data["symbol"] = normalize_symbol_for(account.get("exchange", "toobit"), data["symbol"])
    try:
        symbol_cfg = config_store.add_symbol(account_id, data)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return symbol_cfg


@app.put("/api/accounts/{account_id}/symbols/{symbol}")
async def edit_symbol(account_id: str, symbol: str, payload: SymbolIn, user: dict = Depends(auth.require_user)):
    account = _owned_account(account_id, user)
    data = payload.dict()
    data["symbol"] = normalize_symbol_for(account.get("exchange", "toobit"), data["symbol"])
    updated = config_store.update_symbol(account_id, symbol, data)
    if updated is None:
        raise HTTPException(404, "حساب یا نماد پیدا نشد")
    bot_manager.refresh_symbols(account_id)
    return updated


@app.delete("/api/accounts/{account_id}/symbols/{symbol}")
async def delete_symbol(account_id: str, symbol: str, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    try:
        config_store.remove_symbol(account_id, symbol)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return {"deleted": symbol}


@app.post("/api/accounts/{account_id}/symbols/{symbol}/toggle")
async def toggle_symbol(account_id: str, symbol: str, enabled: bool, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    try:
        config_store.toggle_symbol(account_id, symbol, enabled)
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.refresh_symbols(account_id)
    return {"symbol": symbol, "enabled": enabled}
