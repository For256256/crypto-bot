"""
ثبت‌نام (registry) استراتژی‌ها. هر استراتژی:
- key: شناسه‌ی ذخیره‌شده در پیکربندی نماد
- label: نام نمایشی در داشبورد
- params_schema: پارامترهای قابل ویرایش در داشبورد
- fn: تابع (df, params) -> dict سیگنال

خروجی سیگنال: {"signal": "buy"|"sell"|"none", "close": float, "atr": float|None,
               "source": "strategy", "extra": {نام‌اندیکاتور: مقدار}}
"""
import pandas as pd

from app.core.strategies import indicators as ind


def _signal(sig: str, df: pd.DataFrame, extra: dict, atr_value=None) -> dict:
    return {
        "signal": sig,
        "close": float(df["close"].iat[-1]),
        "atr": float(atr_value) if atr_value is not None and pd.notna(atr_value) else None,
        "source": "strategy",
        "extra": {k: (None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v))
                  for k, v in extra.items()},
    }


# ---------- ۱) SuperTrend + EMA + RSI ----------
def supertrend_ema_rsi(df: pd.DataFrame, p: dict) -> dict:
    st_len = int(p.get("st_length", 10))
    st_mult = float(p.get("st_multiplier", 3.0))
    ema_len = int(p.get("ema_length", 200))
    rsi_len = int(p.get("rsi_length", 14))

    st = ind.supertrend(df, st_len, st_mult)
    ema_v = ind.ema(df["close"], ema_len)
    rsi_v = ind.rsi(df["close"], rsi_len)
    atr_v = ind.atr(df, 14).iat[-1]

    close = df["close"].iat[-1]
    direction = int(st["direction"].iat[-1])
    prev_direction = int(st["direction"].iat[-2]) if len(st) > 1 else direction
    extra = {
        "supertrend": st["supertrend"].iat[-1],
        "ema_fast": ema_v.iat[-1],
        "rsi": rsi_v.iat[-1],
        "atr": atr_v,
    }
    sig = "none"
    # ورود فقط روی تغییر جهت SuperTrend + تایید EMA و RSI
    if direction == 1 and prev_direction == -1 and close > ema_v.iat[-1] and rsi_v.iat[-1] > 50:
        sig = "buy"
    elif direction == -1 and prev_direction == 1 and close < ema_v.iat[-1] and rsi_v.iat[-1] < 50:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۲) MACD + فیلتر روند EMA ----------
def macd_trend(df: pd.DataFrame, p: dict) -> dict:
    fast = int(p.get("fast", 12))
    slow = int(p.get("slow", 26))
    signal_len = int(p.get("signal", 9))
    trend_len = int(p.get("trend_ema", 100))

    m = ind.macd(df["close"], fast, slow, signal_len)
    trend = ind.ema(df["close"], trend_len)
    atr_v = ind.atr(df, 14).iat[-1]

    hist_now, hist_prev = m["macd_hist"].iat[-1], m["macd_hist"].iat[-2]
    close = df["close"].iat[-1]
    extra = {
        "macd": m["macd"].iat[-1],
        "macd_signal": m["macd_signal"].iat[-1],
        "macd_hist": hist_now,
        "trend_ema": trend.iat[-1],
    }
    sig = "none"
    if hist_prev <= 0 < hist_now and close > trend.iat[-1]:
        sig = "buy"
    elif hist_prev >= 0 > hist_now and close < trend.iat[-1]:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۳) برگشت از باند بولینگر + RSI ----------
