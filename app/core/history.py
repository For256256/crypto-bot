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
            "fee": trade.get("fee"),
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
                      current_balance: float | None = None,
                      contributed: float | None = None) -> dict | None:
    """خلاصه‌ی وضعیت مالی یک حساب برای نمایش در بالای داشبورد.

    سود از روی *معاملات* حساب می‌شود، نه از تفاضل اکوییتی. نسخه‌ی قبلی
    `equity_now - equity_start` را سود می‌گرفت؛ یعنی هر واریز به حساب صرافی
    مستقیماً به‌عنوان سود ثبت می‌شد و هر برداشت به‌عنوان زیان. روی حساب واقعی
    این هم سود روزانه و هم درصد بازده کلی را به‌کلی بی‌معنا می‌کرد.

    چون `realized` هر معامله از قبل خالصِ کارمزد است، این رابطه برقرار است:

        موجودی = سرمایه‌ی واریزشده + مجموع سود محقق‌شده

    پس «سرمایه‌ی واریزشده» = موجودی فعلی − مجموع realized، که خودبه‌خود
    واریز را اضافه و برداشت را کم می‌کند؛ بدون نیاز به ثبت دستی یا حدس‌زدن
    از روی جهش موجودی.
    """
    with _lock:
        data = _load()

    points = sorted(
        [e for e in data["equity"] if e.get("account_id") == account_id and e.get("mode") == mode],
        key=lambda e: str(e.get("time") or ""),
    )
    trades = [t for t in data["trades"]
              if t.get("account_id") == account_id and t.get("mode") == mode]

    if current_equity is None:
        if not points and not trades:
            return None
        if points:
            current_equity = _num(points[-1].get("equity"))
            if current_balance is None:
                current_balance = _num(points[-1].get("balance"), current_equity)
        else:
            current_equity = 0.0
    else:
        current_equity = _num(current_equity)
    current_balance = current_equity if current_balance is None else _num(current_balance)

    realized_all = sum(_num(t.get("realized")) for t in trades)
    # اگر جمع واریز/برداشت از خود دفتر صرافی خوانده شده باشد، همان مرجع است.
    # محاسبه‌ی جایگزین (موجودی منهای سود) فقط وقتی دقیق است که هر تغییر موجودی
    # معامله‌ای پشتش باشد؛ روی حساب واقعی کارمزد فاندینگ و اختلاف کارمزد
    # تخمینی این رابطه را کم‌کم به هم می‌زنند و عدد سرمایه می‌لغزد.
    from_exchange = contributed is not None
    contributed = _num(contributed) if from_exchange else (current_balance - realized_all)
    contributed_source = "exchange" if from_exchange else "derived"
    unrealized_now = current_equity - current_balance

    overall_profit = realized_all + unrealized_now
    overall_profit_pct = (overall_profit / contributed * 100) if contributed else 0.0

    # ---- روزانه ----
    today = _now_iso()[:10]
    realized_today = sum(_num(t.get("realized")) for t in trades
                         if str(t.get("close_time") or "")[:10] == today)
    today_points = [p for p in points if str(p.get("time") or "")[:10] == today]
    if today_points:
        eq0 = _num(today_points[0].get("equity"))
        bal0 = _num(today_points[0].get("balance"), eq0)
        unrealized_start = eq0 - bal0
        daily_base = bal0
    else:
        # اولین نقطه‌ی امروز هنوز ثبت نشده — تغییر شناور امروز صفر فرض می‌شود.
        unrealized_start = unrealized_now
        daily_base = contributed

    daily_pnl = realized_today + (unrealized_now - unrealized_start)
    daily_pnl_pct = (daily_pnl / daily_base * 100) if daily_base else 0.0

    return {
        "initial_balance": contributed,
        "current_equity": current_equity,
        "current_balance": current_balance,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "overall_profit": overall_profit,
        "overall_profit_pct": overall_profit_pct,
        "realized_total": realized_all,
        "unrealized": unrealized_now,
        "contributed_source": contributed_source,
    }


