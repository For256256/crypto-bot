import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
from app.core import mailer
from app.core import i18n
from app.core import backup
from app.core import version as app_version
from app.core import twofa
from app.core.errors import ApiError
from app.core.exchanges.toobit import normalize_symbol

app = FastAPI(title="cpulsepro — Toobit / Tabdeal Futures")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_session_secret = settings.SESSION_SECRET_KEY or secrets.token_urlsafe(32)
if not settings.SESSION_SECRET_KEY:
    print("[crypto-bot] هشدار: SESSION_SECRET_KEY تنظیم نشده — یک کلید موقت تصادفی ساخته شد "
          "(با هر ری‌استارت سرویس، همه‌ی کاربران باید دوباره لاگین کنند). "
          "SESSION_SECRET_KEY را در .env تنظیم کنید.")
# https_only فقط وقتی روشن می‌شود که سرویس واقعاً پشت TLS باشد (PUBLIC_BASE_URL
# با https، یا SESSION_COOKIE_SECURE دستی). روشن‌کردنش روی نصب بدون TLS یعنی
# مرورگر کوکی را نمی‌فرستد و هیچ‌کس نمی‌تواند وارد شود.
app.add_middleware(SessionMiddleware, secret_key=_session_secret, session_cookie="cbot_session",
                   same_site="lax", max_age=2592000,
                   https_only=settings.SESSION_COOKIE_SECURE)


# پیام‌های ValueError لایه‌ی ذخیره‌سازی (که خودشان فارسی‌اند) به کلید ترجمه نگاشت
# می‌شوند؛ هر پیام ناشناخته همان متن اصلی را نگه می‌دارد.
_VALUE_ERROR_KEYS = {
    "نام کاربری باید حداقل ۳ کاراکتر باشد.": "err.username_short",
    "رمز عبور باید حداقل ۶ کاراکتر باشد.": "err.password_min",
    "ایمیل معتبر نیست.": "err.email_invalid",
    "این نام کاربری قبلاً ثبت شده است.": "err.username_taken",
    "موضوع و متن تیکت الزامی است.": "err.ticket_required",
    "متن پاسخ نمی‌تواند خالی باشد.": "err.reply_empty",
    "مدت‌زمان توکن باید بزرگ‌تر از صفر باشد.": "err.token_duration",
    "داده‌ی کندل برای بک‌تست کافی نیست (حداقل ~۲۳۰ کندل).": "err.candles_insufficient",
    "فایل پشتیبان معتبر نیست.": "err.backup_invalid",
    "فایل پشتیبان هیچ حسابی ندارد.": "err.backup_empty",
    "حالت بازیابی نامعتبر است.": "err.backup_mode",
}


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError):
    lang = i18n.resolve(request, auth.get_current_user(request))
    return JSONResponse({"detail": i18n.translate_or(lang, exc.key, exc.default, **exc.params)},
                        status_code=exc.status_code)


@app.exception_handler(auth.NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(auth.EmailNotVerified)
async def _email_unverified_handler(request: Request, exc: auth.EmailNotVerified):
    return RedirectResponse("/verify-email", status_code=status.HTTP_303_SEE_OTHER)


def render(request: Request, template: str, user: dict | None = None, **ctx):
    """تنها نقطه‌ی رندر تمپلیت‌ها — زبان، جهت (rtl/ltr) و تابع t() را یک‌جا تزریق
    می‌کند تا هیچ صفحه‌ای بدون i18n از قلم نیفتد."""
    lang = i18n.resolve(request, user)
    meta = i18n.LANGUAGES[lang]
    context = {
        "request": request,
        "user": user,
        "lang": lang,
        "lang_dir": meta["dir"],
        "lang_meta": meta,
        "languages": i18n.language_options(),
        # صرافی‌های قابل انتخاب در این زبان (تبدیل فقط فارسی)
        "allowed_exchanges": i18n.allowed_exchanges(lang),
        "catalog": i18n.get_catalog(lang),
        # نسخه‌ی در حال اجرا، در نوار کناری هر صفحه — تا بعد از هر آپدیت
        # بدون حدس‌زدن معلوم باشد مرورگر کد جدید را گرفته یا نه.
        "app_version": app_version.INFO,
        "t": lambda key, **params: i18n.translate(lang, key, **params),
        **ctx,
    }
    return templates.TemplateResponse(template, context)


@app.on_event("startup")
async def on_startup():
    admin = users.ensure_admin_seed(settings.DASHBOARD_USER, settings.DASHBOARD_PASSWORD)
    if admin is None:
        admin = next((u for u in users.list_users() if u.get("role") == "admin"), None)
    if admin is not None:
        config_store.migrate_owner_less_accounts(admin["id"])
    # کاربران قدیمی نباید بعد از آپدیت پشت صفحه‌ی تأیید ایمیل قفل شوند
    users.migrate_pre_verification_users()
    bot_manager.sync_from_config()
    bot_manager.start_background_tasks()
    asyncio.create_task(telegram.poll_updates_loop())
    # ربات‌هایی که هنگام خاموش‌شدن سرویس (مثلاً موقع آپدیت) در حال اجرا بودند
    # دوباره روشن می‌شوند — فقط همان‌ها. به‌صورت تسک پس‌زمینه، چون هر start
    # چند فراخوانی شبکه به صرافی دارد و نباید بالا آمدن سرور را عقب بیندازد.
    asyncio.create_task(bot_manager.resume_previously_running())


class AccountIn(BaseModel):
    name: str
    exchange: str = "toobit"
    account_type: str = "futures"        # futures | copy_trading (فقط Toobit)
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
    reversal_policy: str = "none"
    # حد ضرر دنبال‌کننده
    trailing_enabled: bool = False
    trailing_activation_pct: float = 1.0
    trailing_distance_pct: float = 1.0
    trailing_mode: str = "bot"           # bot | exchange
    invert_signals: bool = False
    # فیلتر روند تایم‌فریم بالاتر
    trend_filter_enabled: bool = False
    trend_filter_timeframe: str = "4h"
    trend_filter_method: str = "ema"     # ema | supertrend | both
    trend_filter_ema_length: int = 200
    accept_webhook: bool = True
    enabled: bool = True
    notify_telegram: bool = True
    notify_browser: bool = True
    # کد ورود دو مرحله‌ای — فقط وقتی لازم است که حساب live باشد
    otp: Optional[str] = None


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
    email: str


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.get_current_user(request) is not None:
        return RedirectResponse("/")
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request, payload: LoginIn):
    user = users.verify_login(payload.username, payload.password)
    if user is None:
        raise ApiError(401, "err.bad_credentials", "نام کاربری یا رمز عبور اشتباه است")
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
    return render(request, "register.html")


async def _send_email_code(user: dict) -> bool:
    """کد تأیید تازه می‌سازد و ایمیل می‌کند. False یعنی هنوز فاصله‌ی مجاز
    ارسال مجدد نگذشته است."""
    code = users.set_email_code(user["id"])
    if code is None:
        return False
    lang = i18n.user_lang(user)
    subject = i18n.translate(lang, "mail.verify_subject")
    body = i18n.translate(lang, "mail.verify_body", code=code,
                          minutes=users.EMAIL_CODE_TTL_MINUTES)
    if mailer.is_configured():
        asyncio.create_task(mailer.send_email(user.get("email") or "", subject, body))
    else:
        # بدون SMTP ایمیلی نمی‌رود؛ کد را در لاگ سرور می‌گذاریم تا ادمین
        # بتواند دستی به کاربر بدهد و کسی پشت این صفحه گیر نکند.
        print(f"[crypto-bot] SMTP تنظیم نشده — کد تأیید {user.get('username')}: {code}")
    return True


@app.post("/register")
async def register_submit(request: Request, payload: RegisterIn):
    try:
        user = users.create_user(payload.username, payload.password, email=payload.email, role="user")
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    request.session["user_id"] = user["id"]
    await _send_email_code(user)
    asyncio.create_task(telegram.notify_admin(
        i18n.translate(i18n.for_admin(), "notify.new_user", username=user["username"])))
    return {"ok": True, "verify_required": auth.email_verification_required()}