def bollinger_rsi(df: pd.DataFrame, p: dict) -> dict:
    length = int(p.get("bb_length", 20))
    mult = float(p.get("bb_mult", 2.0))
    rsi_len = int(p.get("rsi_length", 14))
    rsi_low = float(p.get("rsi_oversold", 35))
    rsi_high = float(p.get("rsi_overbought", 65))

    bb = ind.bollinger(df["close"], length, mult)
    rsi_v = ind.rsi(df["close"], rsi_len)
    atr_v = ind.atr(df, 14).iat[-1]

    close = df["close"].iat[-1]
    prev_close = df["close"].iat[-2]
    extra = {
        "bb_upper": bb["bb_upper"].iat[-1],
        "bb_mid": bb["bb_mid"].iat[-1],
        "bb_lower": bb["bb_lower"].iat[-1],
        "rsi": rsi_v.iat[-1],
    }
    sig = "none"
    # ورود هنگام برگشت قیمت به داخل باند در ناحیه‌ی اشباع
    if prev_close <= bb["bb_lower"].iat[-2] and close > bb["bb_lower"].iat[-1] and rsi_v.iat[-1] < rsi_low:
        sig = "buy"
    elif prev_close >= bb["bb_upper"].iat[-2] and close < bb["bb_upper"].iat[-1] and rsi_v.iat[-1] > rsi_high:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۴) روبان میانگین متحرک + Kijun + PSAR ----------
def ma_kijun_psar(df: pd.DataFrame, p: dict) -> dict:
    fast = int(p.get("ma_fast", 10))
    mid = int(p.get("ma_mid", 30))
    slow = int(p.get("ma_slow", 60))
    kj_short_len = int(p.get("kijun_short", 9))
    kj_long_len = int(p.get("kijun_long", 26))

    ma_f = ind.sma(df["close"], fast)
    ma_m = ind.sma(df["close"], mid)
    ma_s = ind.sma(df["close"], slow)
    kj_s = ind.kijun(df, kj_short_len)
    kj_l = ind.kijun(df, kj_long_len)
    psar_v = ind.psar(df)
    atr_v = ind.atr(df, 14).iat[-1]

    close = df["close"].iat[-1]
    extra = {
        "ma_fast": ma_f.iat[-1],
        "ma_mid": ma_m.iat[-1],
        "ma_slow": ma_s.iat[-1],
        "kijun_short": kj_s.iat[-1],
        "kijun_long": kj_l.iat[-1],
        "psar": psar_v.iat[-1],
    }
    bull = ma_f.iat[-1] > ma_m.iat[-1] > ma_s.iat[-1]
    bear = ma_f.iat[-1] < ma_m.iat[-1] < ma_s.iat[-1]
    prev_bull = ma_f.iat[-2] > ma_m.iat[-2] > ma_s.iat[-2]
    prev_bear = ma_f.iat[-2] < ma_m.iat[-2] < ma_s.iat[-2]

    sig = "none"
    if bull and not prev_bull and close > kj_l.iat[-1] and close > psar_v.iat[-1]:
        sig = "buy"
    elif bear and not prev_bear and close < kj_l.iat[-1] and close < psar_v.iat[-1]:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۵) شکست کانال (Breakout) ----------
