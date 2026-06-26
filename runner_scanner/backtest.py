"""مُختبِر تاريخي (Backtester) — يعيد تشغيل البوت على الماضي لقياس الحافة.

الفكرة: بدل انتظار أسابيع من البيانات الحيّة، نعيد تمثيل أيام تداول ماضية عبر
**نفس** خط المعالجة (process_candidate) ونقيس النتائج (نجاح/خسارة/بلا حسم).

⚠️ **مبدأ حاسم — لا تسرّب مستقبل (no-lookahead):** عند تقييم مرشّح في لحظة T من
يوم ماضٍ، لا يرى الكود إلا بيانات **حتى T** (يومي قبل اليوم · شموع حتى T · أخبار
حتى T). النتيجة تُقاس من شموع **بعد T** فقط. أي خرق لهذا يعطي نتائج متفائلة كاذبة.

النطاق (v1): يختبر **الاستراتيجية الفنية الأساسية** (كشف + بوّابات + ركيزتان +
وقف/أهداف). يتخطّى طبقات Claude/SEC/الشورت (تُقيَّم حيًّا). تقريبات موثّقة:
- «أعلى N» يُقرَّب بأعلى N صعودًا في قمة اليوم (proxy لـ top-gainers اللحظي).
- نوع الورقة/الفلوت من الحاضر (نادرًا يتغيّران) — best-effort.
- داخل الشمعة: لو لمست الهدف والوقف معًا نَعُدّها **خسارة** (تحفّظ ضد التفاؤل).

التشغيل: python -m runner_scanner.backtest --start 2025-01-02 --end 2025-01-31
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import detector, market_calendar
from .config import Config
from .massive_client import MassiveClient
from .models import Bar, SnapshotEntry
from .pipeline import process_candidate
from .sessions import ET, classify_session

logger = logging.getLogger(__name__)


# ── محوّل «حتى لحظة T» (يمنع تسرّب المستقبل) ──────────────────────
class AsOfClient:
    """يغلّف MassiveClient ويقصّ كل البيانات حتى لحظة الباكتيست (asof_ms).

    يومي: قبل يوم الباكتيست حصرًا. شموع اليوم: حتى asof. أخبار: حتى asof.
    overview/float: من الحاضر (best-effort، نادرًا يتغيّران).
    """

    def __init__(self, base: MassiveClient, date_str: str, asof_ms: int,
                 bars_5min: list[Bar], bars_1min: list[Bar],
                 static_cache: dict):
        self._base = base
        self._date = date_str
        self._asof = asof_ms
        self._5 = bars_5min        # مقصوصة مسبقًا حتى asof
        self._1 = bars_1min
        self._static = static_cache

    def _cached(self, key, fetch):
        if key not in self._static:
            self._static[key] = fetch()
        return self._static[key]

    def ticker_overview(self, ticker):
        return self._cached(f"ov:{ticker}", lambda: self._base.ticker_overview(ticker))

    def float_endpoint(self, ticker):
        return self._cached(f"fl:{ticker}", lambda: self._base.float_endpoint(ticker))

    def bars_5min(self, ticker, start, end):
        return list(self._5)

    def bars_1min(self, ticker, start, end):
        return list(self._1)

    def bars_daily(self, ticker, start, end):
        # يومي قبل يوم الباكتيست حصرًا (لا شمعة اليوم) — مكاش لكل (سهم/تاريخ)
        bars = self._cached(
            f"d:{ticker}:{self._date}",
            lambda: self._base.bars_daily(ticker, start, self._date))
        return [b for b in bars if _bar_date(b) < self._date]

    def aggregates(self, ticker, multiplier, timespan, start, end, **kw):
        bars = self._cached(
            f"h:{ticker}:{self._date}",
            lambda: self._base.aggregates(ticker, multiplier, timespan,
                                          start, self._date, **kw))
        return [b for b in bars if b.t_ms <= self._asof]

    def latest_news(self, ticker, published_gte_utc, limit=5):
        lte = _iso_utc(datetime.fromtimestamp(self._asof / 1000, tz=timezone.utc))
        return self._base.latest_news(ticker, published_gte_utc, limit=limit,
                                      published_lte_utc=lte)


def _bar_date(b: Bar) -> str:
    return datetime.fromtimestamp(b.t_ms / 1000, tz=timezone.utc) \
        .astimezone(ET).strftime("%Y-%m-%d")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_snapshot(ticker: str, prev_close: float,
                    bars: list[Bar]) -> SnapshotEntry | None:
    """سنابشوت «حتى T» من شموع اليوم حتى لحظة الزناد."""
    if not bars or prev_close <= 0:
        return None
    last = bars[-1].c
    pv = sum(((b.h + b.l + b.c) / 3.0) * b.v for b in bars if b.v > 0)
    tv = sum(b.v for b in bars if b.v > 0)
    return SnapshotEntry(
        ticker=ticker, last_price=last, prev_close=prev_close,
        day_open=bars[0].o, day_high=max(b.h for b in bars),
        day_low=min(b.l for b in bars), day_volume=tv,
        day_vwap=(pv / tv if tv > 0 else 0.0),
        change_pct=(last - prev_close) / prev_close * 100.0,
        updated_ns=bars[-1].t_ms * 1_000_000)


# ── محاكاة النتيجة من شموع ما بعد الدخول ──────────────────────────
def simulate_outcome(entry: float, risk, post_bars: list[Bar],
                     asof_ms: int, window_min: float) -> tuple[str, float, float]:
    """يرجّع (result, max_gain%, max_draw%). تحفّظ: لمس الهدف+الوقف بنفس الشمعة=خسارة."""
    if not risk or not risk.targets or entry <= 0:
        return "timeout", 0.0, 0.0
    t1 = risk.targets[0]
    stop = risk.stop_price
    deadline = asof_ms + window_min * 60_000
    high = low = entry
    for b in post_bars:
        if b.t_ms > deadline:
            break
        high = max(high, b.h)
        low = min(low, b.l)
        hit_stop = stop and b.l <= stop
        hit_t1 = b.h >= t1
        if hit_stop:        # تحفّظ: الوقف أولًا حتى لو لمس الهدف بنفس الشمعة
            return "loss", (high - entry) / entry * 100.0, (low - entry) / entry * 100.0
        if hit_t1:
            return "win", (high - entry) / entry * 100.0, (low - entry) / entry * 100.0
    return "timeout", (high - entry) / entry * 100.0, (low - entry) / entry * 100.0


# ── تقويم أيام التداول ────────────────────────────────────────────
def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not market_calendar.is_holiday(d)


def trading_days(start: str, end: str) -> list[str]:
    d = date.fromisoformat(start)
    last = date.fromisoformat(end)
    out = []
    while d <= last:
        if _is_trading_day(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _prev_trading_day(day: str) -> str:
    d = date.fromisoformat(day) - timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def _prev_close_map(grouped: list[dict]) -> dict[str, float]:
    out = {}
    for r in grouped:
        t, c = r.get("T"), r.get("c")
        if t and c:
            out[t] = float(c)
    return out


def _day_candidates(cfg: Config, grouped: list[dict],
                    prev_close: dict[str, float]) -> list[tuple[str, float]]:
    """أعلى N صعودًا في قمة اليوم (proxy لـ top-gainers) — مرشّحو الباكتيست."""
    cands = []
    for r in grouped:
        t, h, c = r.get("T"), r.get("h"), r.get("c")
        pc = prev_close.get(t)
        if not t or not pc or pc <= 0 or not h:
            continue
        chg_high = (float(h) - pc) / pc * 100.0
        if chg_high < cfg.trigger_change_pct or chg_high > cfg.max_change_pct:
            continue
        last = float(c or h)
        if last < cfg.price_min or last > cfg.price_max:
            continue
        if cfg.filter_derivatives and detector.looks_like_derivative(t):
            continue
        cands.append((t, chg_high))
    cands.sort(key=lambda x: -x[1])
    return cands[:cfg.top_n_runners]


def simulate_day(cfg: Config, base: MassiveClient, day: str,
                 static_cache: dict) -> list[dict]:
    """يحاكي يوم تداول كاملًا → قائمة صفقات (تنبيهات مُحاكاة) بنتائجها."""
    prev = _prev_trading_day(day)
    grouped = base.grouped_daily(day)
    if not grouped:
        return []
    prev_close = _prev_close_map(base.grouped_daily(prev))
    trades: list[dict] = []

    for ticker, _chg in _day_candidates(cfg, grouped, prev_close):
        pc = prev_close[ticker]
        full5 = base.bars_5min(ticker, day, day)
        if not full5:
            continue
        # لحظة الزناد: أول شمعة 5د يعبر فيها السعر الحدّ عن إغلاق أمس
        trig = next((b for b in full5
                     if pc > 0 and (b.c - pc) / pc * 100.0 >= cfg.trigger_change_pct),
                    None)
        if trig is None:
            continue
        asof = trig.t_ms
        up_to = [b for b in full5 if b.t_ms <= asof]
        post = [b for b in full5 if b.t_ms > asof]
        snap = _build_snapshot(ticker, pc, up_to)
        if snap is None or not snap.is_valid:
            continue
        full1 = base.bars_1min(ticker, day, day)
        up_to_1 = [b for b in full1 if b.t_ms <= asof]
        asof_dt = datetime.fromtimestamp(asof / 1000, tz=timezone.utc).astimezone(ET)
        client = AsOfClient(base, day, asof, up_to, up_to_1, static_cache)
        try:
            cand = process_candidate(
                cfg, client, snap, halts=None,
                session=classify_session(cfg, asof_dt), et_now=asof_dt)
        except Exception as exc:  # noqa: BLE001 — سهم واحد لا يكسر اليوم
            logger.debug("باكتيست %s@%s فشل: %s", ticker, day, exc)
            continue
        if cand.is_rejected:
            continue
        result, gain, draw = simulate_outcome(
            snap.last_price, cand.risk, post, asof, cfg.outcome_window_min)
        trades.append({
            "date": day, "ticker": ticker, "entry": round(snap.last_price, 4),
            "session": cand.session.value, "score": round(cand.final_score, 1),
            "readiness": round(cand.readiness.classic_score, 1) if cand.readiness else 0,
            "rvol": round(cand.momentum.rvol, 1) if cand.momentum else 0,
            "had_news": bool(cand.catalyst and cand.catalyst.has_news),
            "result": result, "max_gain_pct": round(gain, 1),
            "max_draw_pct": round(draw, 1),
        })
    return trades


# ── النتيجة المجمّعة + التقرير ────────────────────────────────────
@dataclass
class BacktestResult:
    start: str
    end: str
    days: int = 0
    trades: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t["result"] == "win")
        losses = sum(1 for t in self.trades if t["result"] == "loss")
        timeouts = sum(1 for t in self.trades if t["result"] == "timeout")
        decisive = wins + losses
        return {
            "alerts": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "win_rate": (wins / decisive * 100.0) if decisive else None,
            "avg_gain": (sum(t["max_gain_pct"] for t in self.trades) / n) if n else 0.0,
            "per_day": n / self.days if self.days else 0.0,
        }


def _bucket_stats(trades: list[dict], keyfn) -> list[tuple]:
    groups: dict = {}
    for t in trades:
        k = keyfn(t)
        if k is not None:
            groups.setdefault(k, []).append(t)
    out = []
    for k, g in groups.items():
        dec = sum(1 for t in g if t["result"] in ("win", "loss"))
        wr = sum(1 for t in g if t["result"] == "win") / dec * 100.0 if dec else None
        out.append((k, len(g), wr))
    return sorted(out, key=lambda x: -(x[2] or -1))


def format_report(res: BacktestResult) -> str:
    s = res.stats()
    wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"
    lines = [
        f"📈 باكتيست {res.start} → {res.end} ({res.days} يوم تداول)",
        f"تنبيهات مُحاكاة: {s['alerts']} (~{s['per_day']:.1f}/يوم)",
        f"النجاح: {wr} ({s['wins']}✅/{s['losses']}🛑/{s['timeouts']}⏳) · "
        f"متوسط أقصى ربح {s['avg_gain']:+.0f}%",
    ]
    if res.trades:
        def b(title, kf):
            rows = [r for r in _bucket_stats(res.trades, kf) if r[1] >= 3]
            if not rows:
                return
            lines.append(f"\n{title}:")
            for k, cnt, w in rows:
                lines.append(f"  • {k}: {w:.0f}% نجاح ({cnt})" if w is not None
                             else f"  • {k}: — ({cnt})")
        b("حسب الجلسة", lambda t: t["session"])
        b("حسب الخبر", lambda t: "بمحفّز" if t["had_news"] else "بلا محفّز")
        b("حسب الجاهزية", lambda t: ("60-70" if t["readiness"] < 70 else
                                     "70-80" if t["readiness"] < 80 else "80+"))
        b("حسب الدرجة", lambda t: ("60-70" if t["score"] < 70 else
                                   "70-80" if t["score"] < 80 else "80+"))
    lines.append("\n⚠️ تقدير تاريخي (لا يضمن المستقبل؛ بلا انزلاق/دفتر أوامر).")
    return "\n".join(lines)


def run_backtest(cfg: Config, base: MassiveClient, start: str, end: str,
                 progress=None) -> BacktestResult:
    days = trading_days(start, end)
    res = BacktestResult(start=start, end=end, days=len(days))
    static_cache: dict = {}
    for i, day in enumerate(days, 1):
        res.trades.extend(simulate_day(cfg, base, day, static_cache))
        if progress:
            progress(i, len(days), day, len(res.trades))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="باكتيست الماسح الشامل")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--csv", help="مسار لحفظ الصفقات CSV (اختياري)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.from_env()
    if not cfg.massive_api_key:
        print("MASSIVE_API_KEY مطلوب للباكتيست.")
        return 2
    base = MassiveClient(cfg)

    def _prog(i, total, day, n):
        print(f"  [{i}/{total}] {day} … إجمالي صفقات: {n}", flush=True)

    res = run_backtest(cfg, base, args.start, args.end, progress=_prog)
    print("\n" + format_report(res))

    if args.csv and res.trades:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(res.trades[0].keys()))
            w.writeheader()
            w.writerows(res.trades)
        print(f"\n💾 حُفظت الصفقات: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
