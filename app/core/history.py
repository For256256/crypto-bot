"""
تاریخچه‌ی معاملات بسته‌شده و نقاط اکوییتی هر حساب — مبنای گزارش سود/زیان داشبورد.
همه‌چیز در یک فایل JSON (HISTORY_PATH) نگه داشته می‌شود:
{
  "trades":  [ {account_id, mode, symbol, side, qty, entry_price, close_price,
                realized, closed_by, open_time, close_time, estimated} ],
  "equity":  [ {account_id, mode, time, equity, balance} ]
}
"""
import json
import os
import threading
from datetime import datetime, timezone, timedelta

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             os.pardir, "config", "history.json")
HISTORY_PATH = os.getenv("HISTORY_PATH") or os.path.abspath(_DEFAULT_PATH)

_lock = threading.Lock()

# سقف نگه‌داری رکوردها برای جلوگیری از بزرگ‌شدن بی‌رویه‌ی فایل
MAX_TRADES_PER_ACCOUNT = 2000
MAX_EQUITY_POINTS_PER_ACCOUNT = 20000  # ~۷۰ روز با نمونه‌ی ۵ دقیقه‌ای


def _load() -> dict:
    if not os.path.exists(HISTORY_PATH):
        return {"trades": [], "equity": []}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("trades", [])
        data.setdefault("equity", [])
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        return {"trades": [], "equity": []}