def donchian_breakout(df: pd.DataFrame, p: dict) -> dict:
    """شکست سطوح حمایت/مقاومت با تأیید حجم.

    سطوح از کانال دانچیان می‌آیند (سقف/کف N کندل قبل). ورود وقتی است که
    کندل *بسته* بالای سقف یا پایین کف بسته شود — نه صرفاً لمس کند، چون
    فتیله‌ی لحظه‌ای سطح را می‌زند و برمی‌گردد. تأیید حجم اجباری است: شکستِ
    بی‌حجم معمولاً شکست کاذب (fakeout) است.
    """
    length = int(p.get("channel_length", 20))
    vol_len = int(p.get("volume_length", 20))
    # ضریب حجم ۲.۰ (نه ۱.۵): در تیونینگ، سخت‌گیری بیشتر روی حجم هم میانه‌ی
    # بازده و هم نرخ برد را بهتر کرد. حاشیه‌ی ATR و فیلتر روند روشن می‌مانند
    # چون بدترین افت سرمایه را از ۱۵.۵٪ به ۱۰.۰٪ کم می‌کنند.
    vol_mult = float(p.get("volume_mult", 2.0))
    buffer_atr = float(p.get("atr_buffer", 0.25))
    trend_len = int(p.get("trend_ema_filter", 200))

    dc = ind.donchian(df, length)
    atr_series = ind.atr(df, 14)
    atr_v = atr_series.iat[-1]
    vol_avg = ind.sma(df["volume"], vol_len)

    close = df["close"].iat[-1]
    prev_close = df["close"].iat[-2]
    upper, lower = dc["dc_upper"].iat[-1], dc["dc_lower"].iat[-1]
    vol_now, vol_ref = df["volume"].iat[-1], vol_avg.iat[-1]
    trend_v = ind.ema(df["close"], trend_len).iat[-1] if trend_len > 0 else None

    extra = {"dc_upper": upper, "dc_lower": lower, "volume": vol_now,
             "volume_avg": vol_ref, "atr": atr_v, "trend_ema": trend_v}

    sig = "none"
    if pd.isna(upper) or pd.isna(lower) or pd.isna(atr_v) or pd.isna(vol_ref) or vol_ref <= 0:
        return _signal(sig, df, extra, atr_v)

    # حاشیه‌ی ATR: قیمت باید «قاطعانه» رد شود، نه فقط چند تیک بالاتر
    margin = buffer_atr * atr_v
    vol_ok = vol_now >= vol_mult * vol_ref
    # کندل قبلی هنوز داخل کانال بوده باشد تا فقط لحظه‌ی شکست سیگنال بدهد،
    # نه هر کندلی که بالای سطح باقی مانده
    if close > upper + margin and prev_close <= upper and vol_ok:
        if trend_v is None or close > trend_v:
            sig = "buy"
    elif close < lower - margin and prev_close >= lower and vol_ok:
        if trend_v is None or close < trend_v:
            sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۶) معکوس / بازگشت به میانگین (Contrarian) ----------
def rsi_contrarian(df: pd.DataFrame, p: dict) -> dict:
    """خرید در اشباع فروش، فروش در اشباع خرید.

    دو محافظ مهم دارد که نسخه‌ی خام این استراتژی ندارد:
    ۱) ورود روی *خروج* از ناحیه‌ی اشباع است، نه ورود به آن. RSI در یک روند
       قوی می‌تواند روزها زیر ۳۰ بماند؛ خرید در لحظه‌ی رسیدن به ۳۰ یعنی
       گرفتن چاقوی در حال سقوط.
    ۲) فیلتر ADX: این استراتژی فقط در بازار رنج معنا دارد. وقتی ADX بالا
       باشد یعنی روند قوی است و معامله‌ی خلاف آن پرریسک‌ترین کار ممکن است.
    """
    rsi_len = int(p.get("rsi_length", 14))
    oversold = float(p.get("oversold", 25))
    overbought = float(p.get("overbought", 75))
    adx_len = int(p.get("adx_length", 14))
    # پیش‌فرض ۱۰۰ یعنی فیلتر عملاً خاموش است. این خلاف انتظار اولیه‌ی خودم بود:
    # در تیونینگ روی ۱۸ مجموعه‌ی واقعی، سفت‌کردن این فیلتر (۲۰ یا ۲۵) تعداد
    # سیگنال را چنان کم می‌کرد که نتیجه بدتر می‌شد، نه بهتر. فیلتر سر جایش
    # می‌ماند تا هرکس بخواهد فقط در بازار رنج معامله کند آن را پایین بیاورد.
    adx_max = float(p.get("adx_max", 100))

    rsi_v = ind.rsi(df["close"], rsi_len)
    adx_df = ind.adx(df, adx_len)
    atr_v = ind.atr(df, 14).iat[-1]

    now, prev = rsi_v.iat[-1], rsi_v.iat[-2]
    adx_now = adx_df["adx"].iat[-1]
    extra = {"rsi": now, "adx": adx_now, "atr": atr_v}

    sig = "none"
    if pd.isna(adx_now) or adx_now > adx_max:
        return _signal(sig, df, extra, atr_v)   # بازار روندی است؛ معکوس نمی‌گیریم
    if prev <= oversold < now:
        sig = "buy"
    elif prev >= overbought > now:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۷) واکنش به شوک خبری (اسپایک نوسان و حجم) ----------
