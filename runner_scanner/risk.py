"""الوقف الهجين والأهداف (القسم 8).

- الوقف = max(تحت أقرب دعم داخل-جلسة من شموع 5د **مغلقة**، سقف نسبة%)
  بحدّ أدنى لمسافة الوقف ~3–4% (ضوضاء LULD)، وسقف أعلى ~20%.
- الدعم من شمعة 5د مغلقة لا الجارية، ومن داخل الجلسة لا الدعم اليومي البعيد.
- الأهداف من مقاومات/امتدادات داخل-الجلسة (مضاعفات R كأساس + أقرب مقاومة).
"""

from __future__ import annotations

from .config import Config
from .indicators import pivots
from .models import Bar, RiskPlan


def _support_levels(closed_bars: list[Bar], entry: float) -> list[float]:
    """مستويات الدعم تحت الدخول من قيعان شموع 5د المغلقة، الأقرب أولًا."""
    if len(closed_bars) < 3:
        return []
    lows = [b.l for b in closed_bars]
    _, low_idx = pivots(lows)
    candidates = sorted({lows[i] for i in low_idx if lows[i] < entry},
                        reverse=True)  # الأقرب (الأعلى) أولًا
    if not candidates:
        below = sorted({lo for lo in lows if lo < entry}, reverse=True)
        return below
    return candidates


def _intraday_support(closed_bars: list[Bar], entry: float) -> float | None:
    """أقرب دعم تحت الدخول (للوقف)."""
    levels = _support_levels(closed_bars, entry)
    return levels[0] if levels else None


def _intraday_resistance(closed_bars: list[Bar], entry: float) -> float | None:
    """أقرب مقاومة فوق الدخول من قمم شموع 5د المغلقة."""
    if len(closed_bars) < 3:
        return None
    highs = [b.h for b in closed_bars]
    high_idx, _ = pivots(highs)
    candidates = [highs[i] for i in high_idx if highs[i] > entry]
    if not candidates:
        above = [hi for hi in highs if hi > entry]
        return min(above) if above else None
    return min(candidates)


def build_risk_plan(cfg: Config, entry: float,
                    closed_bars_5min: list[Bar]) -> RiskPlan:
    """يبني الوقف والأهداف من شموع 5د المغلقة (آخر شمعة جارية تُستثنى من قبل)."""
    # ── الوقف: هجين ──────────────────────────────────────────────
    # الأساس = الدعم داخل-الجلسة (تحته بهامش بسيط). لو ما فيه دعم،
    # نستخدم الحد الأدنى للنسبة. ثم نقصّ المسافة بين [min%, max%]:
    #   مسافة أقرب من الحد الأدنى → ندفعها للحد الأدنى (ضوضاء LULD).
    #   مسافة أبعد من السقف → نقصّها للسقف.
    support = _intraday_support(closed_bars_5min, entry)
    if support is not None and support < entry:
        stop_price = support * 0.997   # تحت الدعم بهامش بسيط
        basis = "دعم 5د"
    else:
        stop_price = entry * (1 - cfg.stop_min_pct / 100.0)
        basis = "حد أدنى"

    stop_pct = (entry - stop_price) / entry * 100.0 if entry > 0 else 0.0
    if stop_pct < cfg.stop_min_pct:
        stop_pct = cfg.stop_min_pct
        stop_price = entry * (1 - stop_pct / 100.0)
        basis = "حد أدنى" if basis == "دعم 5د" else basis
    elif stop_pct > cfg.stop_max_pct:
        stop_pct = cfg.stop_max_pct
        stop_price = entry * (1 - stop_pct / 100.0)
        basis = "سقف أقصى"

    # ── الأهداف: مقاومات داخل-جلسة أولًا، ثم مضاعفات R لتكملة 3 ──
    r = max(entry - stop_price, entry * 0.05)
    targets: list[float] = []
    # أقرب 3 مقاومات فوق الدخول من قمم الشموع المغلقة
    highs = [b.h for b in closed_bars_5min]
    high_idx, _ = pivots(highs) if len(closed_bars_5min) >= 3 else ([], [])
    resistances = sorted({highs[i] for i in high_idx if highs[i] > entry})
    for res in resistances:
        if len(targets) >= 3:
            break
        targets.append(round(res, 4))
    # تكملة بمضاعفات R لو أقل من 3
    mult = 1
    while len(targets) < 3:
        cand = round(entry + r * mult, 4)
        if not targets or cand > targets[-1]:
            targets.append(cand)
        mult += 1
    targets = sorted(targets)[:3]

    # ── مستويات الدعم ومنطقة الشراء (للعرض) ──────────────────────
    levels = _support_levels(closed_bars_5min, entry)
    support_near = round(levels[0], 4) if len(levels) >= 1 else None
    support_deep = round(levels[1], 4) if len(levels) >= 2 else None
    buy_low = round(entry, 4)
    buy_high = round(entry * (1 + cfg.buy_zone_pct / 100.0), 4)

    return RiskPlan(
        stop_price=round(stop_price, 4),
        stop_pct=round(stop_pct, 2),
        entry_ref=round(entry, 4),
        targets=targets,
        stop_basis=basis,
        support_near=support_near,
        support_deep=support_deep,
        buy_low=buy_low,
        buy_high=buy_high,
    )
