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