def volatility_shock(df: pd.DataFrame, p: dict) -> dict:
    """ربات به خبر دسترسی ندارد؛ این استراتژی «رد پای» خبر را در بازار
    تشخیص می‌دهد: انفجار هم‌زمان حجم و دامنه‌ی کندل.

    یک خبر مهم تقریباً همیشه سه اثر دارد: حجم چند برابر می‌شود، دامنه‌ی
    کندل نسبت به ATR جهش می‌کند، و کندل بدنه‌ی بزرگ و فتیله‌ی کوچک دارد
    (حرکت قاطع، نه تردید). ورود در جهت همان کندل ضربه است.
    """
    vol_len = int(p.get("volume_length", 20))
    vol_mult = float(p.get("volume_spike_mult", 2.0))
    range_mult = float(p.get("range_atr_mult", 2.0))
    body_min = float(p.get("body_ratio_min", 0.7))

    atr_series = ind.atr(df, 14)
    # ATR کندل قبل مبناست: ATR جاری خودش شامل همین کندل انفجاری است و
    # نسبت را رقیق می‌کند، پس جهش دیرتر تشخیص داده می‌شود.
    atr_ref = atr_series.iat[-2]
    atr_v = atr_series.iat[-1]
    vol_avg = ind.sma(df["volume"], vol_len).iat[-2]

    row = df.iloc[-1]
    high, low = float(row["high"]), float(row["low"])
    open_, close = float(row["open"]), float(row["close"])
    vol_now = float(row["volume"])
    rng = high - low
    body = abs(close - open_)
    body_ratio = (body / rng) if rng > 0 else 0.0

    extra = {"range": rng, "atr_ref": atr_ref, "body_ratio": body_ratio,
             "volume": vol_now, "volume_avg": vol_avg, "atr": atr_v}

    sig = "none"
    if pd.isna(atr_ref) or atr_ref <= 0 or pd.isna(vol_avg) or vol_avg <= 0:
        return _signal(sig, df, extra, atr_v)

    shock = (vol_now >= vol_mult * vol_avg
             and rng >= range_mult * atr_ref
             and body_ratio >= body_min)
    if shock:
        sig = "buy" if close > open_ else "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۸) ترکیبی: روند + شکست ----------
def hybrid_trend_breakout(df: pd.DataFrame, p: dict) -> dict:
    """روند را با EMA و ADX تشخیص می‌دهد، ورود را با شکست کانال می‌گیرد.

    منطقش این است: شکست‌ها در جهت روند اصلی نرخ موفقیت بالاتری دارند و
    شکست خلاف روند اغلب کاذب است. پس سه شرط هم‌زمان لازم است — جهت EMA،
    قدرت روند (ADX)، و شکست واقعی کانال با حجم.
    """
    ema_fast_len = int(p.get("ema_fast", 50))
    ema_slow_len = int(p.get("ema_slow", 200))
    adx_len = int(p.get("adx_length", 14))
    adx_min = float(p.get("adx_min", 20))
    # کانال ۵۵ (نه ۲۰): در تیونینگ، کانال بلندتر هم میانه‌ی بازده و هم بدترین
    # افت سرمایه را بهتر کرد — شکست‌های کوچک در دل روند بیشترشان نویز بودند.
    length = int(p.get("channel_length", 55))
    vol_len = int(p.get("volume_length", 20))
    vol_mult = float(p.get("volume_mult", 1.2))

    ema_f = ind.ema(df["close"], ema_fast_len)
    ema_s = ind.ema(df["close"], ema_slow_len)
    adx_df = ind.adx(df, adx_len)
    dc = ind.donchian(df, length)
    vol_avg = ind.sma(df["volume"], vol_len)
    atr_v = ind.atr(df, 14).iat[-1]

    close = df["close"].iat[-1]
    prev_close = df["close"].iat[-2]
    upper, lower = dc["dc_upper"].iat[-1], dc["dc_lower"].iat[-1]
    adx_now = adx_df["adx"].iat[-1]
    vol_now, vol_ref = df["volume"].iat[-1], vol_avg.iat[-1]

    extra = {"ema_fast": ema_f.iat[-1], "ema_slow": ema_s.iat[-1], "adx": adx_now,
             "dc_upper": upper, "dc_lower": lower, "volume": vol_now,
             "volume_avg": vol_ref, "atr": atr_v}

    sig = "none"
    if pd.isna(upper) or pd.isna(lower) or pd.isna(adx_now) or pd.isna(vol_ref) or vol_ref <= 0:
        return _signal(sig, df, extra, atr_v)
    if adx_now < adx_min:
        return _signal(sig, df, extra, atr_v)   # روند به‌قدر کافی قوی نیست

    vol_ok = vol_now >= vol_mult * vol_ref
    uptrend = ema_f.iat[-1] > ema_s.iat[-1] and close > ema_s.iat[-1]
    downtrend = ema_f.iat[-1] < ema_s.iat[-1] and close < ema_s.iat[-1]

    if uptrend and close > upper and prev_close <= upper and vol_ok:
        sig = "buy"
    elif downtrend and close < lower and prev_close >= lower and vol_ok:
        sig = "sell"
    return _signal(sig, df, extra, atr_v)


