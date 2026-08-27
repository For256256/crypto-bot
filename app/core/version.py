"""
نسخه‌ی در حال اجرا — برای اینکه با یک نگاه معلوم باشد سرور و مرورگر روی چه
کدی هستند.

چرا لازم شد: بعد از هر به‌روزرسانی، «تغییرات را نمی‌بینم» می‌توانست سه چیز
باشد — کد قدیمی روی سرور، سرویس ری‌استارت‌نشده، یا کش مرورگر. بدون یک شماره‌ی
نسخه‌ی قابل مشاهده، تشخیصشان از هم فقط حدس بود.

شناسه از خودِ فایل‌های .git خوانده می‌شود، نه با اجرای دستور git: اجرای
subprocess هم به نصب‌بودن git وابسته است و هم روی نصب‌هایی که مالک پوشه با
کاربر سرویس فرق دارد به خطای «dubious ownership» می‌خورد. خواندن مستقیم فایل
هیچ‌کدام از این دو مشکل را ندارد.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GIT_DIR = os.path.join(_ROOT, ".git")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _resolve_head() -> tuple[str, str]:
    """(شناسه‌ی کامیت، نام برنچ) — رشته‌ی خالی یعنی پیدا نشد."""
    head = _read(os.path.join(_GIT_DIR, "HEAD"))
    if not head:
        return "", ""
    if not head.startswith("ref:"):
        return head, ""                      # حالت detached
    ref = head[4:].strip()
    # فقط پیشوند refs/heads/ برداشته می‌شود، نه هر چیزی تا آخرین اسلش:
    # نام برنچ‌های این پروژه خودشان اسلش دارند (claude/…).
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    # حالت عادی: فایل ref مستقیم
    sha = _read(os.path.join(_GIT_DIR, *ref.split("/")))
    if sha:
        return sha, branch

    # حالت فشرده: کلون کم‌عمق نصب معمولاً ref را در packed-refs می‌گذارد
    for line in _read(os.path.join(_GIT_DIR, "packed-refs")).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1] == ref:
            return parts[0], branch
    return "", branch


def _deployed_at() -> str:
    """زمان آخرین به‌روزرسانی — از تاریخ تغییر HEAD.

    عمداً تاریخ خودِ کامیت نیست: چیزی که این‌جا مفید است «کِی این سرور
    آپدیت شد» است، نه «کِی کد نوشته شد».
    """
    for name in ("HEAD", "FETCH_HEAD"):
        path = os.path.join(_GIT_DIR, name)
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)\
                           .isoformat(timespec="seconds")
        except OSError:
            continue
    return ""


def _build() -> dict:
    sha, branch = _resolve_head()
    return {
        "commit": sha[:7] if sha else "unknown",
        "commit_full": sha,
        "branch": branch,
        "deployed_at": _deployed_at(),
    }


# یک‌بار موقع بالا آمدن سرویس خوانده می‌شود؛ تا ری‌استارت بعدی عوض نمی‌شود و
# ری‌استارت دقیقاً همان کاری است که آپدیت انجام می‌دهد.
INFO = _build()