def _flow_adjusted(equity_points: list, trades: list) -> list:
    """منحنی اکوییتی با اثر واریز/برداشت برداشته‌شده.

    اکوییتی خام دو چیز را با هم نشان می‌دهد: نتیجه‌ی معاملات، و پولی که کاربر
    وارد یا خارج کرده. برای سنجش عملکرد فقط اولی معنا دارد — روی منحنی خام
    یک واریز، پله‌ی رو به بالا می‌سازد که شبیه سود است و یک برداشت، افتی که
    شبیه ضرر است.

    جریان بیرونی بین دو نقطه = تغییر موجودی منهای سود محقق‌شده‌ی همان بازه.
    هر جریان از آن نقطه به بعد از منحنی کم می‌شود، پس شکل منحنی فقط اثر
    معاملات را نشان می‌دهد. مقدار خام و خودِ جریان هم برگردانده می‌شوند تا
    نمودار بتواند لحظه‌ی واریز/برداشت را علامت بزند.
    """
    by_time = sorted(trades, key=lambda t: str(t.get("close_time") or ""))
    ti = 0
    cum_realized = prev_cum = offset = 0.0
    prev_balance = None
    out = []
    for e in equity_points:
        point_time = str(e.get("time") or "")
        while ti < len(by_time) and str(by_time[ti].get("close_time") or "") <= point_time:
            cum_realized += _num(by_time[ti].get("realized"))
            ti += 1
        eq = _num(e.get("equity"))
        bal = _num(e.get("balance"), eq)
        flow = 0.0
        if prev_balance is not None:
            flow = (bal - prev_balance) - (cum_realized - prev_cum)
            offset += flow
        out.append({
            "time": e.get("time"),
            "equity": eq - offset,     # همان چیزی که نمودار می‌کشد
            "raw_equity": eq,
            "balance": bal,
            "flow": flow,
        })
        prev_balance, prev_cum = bal, cum_realized
    return out


def _merge_curves(curves: list) -> list:
    """چند منحنی تعدیل‌شده را روی یک محور زمانی مشترک جمع می‌کند.

    هر حساب نقطه‌های اکوییتی خودش را در لحظه‌های متفاوتی ثبت می‌کند، پس
    نمی‌شود نقطه‌ها را ساده به هم چسباند. برای هر زمان در اجتماع زمان‌ها،
    آخرین مقدار شناخته‌شده‌ی هر حساب برداشته و جمع می‌شود (forward-fill).

    دو نکته‌ی ظریف:
    - حسابی که هنوز اولین نقطه‌اش نرسیده، با همان مقدار اولش پر می‌شود (نه با
      صفر). اگر صفر می‌گذاشتیم، لحظه‌ی پیوستن حسابِ دوم یک پله‌ی بزرگ رو به
      بالا در منحنی می‌ساخت و دقیقاً مثل سود دیده می‌شد — همان اشتباهی که
      برای واریز/برداشت اصلاح شده بود. با این کار سرمایه‌ی هر حساب از ابتدای
      بازه در مبنا هست، پس درصد بازده روی کل سرمایه‌ی واقعی حساب می‌شود.
    - flow (واریز/برداشت) رویداد است نه موجودی، پس forward-fill نمی‌شود؛
      فقط جریان‌های دقیقاً همان لحظه جمع می‌شوند وگرنه چند بار شمرده می‌شد.
    """
    curves = [c for c in curves if c]
    if not curves:
        return []
    if len(curves) == 1:
        return curves[0]

    times = sorted({str(p["time"]) for c in curves for p in c})
    cursors = [0] * len(curves)
    last = [c[0] for c in curves]      # پیش از شروع هر حساب، مقدار اولش
    out = []
    for t in times:
        equity = raw = balance = flow = 0.0
        for i, curve in enumerate(curves):
            while cursors[i] < len(curve) and str(curve[cursors[i]]["time"]) <= t:
                point = curve[cursors[i]]
                last[i] = point
                if str(point["time"]) == t:
                    flow += _num(point.get("flow"))
                cursors[i] += 1
            equity += _num(last[i].get("equity"))
            raw += _num(last[i].get("raw_equity"))
            balance += _num(last[i].get("balance"))
        out.append({"time": t, "equity": equity, "raw_equity": raw,
                    "balance": balance, "flow": flow})
    return out


