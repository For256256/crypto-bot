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
"""
import pandas as pd

from app.core.exchanges.toobit import TAKER_FEE_RATE
from app.core.strategies import indicators as ind
from app.core.strategies.registry import run_strategy

# چند استراتژی EMA۲۰۰ دارند. با warmup=۶۰ آن EMA هنوز همگرا نشده و سیگنال‌های
# اول بک‌تست روی مقداری بی‌معنا گرفته می‌شدند — یعنی نتیجه‌ی بک‌تست با رفتار
# واقعی ربات (که ۵۰۰ کندل می‌گیرد) یکی نبود. warmup باید از بلندترین دوره‌ی
# اندیکاتورها بیشتر باشد.
WARMUP = 210
DEFAULT_SL_TP_ATR_MULT = 3.0   # متقارن، مثل موتور واقعی
START_EQUITY = 10000.0
RISK_PCT = 1.0  # ٪ریسک هر معامله از اکوییتی جاری — مثل ربات واقعی


def run_backtest(df: pd.DataFrame, strategy_key: str, params: dict | None = None,
                 invert: bool = False, sl_tp_atr_mult: float = DEFAULT_SL_TP_ATR_MULT,
                 reversal_policy: str = "none") -> dict:
    """reversal_policy باید با تنظیم همان حساب یکی باشد؛ پیش‌فرضش هم مثل
    پیش‌فرض حساب‌هاست تا بک‌تستِ بدون پارامتر، رفتار واقعی ربات را نشان بدهد."""
    if df is None or len(df) < WARMUP + 20:
        raise ValueError("داده‌ی کندل برای بک‌تست کافی نیست (حداقل ~۲۳۰ کندل).")

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

        if sig["signal"] in ("buy", "sell"):
            side = sig["signal"]
            if invert:
                side = "sell" if side == "buy" else "buy"
            wanted = "long" if side == "buy" else "short"
            if position is not None and position["side"] != wanted:
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
            if position is None:
                atr_v = atr_series.iat[i]
                if pd.notna(atr_v) and atr_v > 0:
                    dist = sl_tp_atr_mult * atr_v
                    sl = close - dist if wanted == "long" else close + dist
                    tp = close + dist if wanted == "long" else close - dist
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
