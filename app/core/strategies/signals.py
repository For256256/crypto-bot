"""
تأمین‌کننده‌های سیگنالِ جهت‌دار برای «استراتژی‌ساز».

هر تأمین‌کننده یک سری برمی‌گرداند با مقدار ۱ (صعودی)، ‎−۱ (نزولی) یا ۰ (بی‌نظر)
برای هر کندل. استراتژی‌ساز یکی از این‌ها را به‌عنوان «پیشرو» می‌گیرد (که لحظه‌ی
*تغییر* حالتش ماشه است) و بقیه را به‌عنوان «تأییدکننده» (که فقط حالتشان در همان
لحظه مهم است).

چرا حالت و نه سیگنال لحظه‌ای: اگر همه‌ی اندیکاتورها باید دقیقاً در یک کندل
سیگنال بدهند، عملاً هیچ‌وقت هم‌زمان نمی‌شوند. تفکیک «پیشرو = تغییر حالت» از
«تأییدکننده = حالت»، همان کاری است که ابزارهای مشابه انجام می‌دهند و تنها
تعریفی است که در عمل سیگنال تولید می‌کند.
"""
import numpy as np
import pandas as pd

from app.core.strategies import indicators as ind


def _sign(cond_up: pd.Series, cond_down: pd.Series) -> pd.Series:
    """دو شرط بولی را به سری ۱/‎−۱/۰ تبدیل می‌کند."""
    out = pd.Series(0.0, index=cond_up.index)
    out[cond_up.fillna(False)] = 1.0
    out[cond_down.fillna(False)] = -1.0
    return out


def _hold(raw: pd.Series) -> pd.Series:
    """صفرها را با آخرین جهت غیرصفر پر می‌کند (حالتِ ماندگار)."""
    return raw.replace(0.0, np.nan).ffill().fillna(0.0)


# ---------- روندی ----------
def ema_filter(df, p):
    e = ind.ema(df["close"], int(p.get("ema_length", 200)))
    return _sign(df["close"] > e, df["close"] < e)


def ema_cross2(df, p):
    fast = ind.ema(df["close"], int(p.get("ema_fast", 21)))
    slow = ind.ema(df["close"], int(p.get("ema_slow", 55)))
    return _sign(fast > slow, fast < slow)


def ema_cross3(df, p):
    a = ind.ema(df["close"], int(p.get("ema_fast", 21)))
    b = ind.ema(df["close"], int(p.get("ema_mid", 55)))
    c = ind.ema(df["close"], int(p.get("ema_slow", 200)))
    return _sign((a > b) & (b > c), (a < b) & (b < c))


def supertrend_dir(df, p):
    st = ind.supertrend(df, int(p.get("st_length", 10)), float(p.get("st_multiplier", 3.0)))
    return st["direction"].astype(float)


def donchian_ribbon(df, p):
    dc = ind.donchian(df, int(p.get("donchian_length", 20)))
    return _hold(_sign(df["close"] > dc["dc_upper"], df["close"] < dc["dc_lower"]))


def ichimoku_cloud_dir(df, p):
    c = ind.ichimoku_cloud(df, int(p.get("tenkan_length", 9)), int(p.get("kijun_length", 26)),
                           int(p.get("senkou_b_length", 52)))
    top = c[["span_a", "span_b"]].max(axis=1)
    bottom = c[["span_a", "span_b"]].min(axis=1)
    return _sign(df["close"] > top, df["close"] < bottom)


def psar_dir(df, p):
    sar = ind.psar(df)
    return _sign(df["close"] > sar, df["close"] < sar)


def hull_dir(df, p):
    h = ind.hull_ma(df["close"], int(p.get("hull_length", 55)))
    return _sign(h > h.shift(1), h < h.shift(1))


def chandelier_dir(df, p):
    return ind.chandelier_exit(df, int(p.get("chandelier_length", 22)),
                               float(p.get("chandelier_mult", 3.0)))


def ssl_dir(df, p):
    return ind.ssl_channel(df, int(p.get("ssl_length", 10)))


def vortex_dir(df, p):
    v = ind.vortex(df, int(p.get("vortex_length", 14)))
    return _sign(v["vi_plus"] > v["vi_minus"], v["vi_plus"] < v["vi_minus"])


def vwap_dir(df, p):
    v = ind.vwap(df, int(p.get("vwap_length", 20)))
    return _sign(df["close"] > v, df["close"] < v)


# ---------- مومنتوم / اسیلاتور ----------
def rsi_dir(df, p):
    r = ind.rsi(df["close"], int(p.get("rsi_length", 14)))
    return _sign(r > float(p.get("rsi_long", 50)), r < float(p.get("rsi_short", 50)))