def get_report(account_id, days: int = 30, mode: str | None = None) -> dict:
    """
    گزارش سود/زیان یک حساب — یا مجموع چند حساب اگر لیستی از شناسه‌ها بدهید:
    - summary: آمار کلی (PnL خالص، نرخ برد، profit factor، ماکس دراوداون و ...)
    - equity_curve: نقاط منحنی اکوییتی
    - daily: سود/زیان روزانه‌ی معاملات بسته‌شده
    - trades: آخرین معاملات (جدیدترین اول)
    - accounts: سهم هر حساب (فقط در حالت چندحسابی)
    """
    # «چندحسابی» از روی نوع ورودی تشخیص داده می‌شود نه تعداد: کاربری که فقط
    # یک حساب واقعی دارد هم باید سطر سهم همان یک حساب را ببیند.
    multi = not isinstance(account_id, str)
    account_ids = list(account_id) if multi else [account_id]
    id_set = set(account_ids)
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
              if t.get("account_id") in id_set and _mode_ok(t.get("mode")) and _in_range(t.get("close_time"))]
    trades.sort(key=lambda t: str(t.get("close_time") or ""))

    equity_points = [e for e in data["equity"]
                     if e.get("account_id") in id_set and _mode_ok(e.get("mode")) and _in_range(e.get("time"))]
    equity_points.sort(key=lambda e: str(e.get("time") or ""))

    # ---------- آمار کلی ----------
    pnls = [_num(t.get("realized")) for t in trades]
    fees = [_num(t.get("fee")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(pnls)
    total_fees = sum(fees)
    n = len(pnls)
    # None هم برای «بدون معامله» و هم برای «فقط برد، بدون باخت» (profit factor
    # بی‌نهایت) استفاده می‌شود — فرانت‌اند هر دو را جدا از تعداد معاملات (s.trades)
    # درست نمایش می‌دهد. float('inf') اینجا استفاده نمی‌شود چون JSON استاندارد آن
    # را قبول ندارد و پاسخ API را کرش می‌کند.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0

    # ---------- ریز جزئیات به تفکیک نماد ----------
    by_symbol_map: dict[str, dict] = {}
    for t, r, f in zip(trades, pnls, fees):
        sym = t.get("symbol") or "—"
        s = by_symbol_map.setdefault(sym, {"symbol": sym, "trades": 0, "wins": 0, "losses": 0,
                                           "pnl": 0.0, "fees": 0.0})
        s["trades"] += 1
        s["pnl"] += r
        s["fees"] += f
        if r > 0:
            s["wins"] += 1
        elif r < 0:
            s["losses"] += 1
    by_symbol = sorted(by_symbol_map.values(), key=lambda s: s["pnl"])

    # ---------- ماکس دراوداون، روی منحنی تعدیل‌شده ----------
    # تعدیل واریز/برداشت باید حساب‌به‌حساب انجام شود (موجودی هر حساب مال
    # خودش است)، و تازه بعد از آن منحنی‌ها با هم جمع شوند.
    if not multi:
        adjusted_curve = _flow_adjusted(equity_points, trades)
    else:
        per_account = []
        for aid in account_ids:
            pts = [e for e in equity_points if e.get("account_id") == aid]
            if not pts:
                continue
            per_account.append(_flow_adjusted(pts, [t for t in trades if t.get("account_id") == aid]))
        adjusted_curve = _merge_curves(per_account)

    # ---------- بازده همین بازه‌ی انتخاب‌شده ----------
    # درصدی که تا حالا نشان داده می‌شد، بازده «کل عمر حساب» بود و به فیلتر
    # ۷/۳۰/۹۰ روز کاری نداشت. این یکی از دو سر همان منحنی تعدیل‌شده حساب
    # می‌شود، پس هم واریز/برداشت را نادیده می‌گیرد و هم سود شناور را در بر
    # می‌گیرد؛ مبنا هم سرمایه‌ی ابتدای همان بازه است، نه سرمایه‌ی روز اول.
    period_pnl = None
    period_return_pct = None
    if len(adjusted_curve) >= 2:
        first_eq = adjusted_curve[0]["equity"]
        last_eq = adjusted_curve[-1]["equity"]
        period_pnl = last_eq - first_eq
        if first_eq:
            period_return_pct = period_pnl / first_eq * 100

    max_dd_pct = 0.0
    peak = None
    for point in adjusted_curve:
        eq = point["equity"]
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

    # ---------- سهم هر حساب (وقتی گزارش برای مجموعه‌ای از حساب‌ها گرفته شده) ----------
    per_account_rows = None
    if multi:
        rows = {aid: {"account_id": aid, "trades": 0, "wins": 0, "losses": 0,
                      "pnl": 0.0, "fees": 0.0} for aid in account_ids}
        for t, r, f in zip(trades, pnls, fees):
            row = rows.get(t.get("account_id"))
            if row is None:
                continue
            row["trades"] += 1
            row["pnl"] += r
            row["fees"] += f
            if r > 0:
                row["wins"] += 1
            elif r < 0:
                row["losses"] += 1
        per_account_rows = sorted(rows.values(), key=lambda x: x["pnl"], reverse=True)

    return {
        "accounts": per_account_rows,
        "summary": {
            "total_pnl": total,
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n * 100) if n else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "total_fees": total_fees,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "best": max(pnls) if pnls else 0.0,
            "worst": min(pnls) if pnls else 0.0,
            "avg": (total / n) if n else 0.0,
            "max_drawdown_pct": max_dd_pct,
            "period_pnl": period_pnl,
            "period_return_pct": period_return_pct,
            "has_estimated": any(t.get("estimated") for t in trades),
        },
        "equity_curve": adjusted_curve,
        "daily": daily,
        "by_symbol": by_symbol,
        "trades": list(reversed(trades[-200:])),
    }