# ---------- تأیید ایمیل ----------
class EmailCodeIn(BaseModel):
    code: str


@app.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request):
    user = auth.get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if auth.is_verified(user):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "verify_email.html", user, active="")


@app.post("/api/verify-email")
async def verify_email_submit(body: EmailCodeIn, user: dict = Depends(auth.require_session)):
    if users.verify_email_code(user["id"], body.code):
        return {"ok": True}
    raise ApiError(400, "err.email_code_invalid", "کد تأیید درست نیست یا منقضی شده است.")


@app.post("/api/verify-email/resend")
async def verify_email_resend(user: dict = Depends(auth.require_session)):
    if auth.is_verified(user):
        return {"ok": True, "already": True}
    if not await _send_email_code(user):
        raise ApiError(429, "err.email_code_wait",
                       "کمی صبر کنید و دوباره درخواست دهید.")
    return {"ok": True}


# ---------- ورود دو مرحله‌ای ----------
class OtpIn(BaseModel):
    code: str


@app.get("/api/2fa/status")
async def twofa_status(user: dict = Depends(auth.require_user)):
    return {
        "enabled": bool(user.get("totp_enabled")),
        "pending": bool(user.get("totp_secret") and not user.get("totp_enabled")),
        "recovery_left": len(user.get("recovery_codes") or []),
    }


@app.post("/api/2fa/setup")
async def twofa_setup(user: dict = Depends(auth.require_user)):
    """secret تازه می‌سازد ولی فعالش نمی‌کند. تا وقتی کاربر یک کد درست وارد
    نکرده، هیچ چیزی عوض نمی‌شود — وگرنه کسی که QR را اسکن نکرده خودش را از
    معامله‌ی واقعی محروم می‌کند."""
    if user.get("totp_enabled"):
        raise ApiError(400, "err.2fa_already", "ورود دو مرحله‌ای از قبل فعال است.")
    secret = twofa.generate_secret()
    users.set_totp_secret(user["id"], secret)
    return {
        "secret": secret,
        "uri": twofa.provisioning_uri(secret, user.get("username") or user["id"]),
    }


@app.post("/api/2fa/enable")
async def twofa_enable(body: OtpIn, user: dict = Depends(auth.require_user)):
    secret = user.get("totp_secret")
    if not secret:
        raise ApiError(400, "err.2fa_setup_first", "ابتدا ورود دو مرحله‌ای را راه‌اندازی کنید.")
    if not twofa.verify(secret, body.code):
        raise ApiError(400, "err.otp_invalid", "کد ورود دو مرحله‌ای درست نیست.")
    codes = twofa.generate_recovery_codes()
    hashes = [users._hash_password(twofa.normalize_recovery(c)) for c in codes]
    users.enable_totp(user["id"], hashes)
    # تنها باری است که کدهای بازیابی به‌صورت متن ساده دیده می‌شوند
    return {"ok": True, "recovery_codes": codes}


@app.post("/api/2fa/disable")
async def twofa_disable(body: OtpIn, user: dict = Depends(auth.require_user)):
    """غیرفعال‌کردن هم کد می‌خواهد، وگرنه هرکسی که به سشن باز دسترسی پیدا کند
    می‌تواند اول ۲FA را خاموش کند و بعد حساب را live کند."""
    if not user.get("totp_enabled"):
        return {"ok": True}
    code = (body.code or "").strip()
    if not (twofa.verify(user.get("totp_secret") or "", code)
            or users.consume_recovery_code(user["id"], twofa.normalize_recovery(code))):
        raise ApiError(400, "err.otp_invalid", "کد ورود دو مرحله‌ای درست نیست.")
    live = [a for a in config_store.list_accounts(user["id"])
            if a.get("trading_mode") == "live"]
    users.disable_totp(user["id"])
    # حساب‌های واقعی بدون ۲FA نباید فعال بمانند، وگرنه غیرفعال‌کردن ۲FA
    # راهی می‌شود برای دور زدن همین محافظ.
    for a in live:
        config_store.update_account(a["id"], {"trading_mode": "paper"})
    if live:
        bot_manager.sync_from_config()
    return {"ok": True, "reverted_to_paper": [a["name"] for a in live]}


# ---------- زبان ----------
@app.get("/api/languages")
async def list_languages(request: Request):
    user = auth.get_current_user(request)
    return {"current": i18n.resolve(request, user), "languages": i18n.language_options()}


@app.post("/api/language/{lang}")
async def set_language(lang: str, request: Request):
    """زبان را هم در کوکی می‌گذارد (برای مهمان‌ها و صفحه‌ی ورود) و هم — اگر
    کاربر لاگین باشد — روی حسابش ذخیره می‌کند تا روی هر دستگاهی یکسان بماند."""
    if not i18n.is_supported(lang):
        raise ApiError(400, "err.unsupported_language", "Unsupported language")
    user = auth.get_current_user(request)
    if user is not None:
        users.set_lang(user["id"], lang)
    resp = JSONResponse({"ok": True, "lang": lang})
    resp.set_cookie(i18n.COOKIE_NAME, lang, max_age=i18n.COOKIE_MAX_AGE,
                    samesite="lax", httponly=False,
                    secure=settings.SESSION_COOKIE_SECURE)
    return resp


# ---------- پروفایل کاربری ----------
class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdateIn(BaseModel):
    email: str


@app.post("/api/profile/change-password")
async def change_own_password(payload: ChangePasswordIn, user: dict = Depends(auth.require_user)):
    if users.verify_login(user["username"], payload.current_password) is None:
        raise ApiError(400, "err.current_password_wrong", "رمز عبور فعلی اشتباه است")
    if not payload.new_password or len(payload.new_password) < 6:
        raise ApiError(400, "err.password_too_short", "رمز عبور جدید باید حداقل ۶ کاراکتر باشد.")
    users.set_password(user["id"], payload.new_password)
    return {"ok": True}


@app.put("/api/profile")
async def update_own_profile(payload: ProfileUpdateIn, user: dict = Depends(auth.require_user)):
    try:
        updated = users.set_email(user["id"], payload.email)
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    return users.public_view(updated)


def _list_strategies():
    from app.core.strategies.registry import list_strategies
    return list_strategies()


# ---------- صفحات داشبورد ----------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = auth.get_current_user(request)
    if user is None:
        # تعداد استراتژی از خود رجیستری خوانده می‌شود؛ قبلاً در تمپلیت هاردکد
        # بود و با اضافه‌شدن استراتژی‌های تازه بی‌سروصدا قدیمی می‌ماند.
        return render(request, "landing.html",
                      strategy_count=len(_list_strategies()))
    # این route عمداً Depends(require_user_page) ندارد چون برای مهمان صفحه‌ی
    # معرفی را نشان می‌دهد؛ پس گیت تأیید ایمیل را باید خودمان صدا بزنیم.
    if not auth.is_verified(user):
        return RedirectResponse("/verify-email", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "dashboard.html", user, active="dashboard")


@app.get("/strategies", response_class=HTMLResponse)
async def strategies_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "strategies.html", user, active="strategies")


@app.get("/learn", response_class=HTMLResponse)
async def learn_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "learn.html", user, active="learn")


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "reports.html", user, active="reports")


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "logs.html", user, active="logs")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "settings.html", user, active="settings")


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "support.html", user, active="support")


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, user: dict = Depends(auth.require_user_page)):
    return render(request, "billing.html", user, active="billing")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: dict = Depends(auth.require_admin_page)):
    return render(request, "admin.html", user, active="admin")


# ---------- بک‌تست استراتژی ----------
class BacktestIn(BaseModel):
    symbol: str
    timeframe: str = "1h"
    strategy: str = "supertrend_ema_rsi"
    strategy_params: dict = {}
    candles: int = 500
    invert: bool = False
    reversal_policy: str = "none"
    # فیلتر روند تایم‌فریم بالاتر (خالی = خاموش)
    trend_filter_enabled: bool = False
    trend_filter_timeframe: str = "4h"
    trend_filter_method: str = "ema"
    trend_filter_ema_length: int = 200


