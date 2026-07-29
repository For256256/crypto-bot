"""
بک‌تست ساده‌ی استراتژی‌ها روی کندل‌های تاریخی — برای صفحه‌ی «مدیریت استراتژی».

روش: از کندل warmup به بعد، روی هر کندل بسته‌شده استراتژی اجرا می‌شود (بدون
نگاه به آینده). هر سیگنال buy/sell یک پوزیشن باز/معکوس می‌کند. SL/TP مانند
ربات واقعی از ATR ساخته می‌شود (۱.۵× و ۳×) و در کندل‌های بعدی با high/low
چک می‌شود. حجم هر معامله ثابت فرض می‌شود (یک واحد) تا نتایج خام استراتژی
دیده شود؛ آمار خروجی: نرخ برد، PnL، Profit Factor، ماکس دراوداون، منحنی اکوییتی.
"""
import pandas as pd

from app.core.strategies import indicators as ind
from app.core.strategies.registry import run_strategy

WARMUP = 60
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0
START_EQUITY = 10000.0
RISK_PCT = 1.0  # ٪ریسک هر معامله از اکوییتی جاری — مثل ربات واقعی


def run_backtest(df: pd.DataFrame, strategy_key: str, params: dict | None = None) -> dict:
    if df is None or len(df) < WARMUP + 10:
        raise ValueError("داده‌ی کندل برای بک‌تست کافی نیست (حداقل ~۷۰ کندل).")

    atr_series = ind.atr(df, 14)
    equity = START_EQUITY
    peak = equity
    max_dd_pct = 0.0
    position = None          # {side, entry, qty, sl, tp}
    trades: list[dict] = []
    curve: list[dict] = []

    def close_position(price, when, closed_by):
        nonlocal equity, position, peak, max_dd_pct
        direction = 1 if position["side"] == "long" else -1
        pnl = (price - position["entry"]) * direction * position["qty"]
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
            wanted = "long" if sig["signal"] == "buy" else "short"
            if position is not None and position["side"] != wanted:
                close_position(close, when, "reversal")
            if position is None:
                atr_v = atr_series.iat[i]
                if pd.notna(atr_v) and atr_v > 0:
                    sl = close - SL_ATR_MULT * atr_v if wanted == "long" else close + SL_ATR_MULT * atr_v
                    tp = close + TP_ATR_MULT * atr_v if wanted == "long" else close - TP_ATR_MULT * atr_v
                    risk_amount = equity * RISK_PCT / 100
                    qty = risk_amount / abs(close - sl)
                    position = {"side": wanted, "entry": close, "qty": qty, "sl": sl, "tp": tp}

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
        },
        "equity_curve": curve,
        "trades": trades[-50:],
    }
