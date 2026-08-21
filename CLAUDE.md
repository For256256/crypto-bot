# راهنمای کار روی این پروژه

## ارتباط با کاربر

- توضیحات **فقط به فارسی** نوشته شوند.
- **بعد از هر تغییری که مرج می‌شود، دستور نصب/آپدیت از گیت‌هاب فرستاده شود:**

  ```
  curl -fsSL https://raw.githubusercontent.com/for256256/crypto-bot/main/install-crypto-bot.sh | sudo bash
  ```

  اگر خطای ۴۲۹ (محدودیت نرخ raw.githubusercontent) گرفت، جایگزین‌ها:

  ```
  curl -fsSL https://github.com/for256256/crypto-bot/raw/main/install-crypto-bot.sh | sudo bash
  sudo bash /opt/crypto-bot/install-crypto-bot.sh
  ```

  وقتی ریپازیتوری خصوصی شد، بار اول `GITHUB_TOKEN=…` هم لازم است؛ بعد از آن
  در `.env` ذخیره می‌شود و دستور همیشگی بدون توکن کار می‌کند.

- اگر تغییر روی تمپلیت بود، یادآوری شود که یک بار `Ctrl+Shift+R` بزنند
  (وگرنه نسخه‌ی کش‌شده اجرا می‌شود).

## روال توسعه

- کار روی برنچ `claude/crypto-bot-setup-oz8lbp`، بعد PR و مرج.
- قبل از مرج: `py_compile`، اسکریپت `jscheck` روی تمپلیت‌ها، برابری کلیدهای
  i18n در هر پنج زبان، و تست واقعی (سرور محلی + مرورگر) نه فقط بازبینی کد.
- ترجمه‌های روسی، چینی و ترکی کار مدل است نه گویشور بومی — همیشه یادآوری شود.