@app.post("/api/backtest")
async def run_backtest_api(payload: BacktestIn, _: dict = Depends(auth.require_user)):
    from app.core.backtest import run_backtest
    from app.core.exchanges.toobit import ToobitDriver
    from app.core.exchanges.base import ExchangeError
    from app.core.strategies.registry import STRATEGIES

    if payload.strategy not in STRATEGIES:
        raise ApiError(400, "err.unknown_strategy", "استراتژی ناشناخته است")
    driver = ToobitDriver(api_key="", api_secret="", base_url=settings.TOOBIT_BASE_URL)
    trend_df = None
    try:
        symbol = normalize_symbol(payload.symbol)
        df = await driver.get_candles(symbol, payload.timeframe,
                                      min(max(payload.candles, 100), 1000))
        if payload.trend_filter_enabled:
            # حاشیه‌ی گرم‌کردن اندیکاتور روند، مثل موتور زنده
            need = min(max(int(payload.trend_filter_ema_length) + 200, 300), 1000)
            trend_df = await driver.get_candles(symbol, payload.trend_filter_timeframe, need)
        await driver.close()
    except ExchangeError as e:
        await driver.close()
        raise ApiError(502, "err.candles_failed", f"دریافت کندل از Toobit ناموفق: {e}", msg=str(e))
    try:
        return run_backtest(df, payload.strategy, payload.strategy_params, invert=payload.invert,
                            reversal_policy=payload.reversal_policy,
                            trend_df=trend_df,
                            trend_method=payload.trend_filter_method,
                            trend_ema_length=payload.trend_filter_ema_length,
                            trend_timeframe=payload.trend_filter_timeframe)
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))


# ---------- تیکت‌های پشتیبانی ----------
class TicketIn(BaseModel):
    subject: str
    body: str
    unit: str = "general"   # کلید پایدار؛ برچسب قدیمی فارسی هم پذیرفته می‌شود


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
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    admin_lang = i18n.for_admin()
    asyncio.create_task(telegram.notify_admin(i18n.translate(
        admin_lang, "notify.new_ticket",
        unit=i18n.translate(admin_lang, f"unit.{ticket.get('unit_key', 'general')}"),
        username=user["username"], subject=ticket["subject"])))
    return ticket


@app.post("/api/tickets/{ticket_id}/reply")
async def reply_ticket_as_user(ticket_id: str, payload: ReplyIn, user: dict = Depends(auth.require_user)):
    """ادامه دادن گفتگوی یک تیکت — هم صاحب تیکت و هم ادمین می‌توانند پاسخ اضافه کنند."""
    ticket = tickets.get_ticket(ticket_id)
    if ticket is None:
        raise ApiError(404, "err.ticket_not_found", "تیکت پیدا نشد")
    is_admin = user["role"] == "admin"
    if not is_admin and ticket.get("user_id") != user["id"]:
        raise ApiError(403, "err.ticket_not_yours", "این تیکت متعلق به شما نیست")
    try:
        return tickets.add_reply(ticket_id, payload.message, user["id"], user["username"],
                                 "admin" if is_admin else "user")
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))


@app.post("/api/admin/tickets/{ticket_id}/close")
async def close_ticket_api(ticket_id: str, _: dict = Depends(auth.require_admin)):
    ticket = tickets.close_ticket(ticket_id)
    if ticket is None:
        raise ApiError(404, "err.ticket_not_found", "تیکت پیدا نشد")
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


@app.get("/api/tokens/history")
async def my_token_history(user: dict = Depends(auth.require_user)):
    return tokens.list_tokens(user["id"])


@app.get("/api/admin/tokens")
async def admin_list_tokens(_: dict = Depends(auth.require_admin)):
    return tokens.list_tokens()


@app.post("/api/admin/tokens")
async def admin_issue_token(payload: TokenIssueIn, user: dict = Depends(auth.require_admin)):
    if users.get_user(payload.user_id) is None:
        raise ApiError(404, "err.user_not_found", "کاربر پیدا نشد")
    try:
        token = tokens.issue_token(payload.user_id, payload.duration_days, payload.note, user["id"])
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    target_lang = i18n.for_user(payload.user_id)
    msg = i18n.translate(target_lang, "notify.token_issued", days=payload.duration_days)
    subject = i18n.translate(target_lang, "notify.token_issued_subject")
    asyncio.create_task(telegram.notify_user(payload.user_id, msg))
    asyncio.create_task(mailer.notify_user_email(payload.user_id, subject, msg))
    return token


@app.post("/api/admin/tokens/{token_id}/revoke")
async def admin_revoke_token(token_id: str, _: dict = Depends(auth.require_admin)):
    token = tokens.revoke_token(token_id)
    if token is None:
        raise ApiError(404, "err.token_not_found", "توکن پیدا نشد")
    return token


# ---------- خرید توکن (کیف پول دستی/آفلاین) ----------
class PaymentPlanIn(BaseModel):
    days: int
    price_usdt: float


class PaymentSettingsIn(BaseModel):
    wallet_address: str
    wallet_network: str = "USDT (TRC20)"
    plans: list[PaymentPlanIn]


@app.get("/api/payment/plans")
async def get_payment_plans(_: dict = Depends(auth.require_user)):
    return app_settings.get_settings().get("payment") or {}


@app.get("/api/admin/payment-settings")
async def get_payment_settings(_: dict = Depends(auth.require_admin)):
    return app_settings.get_settings().get("payment") or {}


@app.put("/api/admin/payment-settings")
async def put_payment_settings(payload: PaymentSettingsIn, _: dict = Depends(auth.require_admin)):
    if not payload.wallet_address.strip():
        raise ApiError(400, "err.wallet_required", "آدرس ولت الزامی است.")
    if not payload.plans:
        raise ApiError(400, "err.plan_required", "حداقل یک پلن لازم است.")
    app_settings.update_settings({"payment": {
        "wallet_address": payload.wallet_address.strip(),
        "wallet_network": payload.wallet_network.strip() or "USDT (TRC20)",
        "plans": [p.dict() for p in payload.plans],
    }})
    return app_settings.get_settings().get("payment")


class DepositSubmitIn(BaseModel):
    plan_days: int
    plan_price: float
    note: str = ""


@app.post("/api/billing/submit-deposit")
async def submit_deposit(payload: DepositSubmitIn, user: dict = Depends(auth.require_user)):
    subject = f"درخواست فعال‌سازی توکن — {payload.plan_days} روزه ({payload.plan_price} USDT)"
    body = (payload.note or "").strip() or "اطلاعات واریز پیوست نشده — لطفاً رسید/هش تراکنش را از طریق پاسخ تیکت ارسال کنید."
    try:
        ticket = tickets.create_ticket(subject, body, "billing", user["id"], user["username"])
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    asyncio.create_task(telegram.notify_admin(i18n.translate(
        i18n.for_admin(), "notify.new_deposit_admin", username=user["username"],
        days=payload.plan_days, price=payload.plan_price, ticket=ticket["id"])))
    user_lang = i18n.user_lang(user)
    receipt = i18n.translate(user_lang, "notify.deposit_receipt",
                             days=payload.plan_days, price=payload.plan_price, ticket=ticket["id"])
    asyncio.create_task(telegram.notify_user(user["id"], receipt))
    asyncio.create_task(mailer.notify_user_email(
        user["id"], i18n.translate(user_lang, "notify.deposit_receipt_subject"), receipt))
    return ticket


# ---------- مدیریت کاربران (پنل ادمین) ----------
class ResetPasswordIn(BaseModel):
    new_password: str = ""


