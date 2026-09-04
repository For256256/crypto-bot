"""
بک‌تست ساده‌ی استراتژی‌ها روی کندل‌های تاریخی — برای صفحه‌ی «مدیریت استراتژی».

روش: از کندل warmup به بعد، روی هر کندل بسته‌شده استراتژی اجرا می‌شود (بدون
نگاه به آینده). هر سیگنال buy/sell یک پوزیشن باز/معکوس می‌کند.

SL/TP دقیقاً مثل موتور واقعی و متقارن از ATR ساخته می‌شود (هر دو با همان
sl_tp_atr_mult) و در کندل‌های بعدی با high/low چک می‌شود. کارمزد رفت و برگشت
هم با همان نرخ صرافی کسر می‌شود — بدون آن، نتیجه‌ی بک‌تست (به‌ویژه در حالت
معکوس) به‌شکل گمراه‌کننده‌ای خوش‌بینانه می‌شود.

invert=True همان کاری را می‌کند که تیک «معکوس» روی حساب انجام می‌دهد: هر
سیگنال buy به sell و برعکس تبدیل می‌شود — برای تست فرضیه پیش از ریسک واقعی.

trend_df همان کاری را می‌کند که «فیلتر روند تایم‌فریم بالاتر» روی حساب انجام
می‌دهد: کندل‌های یک تایم‌فریم دیگر (معمولاً بالاتر) گرفته می‌شود و هر سیگنالی
که خلاف روند آن باشد اجرا نمی‌شود. برای جلوگیری از نگاه به آینده، در هر کندل
فقط از آخرین کندلِ روند که *قبل از* آن بسته شده استفاده می‌شود.
"""
import pandas as pd

from app.core.exchanges.toobit import TAKER_FEE_RATE
from app.core.strategies import indicators as ind
from app.core.strategies import trend as trend_mod
from app.core.strategies.registry import run_strategy

# چند استراتژی EMA۲۰۰ دارند. با warmup=۶۰ آن EMA هنوز همگرا نشده و سیگنال‌های
# اول بک‌تست روی مقداری بی‌معنا گرفته می‌شدند — یعنی نتیجه‌ی بک‌تست با رفتار
# واقعی ربات (که ۵۰۰ کندل می‌گیرد) یکی نبود. warmup باید از بلندترین دوره‌ی
# اندیکاتورها بیشتر باشد.
WARMUP = 210
DEFAULT_SL_TP_ATR_MULT = 3.0   # متقارن، مثل موتور واقعی
START_EQUITY = 10000.0
RISK_PCT = 1.0  # ٪ریسک هر معامله از اکوییتی جاری — مثل ربات واقعی


def _bar_step(times: list) -> int:
    """طول یک کندل بر حسب میلی‌ثانیه — از فاصله‌ی زمان‌ها استخراج می‌شود.

    از مینیمم فاصله‌ها استفاده می‌شود نه اولین فاصله: اگر صرافی وسط سری یک
    کندل جا انداخته باشد، اولین فاصله ممکن است دو برابر واقعی باشد.
    """
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    return min(gaps) if gaps else 0


def _trend_lookup(trend_df: pd.DataFrame, exec_times: list, method: str, ema_length: int):
    """تابعی می‌سازد که برای زمانِ باز شدن هر کندل اجرا، جهت روندِ آخرین کندلِ
    *بسته‌شده‌ی* تایم‌فریم روند را برمی‌گرداند.

    نگاه به آینده این‌جا خطر واقعی است: کندل ۴ ساعته‌ای که ساعت ۱۲ باز شده تا
    ساعت ۱۶ بسته نمی‌شود، پس سیگنالِ ساعت ۱۳ حق ندارد آن را ببیند. سیگنال روی
    close کندل اجرا ساخته می‌شود، یعنی در لحظه‌ی `t + exec_step`؛ کندل روند
    وقتی در دسترس است که `t_trend + trend_step` از آن نگذشته باشد. این دقیقاً
    همان چیزی است که موتور زنده می‌بیند (درایور کندل بازِ آخر را دور می‌ریزد).
    """
    series = trend_mod.trend_series(trend_df, method, ema_length)
    times = [int(x) for x in trend_df["time"].tolist()]
    dirs = [str(x) for x in series.tolist()]
    trend_step = _bar_step(times)
    exec_step = _bar_step(exec_times)
    state = {"i": -1}

    def lookup(t: int) -> str:
        available_until = t + exec_step
        # کرسر یک‌طرفه: بک‌تست زمان را صعودی می‌پیماید، پس جست‌وجوی دوباره لازم نیست
        i = state["i"]
        while i + 1 < len(times) and times[i + 1] + trend_step <= available_until:
            i += 1
        state["i"] = i
        if i < 0:
            return "unknown"
        return dirs[i]

    return lookup


