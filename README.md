# کریپتو بات — Toobit / Tabdeal Futures

داشبورد + ربات معاملاتی فیوچرز با اتصال API به **توبیت** یا **تبدیل**:

- **۴ استراتژی داخلی**: SuperTrend+EMA+RSI · MACD+EMA روند · برگشت بولینگر+RSI · روبان MA+Kijun+PSAR
  (روی هر دو صرافی کار می‌کنند)
- **وبهوک TradingView** برای دریافت سیگنال خارجی (`/webhook/tradingview` با توکن امن) — روی هر دو صرافی کار می‌کند
- **حالت paper** (شبیه‌سازی با قیمت واقعی، با کارمزد و مارجین واقعی) به‌صورت پیش‌فرض + سوییچ دستی به live
- **مدیریت ریسک**: ٪ریسک هر معامله، SL/TP خودکار از ATR (فاصله‌ی قابل‌تنظیم و یکسان برای هر دو)، سقف ضرر روزانه، recycle
- **گزارش سود/زیان**: منحنی اکوییتی، PnL روزانه (خالص از کارمزد)، تاریخچه معاملات
- داشبورد روی پورت **8891**

> ℹ️ **صرافی تبدیل (Tabdeal):** API این صرافی اندپوینت کندل تاریخی یا ticker
> ندارد (فقط اردربوک)، پس کندل/قیمت مرجعِ استراتژی‌های داخلی برای حساب‌های
> تبدیل از API عمومی **توبیت** گرفته می‌شود؛ ثبت سفارش، پوزیشن، SL/TP و
> موجودی همیشه واقعاً روی خود **تبدیل** انجام می‌شود (سیگنال از توبیت،
> اجرا روی تبدیل). وبهوک TradingView هم مثل قبل روی هر دو صرافی کار می‌کند.

## نصب/به‌روزرسانی یک‌خطی روی سرور اوبونتو

بدون نیاز به دانلود یا آپلود دستی فایل — همین یک دستور را در سرور کپی‌پیست کنید،
پروژه به‌صورت خودکار دانلود و نصب می‌شود (و اگر قبلاً نصب شده باشد، فقط کد را
به‌روزرسانی و سرویس را ریستارت می‌کند):

```bash
curl -fsSL https://raw.githubusercontent.com/for256256/crypto-bot/main/install-crypto-bot.sh | sudo bash
```

اسکریپت نصب‌کننده، کد را در `/opt/crypto-bot` کلون می‌کند، venv می‌سازد، پکیج‌ها را
نصب می‌کند، در نصب اولیه `.env` با رمز و توکن تصادفی تولید می‌کند و سرویس systemd
با نام `crypto-bot` را راه‌اندازی/ریستارت می‌کند.

اگر مخزن را از قبل کلون کرده‌اید، همان اسکریپت داخل ریشه‌ی پروژه هم قابل اجراست:

```bash
sudo bash install-crypto-bot.sh
```

## اجرای دستی (توسعه)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # رمز و توکن را تنظیم کنید
uvicorn app.main:app --host 0.0.0.0 --port 8891
```

## ساختار

```
app/
  main.py                  API و داشبورد (FastAPI)
  config.py                تنظیمات از .env
  core/
    engine.py              حلقه‌ی معاملاتی هر حساب + مدیریت ریسک + وبهوک
    config_store.py        ذخیره‌ی حساب‌ها/نمادها (config/accounts.json)
    history.py             تاریخچه‌ی معاملات و اکوییتی (config/history.json)
    exchanges/
      base.py              رابط مشترک درایور صرافی
      toobit.py            درایور Toobit (امضا، کندل، سفارش، TP/SL)
      paper.py             شبیه‌ساز با قیمت واقعی
      factory.py           ساخت درایور per-account (paper/live)
    strategies/
      indicators.py        EMA/RSI/ATR/SuperTrend/MACD/Bollinger/Kijun/PSAR
      registry.py          ۴ استراتژی + schema پارامترها برای داشبورد
  templates/dashboard.html داشبورد (RTL، تیره، فارسی)
```

## وبهوک TradingView

آدرس و نمونه پیام دقیق از دکمه‌ی «📡 وبهوک TradingView» در داشبورد یا endpoint
`/api/webhook-info` در دسترس است. فرمت نماد هرچه باشد (BTCUSDT، BTCUSDT.P، …)
به فرمت پرپچوال Toobit (`BTC-SWAP-USDT`) تبدیل می‌شود.

> ⚠️ همه‌ی حساب‌ها پیش‌فرض در حالت **paper** ساخته می‌شوند. قبل از سوییچ به
> **LIVE** حتماً با paper تست کنید.