# ---------- حساب‌های پیشنهادی ادمین ----------
def _suggested_view(acc: dict) -> dict:
    """نمای عمومی یک حساب پیشنهادی — فقط تنظیمات و روند کلی.

    کلید API، توکن وبهوک و شناسه‌ی مالک عمداً بیرون نمی‌روند؛ کاربر قرار است
    فقط روند و پیکربندی را ببیند و از رویش یک حساب مشابه بسازد.
    """
    report = history.get_report(acc["id"], days=30, mode=acc.get("trading_mode"))
    summary = report.get("summary") or {}
    return {
        "id": acc["id"],
        "name": acc.get("name", ""),
        "exchange": acc.get("exchange", ""),
        "trading_mode": acc.get("trading_mode", "paper"),
        "settings": {
            "risk_percent": acc.get("risk_percent"),
            "default_leverage": acc.get("default_leverage"),
            "sl_tp_atr_mult": acc.get("sl_tp_atr_mult"),
            "max_margin_per_trade_pct": acc.get("max_margin_per_trade_pct"),
            "max_open_positions": acc.get("max_open_positions"),
            "max_daily_loss_percent": acc.get("max_daily_loss_percent"),
        },
        "symbols": [
            {"symbol": s.get("symbol"), "timeframe": s.get("timeframe"), "strategy": s.get("strategy")}
            for s in (acc.get("symbols") or [])
        ],
        "trend": {
            "equity_curve": report.get("equity_curve") or [],
            "net_pnl": summary.get("net_pnl"),
            "win_rate": summary.get("win_rate"),
            "trades": summary.get("trades"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "profit_factor": summary.get("profit_factor"),
        },
    }


@app.get("/api/suggested-accounts")
async def get_suggested_accounts(request: Request, user: dict = Depends(auth.require_user)):
    # حساب پیشنهادی روی صرافی‌ای که این زبان نمی‌بیند اصلاً نمایش داده نمی‌شود،
    # وگرنه کاربر کارتی می‌بیند که کلیک روی «ساختن مشابه» آن ۴۰۳ می‌گیرد.
    lang = i18n.resolve(request, user)
    return [_suggested_view(a) for a in config_store.list_suggested()
            if i18n.exchange_allowed(a.get("exchange", "toobit"), lang)]


@app.post("/api/suggested-accounts/{account_id}/clone")
async def clone_suggested_account(account_id: str, request: Request,
                                  user: dict = Depends(auth.require_user)):
    """از یک حساب پیشنهادی، حساب مشابهی برای کاربر جاری می‌سازد."""
    src = config_store.get_account(account_id)
    if src is None or not src.get("is_suggested"):
        raise ApiError(404, "err.account_not_found", "حساب پیدا نشد")
    _assert_exchange_allowed(src.get("exchange", "toobit"), request, user)
    account = config_store.clone_for_user(account_id, user["id"])
    bot_manager.sync_from_config()
    return account


@app.post("/api/admin/accounts/{account_id}/suggested")
async def admin_set_suggested(account_id: str, is_suggested: bool, user: dict = Depends(auth.require_admin)):
    """فقط حساب‌های خودِ ادمین می‌توانند پیشنهادی شوند — نه حساب کاربران."""
    account = config_store.get_account(account_id)
    if account is None:
        raise ApiError(404, "err.account_not_found", "حساب پیدا نشد")
    if account.get("owner_id") != user["id"]:
        raise ApiError(403, "err.account_not_yours", "این حساب متعلق به شما نیست")
    return config_store.set_suggested(account_id, is_suggested)


@app.get("/api/admin/user-accounts")
async def admin_user_accounts(user: dict = Depends(auth.require_admin)):
    """حساب‌های ساخته‌شده توسط کاربران، تفکیک‌شده بر اساس کاربر.

    حساب‌های خودِ ادمینِ درخواست‌دهنده عمداً حذف می‌شوند — آن‌ها در پیشخوان
    خودش دیده می‌شوند و نباید با حساب‌های کاربران قاطی شوند.
    """
    status_map = (await bot_manager.get_status())["accounts"]
    by_owner: dict[str, dict] = {}
    for acc in config_store.list_accounts():
        owner_id = acc.get("owner_id")
        if not owner_id or owner_id == user["id"]:
            continue
        st = status_map.get(acc["id"]) or {}
        stats = st.get("account_stats") or {}
        # سود محقق‌شده از تاریخچه خوانده می‌شود، نه از وضعیت زنده: account_stats
        # فقط وقتی وجود دارد که ربات همان لحظه در حال اجرا باشد، و بدون این،
        # هر حساب متوقفی در مرتب‌سازی سود صفر به حساب می‌آمد.
        summary = (history.get_report(acc["id"], days=0,
                                      mode=acc.get("trading_mode")) or {}).get("summary") or {}
        group = by_owner.setdefault(owner_id, {"accounts": []})
        group["accounts"].append({
            "id": acc["id"],
            "name": acc.get("name", ""),
            "exchange": acc.get("exchange", ""),
            "trading_mode": acc.get("trading_mode", "paper"),
            "enabled": acc.get("enabled", True),
            "symbols_count": len(acc.get("symbols") or []),
            "running": bool(st.get("running")),
            "equity": stats.get("current_equity"),
            "overall_profit_pct": stats.get("overall_profit_pct"),
            "open_positions": len(st.get("positions") or []),
            "net_pnl": summary.get("total_pnl", 0.0),
            "trades": summary.get("trades", 0),
            "win_rate": summary.get("win_rate", 0.0),
            "total_fees": summary.get("total_fees", 0.0),
            "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
            "profit_factor": summary.get("profit_factor"),
        })
    out = []
    for owner_id, group in by_owner.items():
        owner = users.get_user(owner_id)
        accs = sorted(group["accounts"], key=lambda a: a.get("net_pnl") or 0.0, reverse=True)
        out.append({
            "user_id": owner_id,
            "username": owner["username"] if owner else owner_id,
            "email": (owner or {}).get("email"),
            "enabled": (owner or {}).get("enabled", True),
            "has_active_token": tokens.has_active_token(owner_id),
            "accounts": accs,
            "total_pnl": sum(a.get("net_pnl") or 0.0 for a in accs),
            "total_trades": sum(a.get("trades") or 0 for a in accs),
            "running_count": sum(1 for a in accs if a.get("running")),
            "live_count": sum(1 for a in accs if a.get("trading_mode") == "live"),
        })
    # پرسودترین کاربر اول — همان ترتیبی که برای مرور سریع ادمین مفید است.
    return sorted(out, key=lambda g: g["total_pnl"], reverse=True)


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: dict = Depends(auth.require_admin)):
    """حذف کامل یک کاربر به‌همراه حساب‌ها، توکن‌ها و تیکت‌هایش.

    دو محافظ که سمت سرور اعمال می‌شوند، نه فقط در رابط کاربری:
    - ادمین نمی‌تواند خودش را پاک کند (وگرنه با یک کلیک از پنل بیرون می‌ماند).
    - هیچ ادمینی از این مسیر پاک نمی‌شود؛ برداشتن دسترسی ادمین باید کار
      آگاهانه‌ای روی فایل باشد، نه یک دکمه در فهرست کاربران.

    ربات‌های در حال اجرای کاربر اول متوقف می‌شوند، وگرنه تسک‌شان بعد از
    پاک‌شدن پیکربندی هم به کار ادامه می‌داد.
    """
    if user_id == admin["id"]:
        raise ApiError(400, "err.cannot_delete_self", "نمی‌توانید حساب کاربری خودتان را حذف کنید.")
    target = users.get_user(user_id)
    if target is None:
        raise ApiError(404, "err.user_not_found", "کاربر پیدا نشد")
    if target.get("role") == "admin":
        raise ApiError(400, "err.cannot_delete_admin", "کاربر ادمین از این مسیر حذف نمی‌شود.")

    accounts = config_store.list_accounts(user_id)
    n_trades = 0
    for acc in accounts:
        await bot_manager.stop_account(acc["id"])
        # تاریخچه با account_id کلید می‌خورد نه user_id، پس باید همین‌جا و
        # قبل از پاک‌شدن پیکربندی برداشته شود — بعد از آن دیگر راهی برای
        # فهمیدن اینکه کدام رکورد مال چه کسی بوده باقی نمی‌ماند.
        n_trades += (history.reset_account(acc["id"]) or {}).get("trades_removed", 0)
        config_store.delete_account(acc["id"])
    n_tokens = tokens.delete_by_user(user_id)
    n_tickets = tickets.delete_by_user(user_id)
    users.delete_user(user_id)
    bot_manager.sync_from_config()
    return {"deleted": user_id, "username": target.get("username", ""),
            "accounts": len(accounts), "tokens": n_tokens, "tickets": n_tickets,
            "trades": n_trades}