def _save(data: dict):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v, default: float = 0.0) -> float:
    """تبدیل امن به float — رکورد خراب/غیرعددی گزارش را کرش نمی‌کند."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def record_trade(account_id: str, mode: str, trade: dict):
    """ثبت یک معامله‌ی بسته‌شده (paper یا live)."""
    with _lock:
        data = _load()
        data["trades"].append({
            "account_id": account_id,
            "mode": mode,
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "qty": trade.get("qty"),
            "entry_price": trade.get("entry_price"),
            "close_price": trade.get("close_price"),
            "realized": trade.get("realized"),
            "closed_by": trade.get("closed_by", "unknown"),
            "open_time": trade.get("open_time"),
            "close_time": trade.get("close_time") or _now_iso(),
            "estimated": bool(trade.get("estimated", False)),
        })
        # هرس رکوردهای قدیمی همین حساب
        mine = [t for t in data["trades"] if t.get("account_id") == account_id]
        if len(mine) > MAX_TRADES_PER_ACCOUNT:
            extra = len(mine) - MAX_TRADES_PER_ACCOUNT
            keep_ids = set()
            count = 0
            for t in data["trades"]:
                if t.get("account_id") == account_id and count < extra:
                    keep_ids.add(id(t))
                    count += 1
            data["trades"] = [t for t in data["trades"] if id(t) not in keep_ids]
        _save(data)


def record_equity(account_id: str, mode: str, equity: float, balance: float):
    """ثبت یک نقطه از منحنی اکوییتی (engine هر ۵ دقیقه صدا می‌زند)."""
    with _lock:
        data = _load()
        data["equity"].append({
            "account_id": account_id,
            "mode": mode,
            "time": _now_iso(),
            "equity": equity,
            "balance": balance,
        })
        mine = [e for e in data["equity"] if e.get("account_id") == account_id]
        if len(mine) > MAX_EQUITY_POINTS_PER_ACCOUNT:
            extra = len(mine) - MAX_EQUITY_POINTS_PER_ACCOUNT
            keep_ids = set()
            count = 0
            for e in data["equity"]:
                if e.get("account_id") == account_id and count < extra:
                    keep_ids.add(id(e))
                    count += 1
            data["equity"] = [e for e in data["equity"] if id(e) not in keep_ids]
        _save(data)


def reset_account(account_id: str) -> dict:
    """تمام تاریخچه‌ی معاملات و نقاط اکوییتی یک حساب را پاک می‌کند تا از همین
    لحظه گزارش‌ها مثل یک حساب خام از نو ثبت شوند. سایر حساب‌ها دست‌نخورده می‌مانند."""
    with _lock:
        data = _load()
        trades_before = len(data["trades"])
        equity_before = len(data["equity"])
        data["trades"] = [t for t in data["trades"] if t.get("account_id") != account_id]
        data["equity"] = [e for e in data["equity"] if e.get("account_id") != account_id]
        _save(data)
        return {
            "trades_removed": trades_before - len(data["trades"]),
            "equity_removed": equity_before - len(data["equity"]),
        }


def get_account_stats(account_id: str, mode: str, current_equity: float | None = None,
                      current_balance: float | None = None) -> dict | None:
    """خلاصه‌ی وضعیت مالی یک حساب برای نمایش در بالای داشبورد:
    مبلغ اولیه، سود/زیان روزانه و درصد سود کلی — بر پایه‌ی منحنی اکوییتی
    ثبت‌شده‌ی همان حالت (paper/live). با پاکسازی/ریست حساب، این منحنی خالی
    می‌شود و مبلغ اولیه از همان لحظه (equity/balance فعلی) از نو محاسبه می‌شود.
    اگر نه داده‌ی زنده‌ای در دسترس باشد و نه تاریخچه‌ای ثبت شده، None برمی‌گرداند."""
    with _lock:
        data = _load()
    points = sorted(
        [e for e in data["equity"] if e.get("account_id") == account_id and e.get("mode") == mode],
        key=lambda e: str(e.get("time") or ""),
    )
    if current_equity is None:
        if not points:
            return None
        current_equity = _num(points[-1].get("equity"))
        current_balance = _num(points[-1].get("balance"), current_equity)
    else:
        current_equity = _num(current_equity)
    if current_balance is None:
        current_balance = current_equity
    else:
        current_balance = _num(current_balance)

    initial_balance = _num(points[0].get("balance"), current_balance) if points else current_balance

    today = _now_iso()[:10]
    today_points = [p for p in points if str(p.get("time") or "")[:10] == today]
    daily_start_equity = _num(today_points[0].get("equity"), current_equity) if today_points else current_equity
    daily_pnl = current_equity - daily_start_equity
    daily_pnl_pct = (daily_pnl / daily_start_equity * 100) if daily_start_equity else 0.0

    overall_profit = current_equity - initial_balance
    overall_profit_pct = (overall_profit / initial_balance * 100) if initial_balance else 0.0

    return {
        "initial_balance": initial_balance,
        "current_equity": current_equity,
        "current_balance": current_balance,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "overall_profit": overall_profit,
        "overall_profit_pct": overall_profit_pct,
    }


def get_report(account_id: str, days: int = 30, mode: str | None = None) -> dict:
    """
    گزارش سود/زیان یک حساب:
    - summary: آمار کلی (PnL خالص، نرخ برد، profit factor، ماکس دراوداون و ...)
    - equity_curve: نقاط منحنی اکوییتی
    - daily: سود/زیان روزانه‌ی معاملات بسته‌شده
    - trades: آخرین معاملات (جدیدترین اول)
    """
    with _lock:
        data = _load()

    cutoff = None
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    def _in_range(ts) -> bool:
        return cutoff is None or (isinstance(ts, str) and ts >= cutoff)

    def _mode_ok(m) -> bool:
        return mode is None or m == mode

    trades = [t for t in data["trades"]
              if t.get("account_id") == account_id and _mode_ok(t.get("mode")) and _in_range(t.get("close_time"))]
    trades.sort(key=lambda t: str(t.get("close_time") or ""))

    equity_points = [e for e in data["equity"]
                     if e.get("account_id") == account_id and _mode_ok(e.get("mode")) and _in_range(e.get("time"))]
    equity_points.sort(key=lambda e: str(e.get("time") or ""))

    # ---------- آمار کلی ----------
    pnls = [_num(t.get("realized")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(pnls)
    n = len(pnls)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit == 0 else float("inf"))

    # ---------- ماکس دراوداون از روی منحنی اکوییتی ----------
    max_dd_pct = 0.0
    peak = None
    for e in equity_points:
        eq = _num(e.get("equity"))
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd_pct:
                max_dd_pct = dd

    # ---------- تجمیع روزانه ----------
    daily_map: dict[str, dict] = {}
    for t in trades:
        day = str(t.get("close_time") or "")[:10]
        if not day:
            continue
        d = daily_map.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        r = _num(t.get("realized"))
        d["pnl"] += r
        d["trades"] += 1
        if r > 0:
            d["wins"] += 1
    daily = [daily_map[k] for k in sorted(daily_map)]

    return {
        "summary": {
            "total_pnl": total,
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n * 100) if n else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "best": max(pnls) if pnls else 0.0,
            "worst": min(pnls) if pnls else 0.0,
            "avg": (total / n) if n else 0.0,
            "max_drawdown_pct": max_dd_pct,
            "has_estimated": any(t.get("estimated") for t in trades),
        },
        "equity_curve": [{"time": e.get("time"), "equity": e.get("equity"), "balance": e.get("balance")}
                         for e in equity_points],
        "daily": daily,
        "trades": list(reversed(trades[-200:])),
    }
