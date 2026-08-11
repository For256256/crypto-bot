"""
چندزبانگی (i18n) — فارسی، انگلیسی، روسی، چینی.

کاتالوگ‌ها فایل‌های JSON مسطح با کلیدهای نقطه‌دار در app/i18n/<lang>.json هستند.
زنجیره‌ی fallback: زبان انتخابی → انگلیسی → خود کلید. یعنی اگر ترجمه‌ای هنوز
اضافه نشده باشد صفحه نمی‌شکند، فقط همان رشته‌ی انگلیسی/کلید نمایش داده می‌شود.

اولویت تشخیص زبان: انتخاب ذخیره‌شده‌ی کاربر (برای کاربر لاگین‌شده) →
کوکی (برای مهمان‌ها، مثلاً در صفحه‌ی اول/ورود) → هدر Accept-Language → پیش‌فرض.
"""
import json
import os
import threading

I18N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n")

DEFAULT_LANG = "fa"
COOKIE_NAME = "cb_lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# native_name عمداً به خط خود همان زبان است تا کاربر بتواند زبانش را پیدا کند
# حتی وقتی رابط فعلاً به زبانی است که نمی‌فهمد.
LANGUAGES = {
    "fa": {"native_name": "فارسی",   "english_name": "Persian", "dir": "rtl", "flag": "🇮🇷", "date_locale": "fa-IR"},
    "en": {"native_name": "English",  "english_name": "English", "dir": "ltr", "flag": "🇬🇧", "date_locale": "en-US"},
    "ru": {"native_name": "Русский",  "english_name": "Russian", "dir": "ltr", "flag": "🇷🇺", "date_locale": "ru-RU"},
    "zh": {"native_name": "中文",      "english_name": "Chinese", "dir": "ltr", "flag": "🇨🇳", "date_locale": "zh-CN"},
}

_cache: dict[str, dict] = {}
_lock = threading.Lock()


def is_supported(lang: str | None) -> bool:
    return bool(lang) and lang in LANGUAGES


def normalize(lang: str | None) -> str:
    """'en-US' → 'en'؛ اگر پشتیبانی نشود، پیش‌فرض برمی‌گردد."""
    if not lang:
        return DEFAULT_LANG
    base = str(lang).strip().lower().replace("_", "-").split("-")[0]
    return base if base in LANGUAGES else DEFAULT_LANG


def get_catalog(lang: str) -> dict:
    lang = lang if lang in LANGUAGES else DEFAULT_LANG
    with _lock:
        if lang in _cache:
            return _cache[lang]
    path = os.path.join(I18N_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    with _lock:
        _cache[lang] = data
    return data


def reload_catalogs():
    """پاک‌کردن کش — برای توسعه/تست، تا ویرایش فایل JSON بدون ری‌استارت دیده شود."""
    with _lock:
        _cache.clear()


def translate(lang: str, key: str, **params) -> str:
    """ترجمه‌ی یک کلید. پارامترها با str.format جایگذاری می‌شوند؛ اگر ترجمه
    پارامتر ناقصی داشته باشد، به‌جای خطا همان متن خام برگردانده می‌شود."""
    lang = lang if lang in LANGUAGES else DEFAULT_LANG
    text = get_catalog(lang).get(key)
    if text is None and lang != "en":
        text = get_catalog("en").get(key)
    if text is None:
        text = key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def _from_accept_language(header: str | None) -> str | None:
    """ساده‌ترین پارس ممکن از Accept-Language: اولین زبانِ پشتیبانی‌شده به ترتیب q."""
    if not header:
        return None
    items = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        code, _, q = part.partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 1.0
        items.append((weight, code.strip()))
    for _, code in sorted(items, key=lambda t: -t[0]):
        base = code.lower().replace("_", "-").split("-")[0]
        if base in LANGUAGES:
            return base
    return None


def resolve(request, user: dict | None = None) -> str:
    """زبان مؤثر این درخواست."""
    if user and is_supported(user.get("lang")):
        return user["lang"]
    cookie = request.cookies.get(COOKIE_NAME)
    if is_supported(cookie):
        return cookie
    from_header = _from_accept_language(request.headers.get("accept-language"))
    if from_header:
        return from_header
    return DEFAULT_LANG


def language_options() -> list:
    """برای رندر انتخابگر زبان."""
    return [{"code": code, **meta} for code, meta in LANGUAGES.items()]