@app.delete("/api/admin/users/{user_id}/accounts")
async def admin_delete_user_accounts(user_id: str, admin: dict = Depends(auth.require_admin)):
    """پاکسازی همه‌ی حساب‌های یک کاربر — فقط وقتی کاربر غیرفعال شده باشد.

    این شرط عمدی است و صرفاً محافظت از خطا نیست: بدون آن، یک کلیک اشتباه روی
    ردیف کاربر فعال، بات‌های در حال معامله‌ی او را با هم پاک می‌کرد. برای
    حذف تکی، همان مسیر DELETE /api/accounts/{id} در دسترس ادمین هست.
    """
    target = users.get_user(user_id)
    if target is None:
        raise ApiError(404, "err.user_not_found", "کاربر پیدا نشد")
    if target.get("enabled", True):
        raise ApiError(400, "err.user_still_enabled",
                       "فقط حساب‌های کاربرِ غیرفعال‌شده قابل پاکسازی است. "
                       "ابتدا کاربر را غیرفعال کنید.")
    mine = config_store.list_accounts(user_id)
    for acc in mine:
        await bot_manager.stop_account(acc["id"])
        history.reset_account(acc["id"])
        config_store.delete_account(acc["id"])
    bot_manager.sync_from_config()
    return {"deleted": len(mine), "user_id": user_id}


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
        raise ApiError(404, "err.user_not_found", "کاربر پیدا نشد")
    return users.public_view(user)


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, payload: ResetPasswordIn, _: dict = Depends(auth.require_admin)):
    """چون فعلاً ایمیل/فراموشی‌رمز نداریم، این تنها راه بازیابی رمز کاربر است."""
    new_password = payload.new_password.strip() or secrets.token_urlsafe(9)
    if len(new_password) < 6:
        raise ApiError(400, "err.password_min", "رمز عبور باید حداقل ۶ کاراکتر باشد.")
    user = users.set_password(user_id, new_password)
    if user is None:
        raise ApiError(404, "err.user_not_found", "کاربر پیدا نشد")
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
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))


@app.put("/api/admin/presets/{preset_id}")
async def update_preset_api(preset_id: str, payload: PresetIn, _: dict = Depends(auth.require_admin)):
    preset = presets.update_preset(preset_id, payload.dict())
    if preset is None:
        raise ApiError(404, "err.preset_not_found", "پیش‌فرض پیدا نشد")
    return preset


@app.delete("/api/admin/presets/{preset_id}")
async def delete_preset_api(preset_id: str, _: dict = Depends(auth.require_admin)):
    if not presets.delete_preset(preset_id):
        raise ApiError(404, "err.preset_not_found", "پیش‌فرض پیدا نشد")
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
        raise ApiError(400, "err.bot_token_invalid", "توکن ربات نامعتبر است — از BotFather یک توکن معتبر بگیرید.")
    app_settings.update_settings({"telegram": {
        "bot_token": bot_token, "bot_username": me.get("username", ""),
        "admin_chat_id": payload.admin_chat_id.strip(),
    }})
    return {"ok": True, "bot_username": me.get("username", "")}


class EmailSettingsIn(BaseModel):
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    from_name: str = "cpulsepro"
    use_tls: bool = True


@app.get("/api/admin/email-settings")
async def get_email_settings(_: dict = Depends(auth.require_admin)):
    data = app_settings.get_settings().get("email") or {}
    out = dict(data)
    out["smtp_password_set"] = bool(out.pop("smtp_password", ""))
    return out


@app.put("/api/admin/email-settings")
async def put_email_settings(payload: EmailSettingsIn, _: dict = Depends(auth.require_admin)):
    cfg = payload.dict()
    if not cfg["smtp_password"]:
        # اگر رمز خالی فرستاده شده، رمز قبلی حفظ می‌شود (برای جلوگیری از پاک‌شدن با ذخیره‌ی مجدد)
        cfg["smtp_password"] = (app_settings.get_settings().get("email") or {}).get("smtp_password", "")
    ok, error = await mailer.test_connection(cfg)
    if not ok:
        # علت واقعی به ادمین برگردانده می‌شود؛ «ناموفق بود» به‌تنهایی یعنی
        # باید کورکورانه میزبان و پورت و TLS را یکی‌یکی امتحان کند.
        raise ApiError(400, "err.smtp_failed",
                       f"اتصال به سرور SMTP ناموفق بود: {error}", reason=error)
    app_settings.update_settings({"email": cfg})
    return {"ok": True}


@app.post("/api/admin/email-settings/test")
async def send_test_email(admin: dict = Depends(auth.require_admin)):
    """یک ایمیل واقعی به آدرس خودِ ادمین می‌فرستد.

    تست اتصال فقط می‌گوید لاگین گرفت؛ نمی‌گوید نامه واقعاً تحویل داده می‌شود.
    مشکل‌های SPF/DKIM و ردشدن آدرس فرستنده تازه در همین مرحله معلوم می‌شوند.
    """
    to = (admin.get("email") or "").strip()
    if not to:
        raise ApiError(400, "err.no_admin_email",
                       "برای دریافت ایمیل آزمایشی، اول یک ایمیل در پروفایل خودتان ثبت کنید.")
    if not mailer.is_configured():
        raise ApiError(400, "err.smtp_unset", "ابتدا تنظیمات SMTP را ذخیره کنید.")
    lang = i18n.user_lang(admin)
    ok = await mailer.send_email(
        to,
        i18n.translate(lang, "mail.test_subject"),
        i18n.translate(lang, "mail.test_body"),
    )
    status = mailer.last_status()
    if not ok:
        raise ApiError(400, "err.smtp_send_failed",
                       f"ارسال ناموفق بود: {status.get('error') or ''}",
                       reason=status.get("error") or "")
    return {"ok": True, "to": to}


@app.get("/api/admin/email-settings/status")
async def email_last_status(_: dict = Depends(auth.require_admin)):
    """نتیجه‌ی آخرین تلاش ارسال — برای اینکه خرابی SMTP بی‌صدا نماند."""
    return mailer.last_status()