# ---------- ۹) انتخاب خودکار استراتژی بر اساس وضعیت بازار ----------
def adaptive_regime(df: pd.DataFrame, p: dict) -> dict:
    """رژیم بازار را تشخیص می‌دهد و کار را به استراتژی مناسب همان رژیم می‌سپارد.

    چرا این کار منطقی است: در تیونینگ روی ۱۸ مجموعه‌ی واقعی، استراتژی‌های
    بازگشتی مثبت و استراتژی‌های روندی منفی بودند — یعنی عملکرد هر استراتژی
    شدیداً به نوع بازار وابسته است، نه به «خوب یا بد بودن» خودش.

    تشخیص رژیم:
    - شوک (اولویت اول): جهش هم‌زمان حجم و دامنه → volatility_shock
    - روندی (ADX >= adx_trend_min) → supertrend_ema_rsi
    - رنج   (ADX <= adx_range_max) → rsi_contrarian
    - بین دو آستانه: نوار مرده، هیچ معامله‌ای انجام نمی‌شود.

    نوار مرده عمدی است. اگر یک آستانه‌ی واحد می‌گذاشتیم، ADX که دور همان عدد
    نوسان می‌کند باعث می‌شد استراتژی هر چند کندل عوض شود و ربات مدام بین دو
    منطق متضاد بپرد. با فاصله‌انداختن بین دو آستانه، تغییر رژیم فقط وقتی رخ
    می‌دهد که واقعاً از یک حالت به حالت دیگر رفته باشیم.

    نکته: این تابع stateless است — هیچ حافظه‌ای از رژیم قبلی ندارد و لازم هم
    نیست، چون نوار مرده همان کار را بدون state انجام می‌دهد.
    """
    adx_len = int(p.get("adx_length", 14))
    # آستانه‌ی روند ۳۰ (نه ۲۵): در مقایسه روی ۱۸ مجموعه، نوار مرده‌ی پهن‌تر
    # نتیجه‌ی بهتری داد (میانه −۰.۲۴٪ → +۰.۰۴٪). حذف کامل نوار مرده (۲۵/۲۵)
    # هم میانه را به −۰.۶۲٪ و بدترین افت را از −۱۰.۹٪ به −۱۳.۹٪ بدتر کرد.
    adx_trend_min = float(p.get("adx_trend_min", 30))
    adx_range_max = float(p.get("adx_range_max", 20))
    use_shock = float(p.get("enable_shock", 1)) >= 1

    atr_series = ind.atr(df, 14)
    atr_v = atr_series.iat[-1]
    adx_now = ind.adx(df, adx_len)["adx"].iat[-1]

    def tagged(result: dict, regime: str, delegate: str | None) -> dict:
        result["regime"] = regime
        result["delegate"] = delegate
        result["extra"]["adx"] = None if pd.isna(adx_now) else float(adx_now)
        return result

    if pd.isna(adx_now):
        return tagged(_signal("none", df, {"atr": atr_v}, atr_v), "none", None)

    # شوک خبری بر همه‌چیز مقدم است: وقتی بازار در حال جهش است، نه منطق روندی
    # معنا دارد نه منطق بازگشتی.
    if use_shock:
        shock_out = volatility_shock(df, p)
        if shock_out["signal"] in ("buy", "sell"):
            return tagged(shock_out, "shock", "volatility_shock")

    if adx_now >= adx_trend_min:
        return tagged(supertrend_ema_rsi(df, p), "trend", "supertrend_ema_rsi")
    if adx_now <= adx_range_max:
        return tagged(rsi_contrarian(df, p), "range", "rsi_contrarian")
    return tagged(_signal("none", df, {"atr": atr_v}, atr_v), "unclear", None)


