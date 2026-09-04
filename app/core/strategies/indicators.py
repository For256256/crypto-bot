"""
اندیکاتورهای تکنیکال مشترک استراتژی‌ها — همه روی DataFrame کندل
(ستون‌های open/high/low/close/volume) کار می‌کنند و pd.Series برمی‌گردانند.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def supertrend(df: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """خروجی: ستون supertrend (مقدار خط) و direction (1=صعودی، -1=نزولی)."""
    hl2 = (df["high"] + df["low"]) / 2
    atr_v = atr(df, length)
    upper = (hl2 + multiplier * atr_v).values
    lower = (hl2 - multiplier * atr_v).values
    close = df["close"].values

    n = len(df)
    st = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)
    final_upper = upper.copy()
    final_lower = lower.copy()

    for i in range(1, n):
        if np.isnan(atr_v.iat[i]):
            continue
        # باندها فقط در جهت روند حرکت می‌کنند
        final_upper[i] = upper[i] if (upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lower[i] if (lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]

        prev_dir = direction[i - 1]
        if prev_dir == 1 and close[i] < final_lower[i]:
            direction[i] = -1
        elif prev_dir == -1 and close[i] > final_upper[i]:
            direction[i] = 1
        else:
            direction[i] = prev_dir
        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.DataFrame({"supertrend": st, "direction": direction}, index=df.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd": macd_line,
        "macd_signal": signal_line,
        "macd_hist": macd_line - signal_line,
    })


def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(close, length)
    std = close.rolling(length).std(ddof=0)
    return pd.DataFrame({
        "bb_upper": mid + mult * std,
        "bb_mid": mid,
        "bb_lower": mid - mult * std,
    })


def kijun(df: pd.DataFrame, length: int = 26) -> pd.Series:
    return (df["high"].rolling(length).max() + df["low"].rolling(length).min()) / 2


def donchian(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    """کانال دانچیان — سقف/کف N کندل اخیر، یعنی همان سطوح مقاومت و حمایتِ
    ساختاری که استراتژی شکست روی آن‌ها کار می‌کند.

    مهم: باندها با shift(1) یک کندل عقب کشیده می‌شوند. بدون این کار، سقفِ
    کانال شامل high خود کندل جاری می‌شود و شرط «close > سقف» عملاً هیچ‌وقت
    برقرار نمی‌شود (نگاه به آینده‌ی خودش).
    """
    upper = df["high"].rolling(length).max().shift(1)
    lower = df["low"].rolling(length).min().shift(1)
    return pd.DataFrame({"dc_upper": upper, "dc_lower": lower, "dc_mid": (upper + lower) / 2},
                        index=df.index)


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """ADX و ±DI — سنجه‌ی «قدرت» روند، نه جهت آن.

    ADX بالا یعنی بازار روندی است (مناسب استراتژی‌های دنبال‌کننده‌ی روند و
    شکست)، ADX پایین یعنی بازار رنج است (مناسب استراتژی‌های بازگشتی/معکوس).
    """
    high, low = df["high"], df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    prev_close = df["close"].shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1 / length, adjust=False).mean()

    # تقسیم بر صفر در بازار کاملاً بی‌حرکت ممکن است؛ nan بعداً پر می‌شود
    safe_atr = atr_v.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / safe_atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_v = dx.ewm(alpha=1 / length, adjust=False).mean()

    return pd.DataFrame({"adx": adx_v.fillna(0.0),
                         "plus_di": plus_di.fillna(0.0),
                         "minus_di": minus_di.fillna(0.0)}, index=df.index)


def psar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2:
        return pd.Series(out, index=df.index)

    uptrend = high[1] > high[0]
    af = step
    ep = high[0] if uptrend else low[0]
    sar = low[0] if uptrend else high[0]
    out[0] = sar

    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if uptrend:
            sar = min(sar, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < sar:  # برگشت به نزولی
                uptrend = False
                sar = ep
                ep = low[i]
                af = step
            elif high[i] > ep:
                ep = high[i]
                af = min(af + step, max_step)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > sar:  # برگشت به صعودی
                uptrend = True
                sar = ep
                ep = high[i]
                af = step
            elif low[i] < ep:
                ep = low[i]
                af = min(af + step, max_step)
        out[i] = sar

    return pd.Series(out, index=df.index)


def pivot_points(df: pd.DataFrame, strength: int = 3) -> pd.DataFrame:
    """نقاط چرخش (Swing High/Low) با «قدرت پیوت» مشخص.

    یک کندل وقتی Swing High است که سقفش از `strength` کندل قبل *بزرگ‌تر* و از
    `strength` کندل بعد *بزرگ‌تر یا مساوی* باشد (و برعکس برای Swing Low).
    نامساوی اکید در یک سمت و غیراکید در سمت دیگر عمدی است: با دو نامساوی اکید،
    یک سقف دوقلوی کاملاً مسطح هیچ‌وقت پیوت شناخته نمی‌شد، و با دو نامساوی
    غیراکید، یک ناحیه‌ی صاف چند کندلِ پشت‌سرهم را پیوت اعلام می‌کرد.

    نکته‌ی مهم برای استفاده‌ی بدون نگاه به آینده: پیوتِ کندل i تا `strength`
    کندل بعد قابل تشخیص نیست، چون به کندل‌های سمت راستش نگاه می‌کند. پس فراخوان
    باید فقط پیوت‌هایی را به کار ببرد که اندیسشان حداقل `strength` کندل قبل از
    کندل جاری است — وگرنه عملاً از آینده خبر داده است.
    """
    h, lo = df["high"], df["low"]
    # با shift برداری حساب می‌شود نه با حلقه: بک‌تست این تابع را برای هر کندل
    # روی یک پنجره‌ی بزرگ‌شونده صدا می‌زند، پس هزینه‌اش مربعی جمع می‌شود.
    shifts = range(1, strength + 1)
    # skipna=False لازم است: با پیش‌فرض pandas، وقتی بخشی از پنجره NaN است
    # (لبه‌ی سری) از بقیه ماکسیمم گرفته می‌شد و کندلی که هنوز بال راست کاملش را
    # ندارد پیوت اعلام می‌شد — یعنی تصمیم با داده‌ی ناقص.
    h_left = pd.concat([h.shift(k) for k in shifts], axis=1).max(axis=1, skipna=False)
    h_right = pd.concat([h.shift(-k) for k in shifts], axis=1).max(axis=1, skipna=False)
    l_left = pd.concat([lo.shift(k) for k in shifts], axis=1).min(axis=1, skipna=False)
    l_right = pd.concat([lo.shift(-k) for k in shifts], axis=1).min(axis=1, skipna=False)
    # لبه‌های سری NaN می‌شوند و مقایسه با NaN همیشه False است، یعنی `strength`
    # کندل ابتدایی و انتهایی خودبه‌خود پیوت شناخته نمی‌شوند — همان چیزی که
    # می‌خواهیم.
    return pd.DataFrame({"pivot_high": (h > h_left) & (h >= h_right),
                         "pivot_low": (lo < l_left) & (lo <= l_right)},
                        index=df.index)