def run_backtest(df: pd.DataFrame, strategy_key: str, params: dict | None = None,
                 invert: bool = False, sl_tp_atr_mult: float = DEFAULT_SL_TP_ATR_MULT,
                 reversal_policy: str = "none", trend_df: pd.DataFrame | None = None,
                 trend_method: str = trend_mod.DEFAULT_METHOD,
                 trend_ema_length: int = trend_mod.DEFAULT_EMA_LENGTH,
                 trend_timeframe: str = "") -> dict:
    """reversal_policy باید با تنظیم همان حساب یکی باشد؛ پیش‌فرضش هم مثل
    پیش‌فرض حساب‌هاست تا بک‌تستِ بدون پارامتر، رفتار واقعی ربات را نشان بدهد."""
    if df is None or len(df) < WARMUP + 20:
        raise ValueError("داده‌ی کندل برای بک‌تست کافی نیست (حداقل ~۲۳۰ کندل).")

    trend_at = None
    if trend_df is not None and len(trend_df) >= trend_mod.MIN_CANDLES:
        trend_at = _trend_lookup(trend_df, [int(x) for x in df["time"].tolist()],
                                 trend_method, trend_ema_length)
    trend_skipped = 0

    atr_series = ind.atr(df, 14)
    equity = START_EQUITY
    total_fees = 0.0
    peak = equity
    max_dd_pct = 0.0
    position = None          # {side, entry, qty, sl, tp}
    trades: list[dict] = []
    curve: list[dict] = []

    def close_position(price, when, closed_by):
        nonlocal equity, position, peak, max_dd_pct, total_fees
        direction = 1 if position["side"] == "long" else -1
        gross = (price - position["entry"]) * direction * position["qty"]
        exit_fee = abs(price * position["qty"]) * TAKER_FEE_RATE
        fee = position.get("entry_fee", 0.0) + exit_fee
        total_fees += fee
        pnl = gross - fee
        equity += pnl
        trades.append({
            "side": position["side"], "entry": position["entry"], "exit": price,
            "pnl": pnl, "closed_by": closed_by, "time": int(when),
        })
        position = None
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak else 0
        if dd > max_dd_pct:
            max_dd_pct = dd

    for i in range(WARMUP, len(df)):
        window = df.iloc[: i + 1]
        row = df.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        when = int(row["time"])

        # ۱) چک SL/TP پوزیشن باز با high/low همین کندل
        if position is not None:
            if position["side"] == "long":
                if low <= position["sl"]:
                    close_position(position["sl"], when, "SL")
                elif high >= position["tp"]:
                    close_position(position["tp"], when, "TP")
            else:
                if high >= position["sl"]:
                    close_position(position["sl"], when, "SL")
                elif low <= position["tp"]:
                    close_position(position["tp"], when, "TP")

        # ۲) سیگنال استراتژی روی کندل بسته‌شده
        try:
            sig = run_strategy(strategy_key, window, params)
        except Exception:
            sig = {"signal": "none"}

        # استراتژی می‌تواند قانون خروج خودش را داشته باشد (سیگنال close) —
        # دقیقاً مثل موتور واقعی، و روی همین کندل بسته می‌شود.
        if sig["signal"] == "close" and position is not None:
            close_position(close, when, "strategy")

        if sig["signal"] in ("buy", "sell"):
            side = sig["signal"]
            if invert:
                side = "sell" if side == "buy" else "buy"
            # فیلتر روند دقیقاً مثل موتور واقعی: بعد از معکوس‌کردن، و فقط روی
            # ورود — پوزیشن باز به حد ضرر/سود خودش سپرده می‌شود.
            blocked = trend_at is not None and not trend_mod.side_matches(trend_at(when), side)
            if blocked:
                trend_skipped += 1
            wanted = "long" if side == "buy" else "short"
            if not blocked and position is not None and position["side"] != wanted:
                # دقیقاً همان سیاستی که موتور واقعی اعمال می‌کند، وگرنه نتیجه‌ی
                # بک‌تست با رفتار ربات یکی نیست. (تا پیش از این، بک‌تست همیشه
                # معکوس می‌کرد در حالی که موتور فقط پوزیشن سودده را می‌بست.)
                if reversal_policy == "always":
                    close_position(close, when, "reversal")
                elif reversal_policy == "profitable":
                    direction = 1 if position["side"] == "long" else -1
                    floating = (close - position["entry"]) * direction * position["qty"]
                    if floating > 0:
                        close_position(close, when, "reversal")
            if not blocked and position is None:
                atr_v = atr_series.iat[i]
                # حد ضرر/سودِ خودِ استراتژی مقدم است — دقیقاً مثل موتور واقعی.
                # در حالت معکوس حول قیمت ورود آینه می‌شود، باز هم مثل موتور.
                sl, tp = sig.get("stop_loss"), sig.get("take_profit")
                if sl and tp and invert:
                    sl, tp = 2 * close - sl, 2 * close - tp
                if sl and tp and (sl <= 0 or tp <= 0):
                    sl = tp = None
                if not (sl and tp) and pd.notna(atr_v) and atr_v > 0:
                    dist = sl_tp_atr_mult * atr_v
                    sl = close - dist if wanted == "long" else close + dist
                    tp = close + dist if wanted == "long" else close - dist
                if sl and tp and abs(close - sl) > 0:
                    risk_amount = equity * RISK_PCT / 100
                    qty = risk_amount / abs(close - sl)
                    # کارمزد ورود همین‌جا حساب می‌شود اما از اکوییتی کم نمی‌شود؛
                    # هنگام بستن، ورود و خروج با هم در fee لحاظ می‌شوند تا دوبار
                    # کسر نشود.
                    entry_fee = abs(close * qty) * TAKER_FEE_RATE
                    position = {"side": wanted, "entry": close, "qty": qty, "sl": sl, "tp": tp,
                                "entry_fee": entry_fee}

        # ۳) نقطه‌ی منحنی اکوییتی (مارک‌تو‌مارکت)
        floating = 0.0
        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            floating = (close - position["entry"]) * direction * position["qty"]
        curve.append({"time": when, "equity": equity + floating})

    # پوزیشن باز مانده در انتها با قیمت آخر بسته می‌شود
    if position is not None:
        close_position(float(df["close"].iat[-1]), int(df["time"].iat[-1]), "end")

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    n = len(pnls)
    return {
        "summary": {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n * 100) if n else 0.0,
            "total_pnl": sum(pnls),
            "return_pct": (equity - START_EQUITY) / START_EQUITY * 100,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit == 0 else float("inf")),
            "max_drawdown_pct": max_dd_pct,
            "start_equity": START_EQUITY,
            "end_equity": equity,
            "total_fees": total_fees,
            "inverted": bool(invert),
            "reversal_policy": reversal_policy,
            # فیلتر روند: چند سیگنال به‌خاطر خلاف‌جهت بودن اجرا نشد
            "trend_filter": bool(trend_at is not None),
            "trend_timeframe": trend_timeframe,
            "trend_method": trend_method,
            "trend_skipped": trend_skipped,
            # میانگین برد و باخت و نسبتشان: نرخ برد به‌تنهایی گمراه‌کننده است.
            # با نرخ برد W، سربه‌سر شدن نیاز دارد نسبت ≥ (1-W)/W باشد.
            "avg_win": (gross_profit / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "win_loss_ratio": ((gross_profit / len(wins)) / (gross_loss / len(losses)))
                              if wins and losses else None,
        },
        "equity_curve": curve,
        "trades": trades[-50:],
    }
