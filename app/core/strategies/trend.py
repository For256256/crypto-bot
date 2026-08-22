"""
تشخیص «روند کلی بازار» در یک تایم‌فریم مستقل (معمولاً بالاتر از تایم‌فریم
معامله) — برای فیلتر هم‌جهتی: سیگنالی که خلاف روند تایم‌فریم بالاست اجرا نشود.

این ماژول عمداً از خود استراتژی‌ها جداست: استراتژی روی تایم‌فریم معامله سیگنال
می‌سازد، این‌جا فقط جهت کلی بازار تعیین می‌شود. هم موتور زنده و هم بک‌تست از
همین توابع استفاده می‌کنند تا نتیجه‌ی بک‌تست با رفتار واقعی ربات یکی باشد.

جهت‌ها: up (صعودی)، down (نزولی)، neutral (بدون جهت روشن)، unknown (داده کافی
نیست). هر دو حالت آخر یعنی «تأیید نشد» و ورود جدید بلاک می‌شود.
"""
import numpy as np
import pandas as pd

from app.core.strategies import indicators as ind

# روش‌های مجاز تشخیص روند
METHODS = ("ema", "supertrend", "both")
DEFAULT_METHOD = "ema"
DEFAULT_EMA_LENGTH = 200
DEFAULT_TIMEFRAME = "4h"

# پارامترهای سوپرترند فیلتر روند — عمداً ثابت‌اند و از پارامترهای استراتژی
# جدا. این‌جا دنبال نقطه‌ی ورود نیستیم، فقط جهت کلی را می‌خواهیم.
_ST_LENGTH = 10
_ST_MULT = 3.0

# کمینه‌ی کندل لازم برای اینکه اصلاً حرفی درباره‌ی روند بزنیم
MIN_CANDLES = 50


def _normalize_method(method: str) -> str:
    method = str(method or DEFAULT_METHOD).strip().lower()
    return method if method in METHODS else DEFAULT_METHOD


def _warmup_bars(df_len: int, method: str, ema_length: int) -> int:
    """چند کندل اول باید unknown بماند.

    در حالت ایده‌آل به اندازه‌ی طول EMA کندل گرم‌کننده داریم. اگر صرافی آن‌قدر
    تاریخچه نداشت (مثلاً EMA۲۰۰ روی تایم‌فریم روزانه)، به‌جای اینکه کل سری
    unknown شود و ربات هیچ معامله‌ای نکند، warmup کوتاه می‌شود و در عوض
    فراخوان با `insufficient_history` خبردار می‌شود.
    """
    if method == "supertrend":
        return min(_ST_LENGTH * 3, max(df_len - 20, 0))
    return min(ema_length, max(df_len - 30, 0))


def trend_series(df: pd.DataFrame, method: str = DEFAULT_METHOD,
                 ema_length: int = DEFAULT_EMA_LENGTH) -> pd.Series:
    """جهت روند برای هر کندل — سریِ رشته‌ای هم‌طول df.

    برای بک‌تست لازم است: آن‌جا باید بدانیم در لحظه‌ی هر سیگنال، روند چه بوده.
    """
    method = _normalize_method(method)
    ema_length = max(int(ema_length or DEFAULT_EMA_LENGTH), 2)
    n = len(df)
    if df is None or n == 0:
        return pd.Series([], dtype=object)

    close = df["close"]
    if method in ("ema", "both"):
        ema_v = ind.ema(close, ema_length)
        ema_dir = np.where(close.values > ema_v.values, "up", "down")
    if method in ("supertrend", "both"):
        st = ind.supertrend(df, _ST_LENGTH, _ST_MULT)
        st_dir = np.where(st["direction"].values > 0, "up", "down")

    if method == "ema":
        out = ema_dir
    elif method == "supertrend":
        out = st_dir
    else:
        # هر دو باید موافق باشند؛ اختلاف یعنی بازار جهت روشنی ندارد
        out = np.where(ema_dir == st_dir, ema_dir, "neutral")

    out = pd.Series(out, index=df.index, dtype=object)
    warm = _warmup_bars(n, method, ema_length)
    if warm > 0:
        out.iloc[:warm] = "unknown"
    return out


def detect_trend(df: pd.DataFrame, method: str = DEFAULT_METHOD,
                 ema_length: int = DEFAULT_EMA_LENGTH) -> dict:
    """جهت روند روی آخرین کندل + مقادیری که تصمیم از آن‌ها آمده.

    خروجی: {"direction", "close", "ema", "supertrend", "insufficient_history"}
    """
    method = _normalize_method(method)
    ema_length = max(int(ema_length or DEFAULT_EMA_LENGTH), 2)
    empty = {"direction": "unknown", "method": method, "ema_length": ema_length,
             "close": None, "ema": None, "supertrend": None,
             "insufficient_history": True}
    if df is None or len(df) < MIN_CANDLES:
        return empty

    series = trend_series(df, method, ema_length)
    direction = str(series.iat[-1])

    ema_val = st_val = None
    if method in ("ema", "both"):
        v = ind.ema(df["close"], ema_length).iat[-1]
        ema_val = float(v) if pd.notna(v) else None
    if method in ("supertrend", "both"):
        v = ind.supertrend(df, _ST_LENGTH, _ST_MULT)["supertrend"].iat[-1]
        st_val = float(v) if pd.notna(v) else None

    needed = _ST_LENGTH * 3 if method == "supertrend" else ema_length
    return {
        "direction": direction,
        "method": method,
        "ema_length": ema_length,
        "close": float(df["close"].iat[-1]),
        "ema": ema_val,
        "supertrend": st_val,
        # true یعنی جهت داده شده ولی روی تاریخچه‌ی کوتاه‌تر از دوره‌ی
        # اندیکاتور حساب شده و باید با احتیاط نگاهش کرد
        "insufficient_history": len(df) < needed,
    }


def side_matches(direction: str, side: str) -> bool:
    """آیا جهت معامله (buy/sell) با روند هم‌سوست؟

    neutral و unknown عمداً False برمی‌گردانند: وقتی روند تأیید نشده، «هم‌جهت
    بودن» هم تأیید نشده است.
    """
    if side == "buy":
        return direction == "up"
    if side == "sell":
        return direction == "down"
    return False
