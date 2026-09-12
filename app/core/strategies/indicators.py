"""
اندیکاتورهای تکنیکال مشترک استراتژی‌ها — همه روی DataFrame کندل
(ستون‌های open/high/low/close/volume) کار می‌کنند و pd.Series برمی‌گردانند.
"""
import math

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


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


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


def ichimoku_lines(df: pd.DataFrame, tenkan_length: int = 9,
                   kijun_length: int = 26) -> pd.DataFrame:
    """تنکن‌سن و کیجن‌سن ایچیموکو.

    هر دو «میانه‌ی بازه» هستند نه میانگین متحرک: وسط بالاترین سقف و
    پایین‌ترین کف N کندل اخیر. همین باعث می‌شود تا وقتی سقف و کف آن پنجره
    عوض نشده‌اند، خط دقیقاً صاف (افقی) بماند — رفتاری که میانگین متحرک هرگز
    ندارد و پایه‌ی استراتژی «کیجن صاف» است.
    """
    def mid(length: int) -> pd.Series:
        return (df["high"].rolling(length).max() + df["low"].rolling(length).min()) / 2
    return pd.DataFrame({"tenkan": mid(tenkan_length), "kijun": mid(kijun_length)},
                        index=df.index)


def realized_vol(close: pd.Series, length: int = 20) -> pd.Series:
    """نوسان تحقق‌یافته: انحراف معیار بازده‌های لگاریتمی روی پنجره‌ی length.

    عمداً بازده لگاریتمی و نه درصدی: در بازاری که ۳۰٪ می‌افتد و ۳۰٪ بالا
    می‌رود، بازده درصدی متقارن نیست ولی لگاریتمی هست، و نوسانی که می‌خواهیم
    اندازه بگیریم باید نسبت به جهت حرکت بی‌طرف باشد.
    """
    return np.log(close / close.shift(1)).rolling(length).std()


def rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    """رتبه‌ی صدکی هر مقدار نسبت به پنجره‌ی خودش (۰ تا ۱۰۰).

    برای فیلترهایی که باید «نسبت به تاریخ خودِ همین نماد» تصمیم بگیرند، نه با
    یک عدد ثابت: نوسان ۲٪ برای بیت‌کوین زیاد است و برای یک آلت‌کوین کم.
    """
    return series.rolling(window).apply(
        lambda w: (w[:-1] < w[-1]).sum() / max(len(w) - 1, 1) * 100, raw=True)


# ---------- اندیکاتورهای «استراتژی‌ساز» ----------
# همه از تعریف عمومی و استانداردشان پیاده شده‌اند.

def stochastic(df: pd.DataFrame, k_length: int = 14, k_smooth: int = 3,
               d_smooth: int = 3) -> pd.DataFrame:
    """استوکاستیک: جای بسته‌شدن قیمت در دامنه‌ی k_length کندل اخیر."""
    low = df["low"].rolling(k_length).min()
    high = df["high"].rolling(k_length).max()
    rng = (high - low).replace(0, np.nan)
    raw = (df["close"] - low) / rng * 100
    k = raw.rolling(k_smooth).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(d_smooth).mean()}, index=df.index)