def rsi_ma_direction(df, p):
    r = ind.rsi(df["close"], int(p.get("rsi_length", 14)))
    m = ind.sma(r, int(p.get("rsi_ma_length", 14)))
    return _sign(r > m, r < m)


def macd_dir(df, p):
    m = ind.macd(df["close"], int(p.get("macd_fast", 12)), int(p.get("macd_slow", 26)),
                 int(p.get("macd_signal", 9)))
    return _sign(m["macd_hist"] > 0, m["macd_hist"] < 0)


def stochastic_dir(df, p):
    s = ind.stochastic(df, int(p.get("stoch_length", 14)))
    return _sign(s["stoch_k"] > s["stoch_d"], s["stoch_k"] < s["stoch_d"])


def cci_dir(df, p):
    c = ind.cci(df, int(p.get("cci_length", 20)))
    return _sign(c > 0, c < 0)


def awesome_dir(df, p):
    ao = ind.awesome_oscillator(df)
    return _sign(ao > 0, ao < 0)


def accelerator_dir(df, p):
    ac = ind.accelerator_oscillator(df)
    return _sign(ac > ac.shift(1), ac < ac.shift(1))


def dmi_dir(df, p):
    a = ind.adx(df, int(p.get("adx_length", 14)))
    strong = a["adx"] >= float(p.get("adx_min", 20))
    return _sign(strong & (a["plus_di"] > a["minus_di"]),
                 strong & (a["plus_di"] < a["minus_di"]))


def stc_dir(df, p):
    s = ind.schaff_trend_cycle(df["close"])
    return _sign(s > s.shift(1), s < s.shift(1))


def bb_oscillator(df, p):
    b = ind.bollinger(df["close"], int(p.get("bb_length", 20)), float(p.get("bb_mult", 2.0)))
    return _sign(df["close"] > b["bb_mid"], df["close"] < b["bb_mid"])


# ---------- حجم / نوسان ----------
def waddah_dir(df, p):
    w = ind.waddah_attar(df)
    strong = w["waddah_trend"].abs() > w["waddah_explosion"]
    return _sign(strong & (w["waddah_trend"] > 0), strong & (w["waddah_trend"] < 0))


def cmf_dir(df, p):
    c = ind.chaikin_money_flow(df, int(p.get("cmf_length", 20)))
    return _sign(c > 0, c < 0)


def volume_filter(df, p):
    """فیلتر بدون جهت: وقتی حجم بالای میانگین است هر دو جهت را تأیید می‌کند.

    مقدار ۲ یعنی «موافق با هر جهتی» — استراتژی‌ساز آن را همیشه هم‌سو حساب
    می‌کند. بدون این قرارداد، یک فیلتر حجمی هیچ‌وقت با هیچ سیگنالی هم‌جهت
    نمی‌شد و عملاً همه‌ی سیگنال‌ها را می‌بست.
    """
    avg = df["volume"].rolling(int(p.get("volume_length", 20))).mean()
    high = df["volume"] > avg * float(p.get("volume_mult", 1.0))
    return pd.Series(np.where(high.fillna(False), 2.0, 0.0), index=df.index)


PROVIDERS = {
    "ema_filter": ema_filter,
    "ema_cross2": ema_cross2,
    "ema_cross3": ema_cross3,
    "supertrend": supertrend_dir,
    "donchian": donchian_ribbon,
    "ichimoku": ichimoku_cloud_dir,
    "psar": psar_dir,
    "hull": hull_dir,
    "chandelier": chandelier_dir,
    "ssl": ssl_dir,
    "vortex": vortex_dir,
    "vwap": vwap_dir,
    "rsi": rsi_dir,
    "rsi_ma": rsi_ma_direction,
    "macd": macd_dir,
    "stochastic": stochastic_dir,
    "cci": cci_dir,
    "awesome": awesome_dir,
    "accelerator": accelerator_dir,
    "dmi": dmi_dir,
    "stc": stc_dir,
    "bb_osc": bb_oscillator,
    "waddah": waddah_dir,
    "cmf": cmf_dir,
    "volume": volume_filter,
}

# ترتیب نمایش در داشبورد — گروه‌بندی‌شده تا فهرست ۲۵تایی قابل خواندن بماند
ORDER = list(PROVIDERS.keys())


def direction_series(key: str, df: pd.DataFrame, params: dict) -> pd.Series:
    fn = PROVIDERS.get(key)
    if fn is None:
        raise KeyError(f"اندیکاتور ناشناخته: {key}")
    return fn(df, params).astype(float)