STRATEGIES = {
    "supertrend_ema_rsi": {
        "label": "SuperTrend + EMA + RSI",
        "params_schema": [
            {"key": "st_length", "label": "دوره SuperTrend", "type": "int", "default": 10},
            {"key": "st_multiplier", "label": "ضریب SuperTrend", "type": "float", "default": 3.0, "step": 0.5},
            {"key": "ema_length", "label": "دوره EMA روند", "type": "int", "default": 200},
            {"key": "rsi_length", "label": "دوره RSI", "type": "int", "default": 14},
        ],
        "fn": supertrend_ema_rsi,
    },
    "macd_trend": {
        "label": "MACD + فیلتر روند EMA",
        "params_schema": [
            {"key": "fast", "label": "MACD سریع", "type": "int", "default": 12},
            {"key": "slow", "label": "MACD کند", "type": "int", "default": 26},
            {"key": "signal", "label": "خط سیگنال", "type": "int", "default": 9},
            {"key": "trend_ema", "label": "دوره EMA روند", "type": "int", "default": 100},
        ],
        "fn": macd_trend,
    },
    "bollinger_rsi": {
        "label": "برگشت بولینگر + RSI",
        "params_schema": [
            {"key": "bb_length", "label": "دوره بولینگر", "type": "int", "default": 20},
            {"key": "bb_mult", "label": "ضریب انحراف", "type": "float", "default": 2.0, "step": 0.1},
            {"key": "rsi_length", "label": "دوره RSI", "type": "int", "default": 14},
            {"key": "rsi_oversold", "label": "RSI اشباع فروش", "type": "float", "default": 35},
            {"key": "rsi_overbought", "label": "RSI اشباع خرید", "type": "float", "default": 65},
        ],
        "fn": bollinger_rsi,
    },
    "ma_kijun_psar": {
        "label": "روبان MA + Kijun + PSAR",
        "params_schema": [
            {"key": "ma_fast", "label": "MA سریع", "type": "int", "default": 10},
            {"key": "ma_mid", "label": "MA میانی", "type": "int", "default": 30},
            {"key": "ma_slow", "label": "MA کند", "type": "int", "default": 60},
            {"key": "kijun_short", "label": "Kijun کوتاه", "type": "int", "default": 9},
            {"key": "kijun_long", "label": "Kijun بلند", "type": "int", "default": 26},
        ],
        "fn": ma_kijun_psar,
    },
    "donchian_breakout": {
        "label": "شکست کانال + تأیید حجم",
        "params_schema": [
            {"key": "channel_length", "label": "دوره کانال (سطوح حمایت/مقاومت)", "type": "int", "default": 20},
            {"key": "volume_length", "label": "دوره میانگین حجم", "type": "int", "default": 20},
            {"key": "volume_mult", "label": "ضریب تأیید حجم", "type": "float", "default": 2.0, "step": 0.1},
            {"key": "atr_buffer", "label": "حاشیه شکست (ضریب ATR)", "type": "float", "default": 0.25, "step": 0.05},
            {"key": "trend_ema_filter", "label": "EMA فیلتر روند (۰ = خاموش)", "type": "int", "default": 200},
        ],
        "fn": donchian_breakout,
    },
    "rsi_contrarian": {
        "label": "معکوس (بازگشت از اشباع RSI)",
        "params_schema": [
            {"key": "rsi_length", "label": "دوره RSI", "type": "int", "default": 14},
            {"key": "oversold", "label": "سطح اشباع فروش", "type": "float", "default": 25},
            {"key": "overbought", "label": "سطح اشباع خرید", "type": "float", "default": 75},
            {"key": "adx_length", "label": "دوره ADX", "type": "int", "default": 14},
            {"key": "adx_max", "label": "حداکثر ADX (بالاتر = بازار روندی، معامله نمی‌شود)", "type": "float", "default": 100},
        ],
        "fn": rsi_contrarian,
    },
    "volatility_shock": {
        "label": "واکنش به شوک خبری (جهش حجم و نوسان)",
        "params_schema": [
            {"key": "volume_length", "label": "دوره میانگین حجم", "type": "int", "default": 20},
            {"key": "volume_spike_mult", "label": "ضریب جهش حجم", "type": "float", "default": 2.0, "step": 0.5},
            {"key": "range_atr_mult", "label": "ضریب جهش دامنه نسبت به ATR", "type": "float", "default": 2.0, "step": 0.25},
            {"key": "body_ratio_min", "label": "حداقل نسبت بدنه به دامنه کندل", "type": "float", "default": 0.7, "step": 0.05},
        ],
        "fn": volatility_shock,
    },
    "hybrid_trend_breakout": {
        "label": "ترکیبی: روند (EMA+ADX) + شکست کانال",
        "params_schema": [
            {"key": "ema_fast", "label": "EMA سریع", "type": "int", "default": 50},
            {"key": "ema_slow", "label": "EMA کند", "type": "int", "default": 200},
            {"key": "adx_length", "label": "دوره ADX", "type": "int", "default": 14},
            {"key": "adx_min", "label": "حداقل ADX (قدرت روند)", "type": "float", "default": 20},
            {"key": "channel_length", "label": "دوره کانال شکست", "type": "int", "default": 55},
            {"key": "volume_length", "label": "دوره میانگین حجم", "type": "int", "default": 20},
            {"key": "volume_mult", "label": "ضریب تأیید حجم", "type": "float", "default": 1.2, "step": 0.1},
        ],
        "fn": hybrid_trend_breakout,
    },
    "adaptive_regime": {
        "label": "خودکار: انتخاب استراتژی بر اساس وضعیت بازار",
        "params_schema": [
            {"key": "adx_length", "label": "دوره ADX", "type": "int", "default": 14},
            {"key": "adx_trend_min", "label": "آستانه بازار روندی (ADX بالاتر)", "type": "float", "default": 30},
            {"key": "adx_range_max", "label": "آستانه بازار رنج (ADX پایین‌تر)", "type": "float", "default": 20},
            {"key": "enable_shock", "label": "واکنش به شوک خبری (۱ = روشن، ۰ = خاموش)", "type": "int", "default": 1},
        ],
        "fn": adaptive_regime,
    },
}


def list_strategies() -> list:
    """برای API داشبورد: [{key, label, params_schema}]"""
    return [
        {"key": key, "label": cfg["label"], "params_schema": cfg["params_schema"]}
        for key, cfg in STRATEGIES.items()
    ]


def run_strategy(key: str, df: pd.DataFrame, params: dict) -> dict:
    cfg = STRATEGIES.get(key)
    if cfg is None:
        raise KeyError(f"استراتژی ناشناخته: {key}")
    merged = {p["key"]: p["default"] for p in cfg["params_schema"]}
    merged.update(params or {})
    return cfg["fn"](df, merged)