def cci(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """CCI: فاصله‌ی قیمت معمول از میانگینش، بر حسب انحراف مطلق میانگین."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(length).mean()
    md = (tp - ma).abs().rolling(length).mean().replace(0, np.nan)
    return (tp - ma) / (0.015 * md)


def awesome_oscillator(df: pd.DataFrame, fast: int = 5, slow: int = 34) -> pd.Series:
    """AO بیل ویلیامز: تفاضل دو میانگین ساده روی قیمت میانه‌ی کندل."""
    median = (df["high"] + df["low"]) / 2
    return median.rolling(fast).mean() - median.rolling(slow).mean()


def accelerator_oscillator(df: pd.DataFrame) -> pd.Series:
    """AC بیل ویلیامز: AO منهای میانگین ۵ دوره‌ای خودش."""
    ao = awesome_oscillator(df)
    return ao - ao.rolling(5).mean()


def vwap(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """VWAP غلتان. عمداً غلتان است نه روزانه: موتور همیشه از ابتدای روز
    کندل ندارد و VWAP روزانه با پنجره‌ی ناقص عدد گمراه‌کننده می‌دهد."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)
    return (tp * vol).rolling(length).sum() / vol.rolling(length).sum()


def chaikin_money_flow(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """CMF: جریان پول، بر اساس جای بسته‌شدن در دامنه‌ی هر کندل × حجم."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    mfv = mfm * df["volume"]
    return mfv.rolling(length).sum() / df["volume"].rolling(length).sum().replace(0, np.nan)


def vortex(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """اندیکاتور ورتکس: قدرت حرکت صعودی در برابر نزولی."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    vm_plus = (df["high"] - df["low"].shift(1)).abs()
    vm_minus = (df["low"] - df["high"].shift(1)).abs()
    tr_sum = tr.rolling(length).sum().replace(0, np.nan)
    return pd.DataFrame({"vi_plus": vm_plus.rolling(length).sum() / tr_sum,
                         "vi_minus": vm_minus.rolling(length).sum() / tr_sum},
                        index=df.index)


def wma(series: pd.Series, length: int) -> pd.Series:
    """میانگین متحرک وزنی خطی: وزن جدیدترین کندل بیشترین است.

    با کانولوشن حساب می‌شود نه با `rolling().apply()`. دلیلش کارایی است: این
    تابع در بک‌تست روی پنجره‌ی بزرگ‌شونده برای هر کندل صدا زده می‌شود، پس
    هزینه‌اش مربعی جمع می‌شود و نسخه‌ی حلقه‌ای عملاً بک‌تست را قفل می‌کند.
    """
    n = max(1, int(length))
    arr = series.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if len(arr) >= n:
        w = np.arange(1, n + 1, dtype=float)
        w /= w.sum()
        # وزن‌ها برعکس داده می‌شوند تا در خروجیِ کانولوشن، بزرگ‌ترین وزن روی
        # جدیدترین کندلِ هر پنجره بیفتد.
        out[n - 1:] = np.convolve(arr, w[::-1], mode="valid")
    return pd.Series(out, index=series.index)


def hull_ma(series: pd.Series, length: int = 55) -> pd.Series:
    """میانگین متحرک هال: کم‌تأخیرتر از میانگین‌های معمول."""
    half = max(1, int(length / 2))
    sqrt_len = max(1, int(math.sqrt(length)))
    return wma(2 * wma(series, half) - wma(series, length), sqrt_len)


def ssl_hybrid(df: pd.DataFrame, ssl_length: int = 8, baseline_length: int = 55,
               keltner_mult: float = 0.2) -> pd.DataFrame:
    """هسته‌ی اندیکاتور SSL Hybrid.

    سه خروجی می‌دهد:
    - `hlv`: جهت کانال SSL. وقتی بسته‌شدن از میانگین وزنیِ سقف‌ها بالاتر برود
      ۱ می‌شود و وقتی از میانگین وزنیِ کف‌ها پایین‌تر برود ‎−۱؛ بین این دو،
      مقدار قبلی را نگه می‌دارد. برچسب‌های Buy/Sell دقیقاً روی *تغییر* همین
      مقدار چاپ می‌شوند.
    - `baseline`: همان خطِ ضخیم روی چارت — میانگین متحرک هال روی close.
    - `bar_color`: رنگ خط، از کانال کلتنر دور خط پایه: ۱ (آبی) وقتی قیمت بالای
      باند بالاست، ‎−۱ (قرمز) وقتی زیر باند پایین است، و ۰ (خاکستری) وسط.
    """
    ssl_high = wma(df["high"], ssl_length)
    ssl_low = wma(df["low"], ssl_length)
    close = df["close"].to_numpy(dtype=float)
    hi, lo = ssl_high.to_numpy(), ssl_low.to_numpy()
    hlv = np.zeros(len(df))
    for i in range(len(df)):
        prev = hlv[i - 1] if i else 0.0
        if not (np.isfinite(hi[i]) and np.isfinite(lo[i])):
            hlv[i] = prev
            continue
        hlv[i] = 1.0 if close[i] > hi[i] else (-1.0 if close[i] < lo[i] else prev)

    baseline = hull_ma(df["close"], baseline_length)
    keltma = baseline
    rangema = true_range(df).ewm(span=max(1, int(baseline_length)), adjust=False).mean()
    upper = keltma + rangema * keltner_mult
    lower = keltma - rangema * keltner_mult
    bar_color = np.where(df["close"] > upper, 1.0,
                         np.where(df["close"] < lower, -1.0, 0.0))
    return pd.DataFrame({"hlv": hlv, "baseline": baseline,
                         "bar_color": bar_color,
                         "ssl_high": ssl_high, "ssl_low": ssl_low},
                        index=df.index)


def chandelier_exit(df: pd.DataFrame, length: int = 22, mult: float = 3.0) -> pd.Series:
    """خروج شمعدانی: جهت (۱ صعودی، ‎−۱ نزولی) بر اساس عبور قیمت از حد
    ATR-محورِ پشت بالاترین سقف/پایین‌ترین کف اخیر."""
    atr_v = atr(df, length) * mult
    long_stop = df["high"].rolling(length).max() - atr_v
    short_stop = df["low"].rolling(length).min() + atr_v
    close = df["close"].to_numpy()
    # copy لازم است: to_numpy روی سری‌های تازه‌ساخته گاهی نمای فقط‌خواندنی
    # می‌دهد و این حلقه عمداً حدها را جابه‌جا می‌کند.
    ls, ss = long_stop.to_numpy().copy(), short_stop.to_numpy().copy()
    direction = np.ones(len(df))
    for i in range(1, len(df)):
        if not (np.isfinite(ls[i]) and np.isfinite(ss[i])):
            direction[i] = direction[i - 1]
            continue
        # حد قبلی فقط در جهت سود جابه‌جا می‌شود (مثل تریلینگ)
        if close[i - 1] > ls[i - 1]:
            ls[i] = max(ls[i], ls[i - 1])
        if close[i - 1] < ss[i - 1]:
            ss[i] = min(ss[i], ss[i - 1])
        direction[i] = 1 if close[i] > ss[i - 1] else (-1 if close[i] < ls[i - 1] else direction[i - 1])
    return pd.Series(direction, index=df.index)


def ssl_channel(df: pd.DataFrame, length: int = 10) -> pd.Series:
    """کانال SSL: جهت بر اساس اینکه بسته‌شدن بالای میانگین سقف‌هاست یا زیر
    میانگین کف‌ها. خروجی ۱ یا ‎−۱."""
    sma_high = df["high"].rolling(length).mean()
    sma_low = df["low"].rolling(length).mean()
    close = df["close"].to_numpy()
    hi, lo = sma_high.to_numpy(), sma_low.to_numpy()
    out = np.zeros(len(df))
    for i in range(1, len(df)):
        if not (np.isfinite(hi[i]) and np.isfinite(lo[i])):
            continue
        out[i] = 1 if close[i] > hi[i] else (-1 if close[i] < lo[i] else out[i - 1])
    return pd.Series(out, index=df.index)


def waddah_attar(df: pd.DataFrame, fast: int = 20, slow: int = 40,
                 bb_length: int = 20, bb_mult: float = 2.0,
                 sensitivity: int = 150) -> pd.DataFrame:
    """Waddah Attar Explosion: قدرت روند از تغییر MACD، و آستانه‌ی «انفجار»
    از پهنای باند بولینگر. خروجی: trend (علامت‌دار) و explosion."""
    macd_line = ema(df["close"], fast) - ema(df["close"], slow)
    trend = (macd_line - macd_line.shift(1)) * sensitivity
    bb = bollinger(df["close"], bb_length, bb_mult)
    return pd.DataFrame({"waddah_trend": trend,
                         "waddah_explosion": bb["bb_upper"] - bb["bb_lower"]},
                        index=df.index)


def schaff_trend_cycle(close: pd.Series, fast: int = 23, slow: int = 50,
                       cycle: int = 10) -> pd.Series:
    """STC: استوکاستیکِ دو مرحله‌ای روی خط MACD. بین ۰ تا ۱۰۰ نوسان می‌کند."""
    macd_line = ema(close, fast) - ema(close, slow)

    def stoch_of(s: pd.Series) -> pd.Series:
        lo = s.rolling(cycle).min()
        rng = (s.rolling(cycle).max() - lo).replace(0, np.nan)
        return ((s - lo) / rng * 100).ewm(span=3, adjust=False).mean()

    return stoch_of(stoch_of(macd_line)).clip(0, 100)


def ichimoku_cloud(df: pd.DataFrame, tenkan_length: int = 9, kijun_length: int = 26,
                   senkou_b_length: int = 52) -> pd.DataFrame:
    """ابر ایچیموکو: دو مرز ابر که kijun_length کندل به جلو منتقل شده‌اند —
    همان چیزی که در چارت زیر قیمتِ *امروز* دیده می‌شود."""
    lines = ichimoku_lines(df, tenkan_length, kijun_length)
    span_a = ((lines["tenkan"] + lines["kijun"]) / 2).shift(kijun_length)
    mid = (df["high"].rolling(senkou_b_length).max()
           + df["low"].rolling(senkou_b_length).min()) / 2
    return pd.DataFrame({"tenkan": lines["tenkan"], "kijun": lines["kijun"],
                         "span_a": span_a, "span_b": mid.shift(kijun_length)},
                        index=df.index)

def range_filter(close: pd.Series, period: int = 100, mult: float = 3.0) -> pd.DataFrame:
    """فیلتر بازه (Range Filter) به سبک Donovan Wall.

    یک خط پلکانی که فقط وقتی حرکت می‌کند که قیمت بیش از «بازه‌ی هموارشده» از
    آن فاصله بگیرد؛ در نوسان‌های کوچک‌تر از آن بازه ثابت می‌ماند. همین ثابت
    ماندن است که نویز را حذف می‌کند.

    خروجی: `filt` (خود خط)، `smrng` (نصف پهنای بازه)، و شمارنده‌های `upward` و
    `downward` که می‌گویند خط چند کندل است پشت‌سرهم بالا/پایین می‌رود.
    """
    n = max(1, int(period))
    avg_range = close.diff().abs().ewm(span=n, adjust=False).mean()
    smrng = avg_range.ewm(span=max(1, n * 2 - 1), adjust=False).mean() * float(mult)

    x = close.to_numpy(dtype=float)
    r = smrng.to_numpy(dtype=float)
    filt = np.empty(len(x)); filt[:] = np.nan
    prev = x[0] if len(x) else np.nan
    for i in range(len(x)):
        ri = r[i]
        if not np.isfinite(ri):
            filt[i] = prev
            continue
        if x[i] > prev:
            prev = prev if x[i] - ri < prev else x[i] - ri
        else:
            prev = prev if x[i] + ri > prev else x[i] + ri
        filt[i] = prev

    up = np.zeros(len(x)); dn = np.zeros(len(x))
    for i in range(1, len(x)):
        if filt[i] > filt[i - 1]:
            up[i] = up[i - 1] + 1; dn[i] = 0
        elif filt[i] < filt[i - 1]:
            dn[i] = dn[i - 1] + 1; up[i] = 0
        else:
            up[i] = up[i - 1]; dn[i] = dn[i - 1]
    return pd.DataFrame({"filt": filt, "smrng": smrng.to_numpy(),
                         "upward": up, "downward": dn}, index=close.index)


def rqk(close: pd.Series, lookback: int = 8, relative_weight: float = 8.0) -> pd.Series:
    """برآورد هسته‌ی درجه‌دوم گویا (Rational Quadratic Kernel).

    میانگین وزنیِ `lookback` کندل آخر است که وزن هر کندل با فاصله‌اش افت
    می‌کند: w(i) = (1 + i²/(2·α·L²))^(−α). نسبت به میانگین متحرک نرم‌تر است
    و دیرتر هم عوض نمی‌شود.

    فقط از کندل جاری و گذشته استفاده می‌کند، پس بازترسیم (repaint) ندارد.
    """
    L = max(2, int(lookback))
    a = float(relative_weight)
    i = np.arange(L, dtype=float)
    w = np.power(1.0 + (i * i) / (2.0 * a * L * L), -a)
    w = w / w.sum()
    arr = close.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if len(arr) >= L:
        # w[0] وزن کندل جاری است، پس برای کانولوشن معکوس نمی‌شود.
        out[L - 1:] = np.convolve(arr, w, mode="valid")
    return pd.Series(out, index=close.index)