@app.get("/api/settings/telegram/link-code")
async def telegram_link_code(user: dict = Depends(auth.require_user)):
    bot_username = (app_settings.get_settings().get("telegram") or {}).get("bot_username", "")
    if not bot_username:
        raise ApiError(503, "err.telegram_not_configured", "ربات تلگرام هنوز توسط ادمین تنظیم نشده است.")
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
    # ادمین هم فقط حساب‌های خودش را می‌بیند؛ حساب‌های کاربران در پنل مدیریت و
    # تفکیک‌شده بر اساس کاربر نمایش داده می‌شوند.
    return await bot_manager.get_status(user["id"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "app": "crypto-bot", "port": settings.DASHBOARD_PORT}


@app.get("/api/strategies")
async def get_strategies(request: Request, user: dict = Depends(auth.require_user)):
    """برچسب استراتژی‌ها و پارامترها به زبان همان درخواست ترجمه می‌شوند. کلیدها
    (strategy key / param key) پایدارند، پس رجیستری دست‌نخورده می‌ماند و برچسب
    فارسی موجود به‌عنوان fallback عمل می‌کند."""
    from app.core.strategies.registry import list_strategies
    lang = i18n.resolve(request, user)
    out = []
    for st in list_strategies():
        item = dict(st)
        item["label"] = i18n.translate_or(lang, f"strategy.{st['key']}.label", st.get("label", st["key"]))
        item["params_schema"] = [
            {**p, "label": i18n.translate_or(lang, f"strategy.param.{p['key']}", p.get("label", p["key"]))}
            for p in st.get("params_schema", [])
        ]
        out.append(item)
    return out


# کش لیست نمادهای فیوچرز، به تفکیک صرافی (یک ساعت)
_symbols_cache: dict = {}


@app.get("/api/futures-symbols")
async def futures_symbols(request: Request, exchange: str = "toobit", force: bool = False,
                          user: dict = Depends(auth.require_user)):
    """لیست همه‌ی نمادهای قابل معامله در فیوچرز صرافی انتخاب‌شده — برای لیست کشویی داشبورد.
    endpoint عمومی صرافی است و نیازی به کلید API ندارد."""
    _assert_exchange_allowed(exchange, request, user, allow_owned=True)
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
        raise ApiError(400, "err.unsupported_exchange", f"صرافی پشتیبانی‌نشده: {exchange}", exchange=exchange)

    try:
        symbols = await driver.list_symbols()
        await driver.close()
    except ExchangeError as e:
        await driver.close()
        # اگر صرافی در دسترس نبود، کش قبلی (حتی قدیمی) را برگردان تا داشبورد از کار نیفتد
        if cache["data"]:
            return {"symbols": cache["data"], "cached": True, "warning": str(e)}
        raise ApiError(502, "err.symbols_failed", f"دریافت لیست نمادها از {label} ناموفق: {e}", label=label, msg=str(e))
    except Exception as e:
        await driver.close()
        if cache["data"]:
            return {"symbols": cache["data"], "cached": True, "warning": str(e)}
        raise ApiError(502, "err.symbols_unexpected", f"خطای غیرمنتظره در دریافت لیست نمادها: {e}", msg=str(e))

    cache["data"] = symbols
    cache["time"] = now
    return {"symbols": symbols, "cached": False}


# ---------- Webhook تریدینگ‌ویو ----------
def _parse_webhook_payload(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "err.bad_json", "بدنه‌ی درخواست JSON معتبر نیست. پیام Alert را دقیقاً مطابق نمونه‌ی صفحه‌ی تنظیمات تنظیم کنید.")


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
        raise ApiError(404, "err.account_not_found", "حساب پیدا نشد")
    payload = _parse_webhook_payload(await request.body())
    token = account.get("webhook_token") or ""
    if not token or not secrets.compare_digest(str(payload.get("token", "")), token):
        raise ApiError(401, "err.webhook_token_wrong", "توکن وبهوک اشتباه است.")

    symbol_raw = str(payload.get("symbol", "")).strip()
    signal = _normalize_signal(str(payload.get("signal", "")))
    if not symbol_raw or signal not in ("buy", "sell", "close"):
        raise ApiError(400, "err.webhook_fields", "فیلدهای symbol و signal (buy/sell/close) الزامی هستند.")

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
        raise ApiError(503, "err.webhook_token_unset", "WEBHOOK_TOKEN در فایل .env تنظیم نشده است؛ وبهوک غیرفعال است.")
    if not secrets.compare_digest(str(payload.get("token", "")), settings.WEBHOOK_TOKEN):
        raise ApiError(401, "err.webhook_token_wrong", "توکن وبهوک اشتباه است.")

    symbol_raw = str(payload.get("symbol", "")).strip()
    signal = _normalize_signal(str(payload.get("signal", "")))
    if not symbol_raw or signal not in ("buy", "sell", "close"):
        raise ApiError(400, "err.webhook_fields", "فیلدهای symbol و signal (buy/sell/close) الزامی هستند.")

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
        raise ApiError(404, "err.account_not_found", "حساب پیدا نشد")
    if user.get("role") != "admin" and account.get("owner_id") != user["id"]:
        raise ApiError(403, "err.account_not_yours", "این حساب متعلق به شما نیست")
    return account


def _assert_exchange_allowed(exchange: str, request: Request, user: dict,
                             current: str | None = None, allow_owned: bool = False):
    """بعضی صرافی‌ها فقط برای یک زبان نمایش داده می‌شوند (تبدیل → فقط فارسی).

    این چک سمت سرور است چون پنهان‌کردن یک <option> در HTML جلوی درخواست مستقیم
    به API را نمی‌گیرد. زبان از همان مسیری خوانده می‌شود که صفحه با آن رندر شده
    (i18n.resolve) تا چیزی که کاربر می‌بیند و چیزی که سرور می‌پذیرد یکی باشد.

    دو استثنا، تا کاربری که از قبل حساب تبدیل دارد با عوض‌کردن زبان قفل نشود:
    - ``current``: صرافی فعلیِ همان حسابی که دارد ویرایش می‌شود (تغییری نمی‌دهد).
    - ``allow_owned``: مسیرهای فقط-خواندنی مثل لیست نمادها، اگر کاربر دست‌کم یک
      حساب روی این صرافی داشته باشد.

    هیچ‌کدام برای *ساختن* حساب تازه فعال نیستند؛ آن‌جا محدودیت زبان قطعی است.
    """
    if current is not None and exchange == current:
        return
    if i18n.exchange_allowed(exchange, i18n.resolve(request, user)):
        return
    if allow_owned and any(a.get("exchange") == exchange
                           for a in config_store.list_accounts(user["id"])):
        return
    raise ApiError(
        403, "err.exchange_lang_restricted",
        "این صرافی فقط برای کاربران فارسی‌زبان در دسترس است.",
        exchange=exchange,
    )


def _assert_can_go_live(user: dict, otp: str | None = None):
    """دو شرط برای رفتن به معامله‌ی واقعی:

    ۱) توکن فعال‌سازی (ادمین از این یکی معاف است — همیشه بوده).
    ۲) ورود دو مرحله‌ای فعال + یک کد معتبر همین لحظه. این یکی *شامل ادمین
       هم می‌شود*: کل هدف این است که اگر سشن کسی دزدیده شد، مهاجم نتواند
       پول واقعی را وارد معامله کند، و حساب ادمین جذاب‌ترین هدف است.
    """
    if user.get("role") != "admin" and not tokens.has_active_token(user["id"]):
        raise ApiError(
            403, "err.live_needs_token",
            "برای معامله‌ی واقعی (live) نیاز به توکن فعال‌سازی دارید — "
            "از صفحه‌ی پشتیبانی (واحد مالی) درخواست خرید توکن کنید.",
        )

    if not user.get("totp_enabled"):
        raise ApiError(
            403, "err.live_needs_2fa",
            "برای فعال‌کردن معامله‌ی واقعی باید ابتدا ورود دو مرحله‌ای را "
            "از صفحه‌ی تنظیمات فعال کنید.",
        )

    code = (otp or "").strip()
    if not code:
        raise ApiError(403, "err.otp_required", "کد ورود دو مرحله‌ای را وارد کنید.")
    # کد بازیابی هم پذیرفته می‌شود تا کاربری که گوشی‌اش را گم کرده قفل نشود
    if twofa.verify(user.get("totp_secret") or "", code):
        return
    if users.consume_recovery_code(user["id"], twofa.normalize_recovery(code)):
        return
    raise ApiError(403, "err.otp_invalid", "کد ورود دو مرحله‌ای درست نیست.")


# ---------- پشتیبان‌گیری و بازیابی ----------
class RestoreIn(BaseModel):
    payload: dict
    mode: str = "new"


@app.get("/api/backup")
async def download_backup(user: dict = Depends(auth.require_user)):
    """فایل پشتیبان تنظیمات و نمادهای حساب‌های همین کاربر. کلید API و توکن
    وبهوک عمداً داخلش نیست — توضیحش در app/core/backup.py آمده."""
    data = backup.export_for_owner(user["id"], user.get("username", ""))
    stamp = data["exported_at"][:10]
    filename = f"cpulsepro-backup-{stamp}.json"
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/backup/restore")
async def restore_backup(body: RestoreIn, user: dict = Depends(auth.require_user)):
    try:
        result = backup.restore_for_owner(user["id"], body.payload, body.mode)
    except ValueError as e:
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
    # حساب‌های تازه هنوز در موتور نیستند؛ بدون این، کاربر باید سرور را
    # ری‌استارت کند تا ربات آن‌ها را ببیند.
    bot_manager.sync_from_config()
    return result


@app.get("/api/accounts")
async def get_accounts(user: dict = Depends(auth.require_user)):
    # فهرست حساب‌های «خودِ» کاربر — برای ادمین هم همین‌طور. حساب‌های سایر کاربران
    # فقط از مسیر /api/admin/user-accounts و تفکیک‌شده در دسترس‌اند.
    return config_store.list_accounts(user["id"])


@app.post("/api/accounts")
async def create_account(payload: AccountIn, request: Request,
                         user: dict = Depends(auth.require_user)):
    _assert_exchange_allowed(payload.exchange, request, user)
    if payload.trading_mode == "live":
        _assert_can_go_live(user, payload.otp)
    account = config_store.add_account(payload.dict(exclude={"otp"}), owner_id=user["id"])
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
async def edit_account(account_id: str, payload: AccountIn, request: Request,
                       user: dict = Depends(auth.require_user)):
    existing = _owned_account(account_id, user)
    _assert_exchange_allowed(payload.exchange, request, user, existing.get("exchange"))
    if payload.trading_mode == "live":
        _assert_can_go_live(user, payload.otp)
    try:
        account = config_store.update_account(account_id, payload.dict(exclude={"otp"}))
    except KeyError as e:
        raise HTTPException(404, str(e))
    bot_manager.sync_from_config()
    return account


@app.post("/api/accounts/{account_id}/trading-mode")
async def set_trading_mode(account_id: str, mode: str, otp: str = "",
                           user: dict = Depends(auth.require_user)):
    """سوییچ paper/live از داشبورد. برای اعمال، حساب باید متوقف و دوباره شروع شود."""
    _owned_account(account_id, user)
    if mode not in ("paper", "live"):
        raise ApiError(400, "err.mode_paper_live", "حالت باید paper یا live باشد")
    if mode == "live":
        _assert_can_go_live(user, otp)
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
    # حساب که رفت، تاریخچه‌اش دیگر در هیچ گزارشی دیده نمی‌شود و فقط فایل را
    # بزرگ می‌کند؛ پس همراه خودش برداشته می‌شود.
    history.reset_account(account_id)
    config_store.delete_account(account_id)
    bot_manager.sync_from_config()
    return {"deleted": account_id}


async def _diagnose_copy_trading(driver) -> dict:
    """بررسی می‌کند کلید این حساب واقعاً از نوع کپی‌ترید است یا نه، و چه چیزی
    با آن در دسترس است.

    ثبت سفارش روی حساب کپی‌ترید با همین اندپوینت‌های معمولی فیوچرز انجام
    می‌شود (توبیت اندپوینت جداگانه‌ای برای لیدر منتشر نکرده) و روی حساب
    واقعی آزموده و تأیید شده است: کلیدِ نوع COPY_TRADING سفارش را روی حساب
    کپی‌ترید می‌نشاند. پس آنچه واقعاً می‌تواند خراب باشد، نوع کلید است — و
    این تست بدون باز کردن هیچ معامله‌ای همان را می‌سنجد.
    """
    from app.core.exchanges.base import ExchangeError

    out = {"is_copy_trading_key": False, "key_error": None,
           "symbols": [], "followers": None, "profit_rate": None}
    try:
        config = await driver.get_leader_config()
        out["is_copy_trading_key"] = True
        rate = config.get("profitRate")
        if rate is not None:
            try:
                out["profit_rate"] = float(rate) * 100
            except (TypeError, ValueError):
                pass
    except ExchangeError as e:
        out["key_error"] = str(e)
        return out                     # بقیه‌ی تست‌ها بی‌معنا می‌شوند

    try:
        out["symbols"] = await driver.get_leader_symbols()
    except ExchangeError as e:
        out["symbols_error"] = str(e)
    try:
        # total از خود صرافی، نه طول لیست: پاسخ صفحه‌بندی‌شده است
        out["followers"] = (await driver.get_leader_followers())["total"]
    except ExchangeError as e:
        out["followers_error"] = str(e)
    return out


@app.post("/api/accounts/{account_id}/test-connection")
async def test_connection(account_id: str, user: dict = Depends(auth.require_user)):
    cfg = _owned_account(account_id, user)

    from app.core.exchanges.factory import build_driver
    from app.core.exchanges.base import ExchangeError
    # برای تست همیشه درایور واقعی ساخته می‌شود (نه شبیه‌ساز paper) تا کلیدهای API واقعاً چک شوند
    driver = build_driver("live", cfg)
    is_copy = (cfg.get("account_type") == "copy_trading"
               and cfg.get("exchange", "toobit") == "toobit")
    try:
        await driver.connect()
        info = await driver.get_account_info()
        message = (f"اتصال موفق. موجودی فیوچرز: {info.get('balance', 0):.2f} "
                   f"{info.get('currency', 'USDT')}")
        result = {"ok": True, "message": message, "account": info}

        if is_copy:
            diag = await _diagnose_copy_trading(driver)
            result["copy_trading"] = diag
            if not diag["is_copy_trading_key"]:
                # موجودی خوانده شد ولی کلید از نوع کپی‌ترید نیست: یعنی این کلید
                # روی حساب فیوچرز کار می‌کند، نه حساب کپی‌ترید. اگر این را
                # نگوییم، کاربر فکر می‌کند همه‌چیز درست است و معامله‌ها روی
                # حساب اشتباه باز می‌شوند.
                result["ok"] = False
                result["message"] = (
                    "این کلید از نوع «کپی‌ترید» نیست — در اپ توبیت از تب "
                    "«API کپی‌ترید» یک کلید جدید بسازید. "
                    f"(پاسخ صرافی: {diag['key_error']})"
                )
        await driver.close()
        return result
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
        raise ApiError(400, "err.start_failed", f"شروع ربات ناموفق: {e}", msg=str(e))
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/accounts/{account_id}/stop")
async def stop_account(account_id: str, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    await bot_manager.stop_account(account_id)
    return (await bot_manager.get_status())["accounts"].get(account_id)


@app.post("/api/start-all")
async def start_all(user: dict = Depends(auth.require_user)):
    await bot_manager.start_all(user["id"])
    return await bot_manager.get_status(user["id"])


@app.post("/api/stop-all")
async def stop_all(user: dict = Depends(auth.require_user)):
    await bot_manager.stop_all(user["id"])
    return await bot_manager.get_status(user["id"])


# ---------- بستن دستی پوزیشن ----------
@app.post("/api/accounts/{account_id}/close-position")
async def close_position(account_id: str, payload: dict, user: dict = Depends(auth.require_user)):
    _owned_account(account_id, user)
    result = await bot_manager.close_position_manual(account_id, payload)
    if not result.get("ok"):
        raise ApiError(400, "err.close_failed", result.get("detail", "بستن پوزیشن ناموفق بود"))
    return result


# ---------- گزارش‌گیری و سود/زیان ----------
@app.get("/api/accounts/{account_id}/report")
async def account_report(account_id: str, days: int = 30, mode: str | None = None,
                         user: dict = Depends(auth.require_user)):
    account = _owned_account(account_id, user)
    if mode not in (None, "paper", "live"):
        raise ApiError(400, "err.mode_paper_live", "mode باید paper یا live باشد")
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


@app.get("/api/reports/combined")
async def combined_report(days: int = 30, user: dict = Depends(auth.require_user)):
    """گزارش مجموع عملکرد همه‌ی حساب‌های واقعی (live) این کاربر.

    فیلتر روی *رکوردهای تاریخچه* اعمال می‌شود نه روی حالت فعلی حساب: اگر
    حسابی امروز کاغذی باشد ولی قبلاً واقعی بوده، معامله‌های واقعی گذشته‌اش
    پول واقعی بوده‌اند و باید در این گزارش بیایند. برعکسش هم درست است —
    معامله‌های کاغذیِ یک حساب واقعی این‌جا شمرده نمی‌شوند.
    """
    accounts = config_store.list_accounts(user["id"])
    if not accounts:
        return {"accounts": [], "summary": None, "equity_curve": [], "daily": [],
                "by_symbol": [], "trades": [], "account_count": 0}

    ids = [a["id"] for a in accounts]
    report = history.get_report(ids, days=days, mode="live")

    # سود/زیان باز و اکوییتی لحظه‌ای فقط از حساب‌هایی که همین حالا واقعی و
    # در حال اجرا هستند می‌آید؛ حساب کاغذی اکوییتی واقعی ندارد.
    names = {a["id"]: a for a in accounts}
    unrealized = 0.0
    open_positions = 0
    total_equity = total_balance = 0.0
    live_running = 0
    for aid in ids:
        acc = names[aid]
        if acc.get("trading_mode") != "live":
            continue
        runner = bot_manager.runners.get(aid)
        if runner is None:
            continue
        live_running += 1
        unrealized += sum(float(p.get("profit", 0) or 0) for p in (runner.positions or []))
        open_positions += len(runner.positions or [])
        info = runner.account_info or {}
        total_equity += float(info.get("equity", 0) or 0)
        total_balance += float(info.get("balance", 0) or 0)

    summary = report["summary"]
    summary["unrealized_pnl"] = unrealized
    summary["open_positions"] = open_positions
    summary["total_pnl_with_open"] = summary["total_pnl"] + unrealized
    summary["total_equity"] = total_equity
    summary["total_balance"] = total_balance
    summary["live_running"] = live_running

    # نام و حالت هر حساب به سطرهای سهم‌بندی اضافه می‌شود تا فرانت‌اند لازم
    # نباشد دوباره لیست حساب‌ها را بگیرد و خودش join کند.
    rows = []
    for row in (report.get("accounts") or []):
        acc = names.get(row["account_id"], {})
        rows.append({**row,
                     "name": acc.get("name", "—"),
                     "trading_mode": acc.get("trading_mode", "paper")})
    # حساب‌هایی که در این بازه هیچ معامله‌ی واقعی نداشته‌اند حذف می‌شوند تا
    # جدول با ردیف‌های صفر شلوغ نشود.
    report["accounts"] = [r for r in rows if r["trades"]]
    report["account_count"] = len(report["accounts"])
    report["account_info"] = None
    report["account_stats"] = None
    return report


@app.get("/api/version")
async def get_version():
    """نسخه‌ی در حال اجرا — عمداً بدون نیاز به لاگین.

    کل فایده‌اش این است که بعد از هر به‌روزرسانی، با یک باز کردن آدرس در
    مرورگر (حتی روی موبایل و بدون ورود) معلوم شود سرور روی چه کدی است. اگر
    پشت لاگین بود، دقیقاً در همان موقعیتی که بیشترین کاربرد را دارد —
    وقتی معلوم نیست چه چیزی درست کار نمی‌کند — قابل استفاده نبود.
    فقط شناسه‌ی کامیت و زمان استقرار برمی‌گردد، نه چیزی از پیکربندی.
    """
    return app_version.INFO


@app.get("/api/accounts/{account_id}/copy-report")
async def copy_trading_report(account_id: str, days: int = 30,
                              user: dict = Depends(auth.require_user)):
    """بخش‌های مخصوص حساب کپی‌ترید، جدا از گزارش معاملات.

    چرا جدا: یک حساب لیدر دو منبع درآمد دارد — سود معاملات خودش، و سهمی که
    از سود دنبال‌کننده‌ها می‌گیرد. دومی در سود/زیان معاملات اصلاً دیده
    نمی‌شود، پس گزارش فیوچرز به‌تنهایی تصویر کاملی از یک حساب کپی‌ترید
    نمی‌دهد. ضمناً معیارهایی مثل شارپ و سرمایه‌ی تحت مدیریت را خودمان
    نمی‌توانیم حساب کنیم و فقط صرافی دارد.

    اندپوینت جداست تا اگر این فراخوانی‌ها شکست خوردند، گزارش اصلی سالم بماند.
    """
    account = _owned_account(account_id, user)
    if account.get("account_type") != "copy_trading" or account.get("exchange", "toobit") != "toobit":
        raise ApiError(400, "err.not_copy_account", "این حساب از نوع کپی‌ترید نیست")

    from app.core.exchanges.factory import build_driver
    from app.core.exchanges.base import ExchangeError

    driver = build_driver("live", account)
    out = {"trade_data": None, "followers": None, "profit_sharing": None, "errors": {}}
    try:
        await driver.connect_public()
        # هر بخش جدا خطا می‌گیرد: نبودن یکی نباید بقیه را از بین ببرد
        try:
            out["trade_data"] = await driver.get_leader_trade_data(days or 365)
        except ExchangeError as e:
            out["errors"]["trade_data"] = str(e)
        try:
            out["followers"] = await driver.get_leader_followers(size=50)
        except ExchangeError as e:
            out["errors"]["followers"] = str(e)
        try:
            rows = await driver.get_leader_profit_sharings()
            cutoff = None
            if days and days > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            in_range = [r for r in rows if not cutoff or str(r["date"]) >= cutoff]
            out["profit_sharing"] = {
                "rows": in_range[:52],
                "total_net": sum(r["net"] or 0 for r in in_range),
                "total_gross": sum(r["total"] or 0 for r in in_range),
                "total_referral": sum(r["referral_share"] or 0 for r in in_range),
            }
        except ExchangeError as e:
            out["errors"]["profit_sharing"] = str(e)
    except ExchangeError as e:
        out["errors"]["connect"] = str(e)
    finally:
        await driver.close()
    return out


@app.post("/api/accounts/{account_id}/report/reset")
async def reset_account_report(account_id: str, user: dict = Depends(auth.require_user)):
    """پاکسازی و ریست کامل گزارش‌های یک حساب — از این لحظه مثل حساب خام ثبت می‌شود."""
    _owned_account(account_id, user)
    return history.reset_account(account_id)


def _public_base(request: Request) -> str:
    """آدرس پایه‌ای که کاربر با آن به سرویس وصل است — برای ساختن لینک وبهوک.

    اگر PUBLIC_BASE_URL در .env تنظیم شده باشد (نصب پشت دامنه) همان برمی‌گردد.
    وگرنه از خود درخواست ساخته می‌شود؛ در این حالت X-Forwarded-Proto در نظر
    گرفته می‌شود، چون پشت nginx خودِ درخواست همیشه http است و بدون این، آدرس
    وبهوک http درمی‌آمد و TradingView به ریدایرکت https می‌خورد.
    """
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL
    host = request.headers.get("host")
    if not host:
        return f"http://YOUR-SERVER-IP:{settings.DASHBOARD_PORT}"
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    proto = proto.split(",")[0].strip() or "http"
    return f"{proto}://{host}"


def _webhook_url(request: Request, account_id: str) -> str:
    return f"{_public_base(request)}/webhook/tradingview/{account_id}"


# ---------- وبهوک اختصاصی هر حساب ----------
@app.get("/api/accounts/{account_id}/webhook-info")
async def account_webhook_info(account_id: str, request: Request, user: dict = Depends(auth.require_user)):
    """آدرس Webhook اختصاصی و نمونه‌ی پیام Alert در TradingView برای همین حساب."""
    account = _owned_account(account_id, user)
    url = _webhook_url(request, account_id)
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
    return {"token": new_token, "url": _webhook_url(request, account_id)}


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
        raise ApiError(400, _VALUE_ERROR_KEYS.get(str(e), ""), str(e))
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
        raise ApiError(404, "err.account_or_symbol_not_found", "حساب یا نماد پیدا نشد")
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
