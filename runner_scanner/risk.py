"""الوقف الهجين والأهداف (القسم 8).

- الوقف = max(تحت أقرب دعم داخل-جلسة من شموع 5د مغلقة، سقف نسبة%)
  بحد أدنى لمسافة الوقف ~3–4% (ضوضاء LULD)، وسقف أعلى ~20%.
- الدعم من شمعة 5د مغلقة لا الجارية، ومن داخل الجلسة لا الدعم اليومي البعيد.
- الأهداف = مقاومات حقيقية فقط (قمم 5د + قمة اليوم + قمم يومية +
  أرقام مستديرة) — لا مضاعفات حسابية عشوائية.
"""

from __future__ import annotations

import math
from .config import Config
from .indicators import pivots
from .models import Bar, RiskPlan


def _support_levels(closed_bars: list[Bar], entry: float) -> list[float]:
    """مستويات الدعم تحت الدخول من قيعان شموع 5د المغلقة، الأقرب أولاً."""
    if len(closed_bars) < 3:
        return []
    lows = [b.l for b in closed_bars]
    _, low_idx = pivots(lows)
    candidates = sorted({lows[i] for i in low_idx if lows[i] < entry},
                        reverse=True)  # الأقرب (الأعلى) أولاً
    if not candidates:
        below = sorted({lo for lo in lows if lo < entry}, reverse=True)
        return below
    return candidates


def _intraday_support(closed_bars: list[Bar], entry: float) -> float | None:
    """أقرب دعم تحت الدخول (للوقف)."""
    levels = _support_levels(closed_bars, entry)
    return levels[0] if levels else None


def _round_step(price: float) -> float:
    """خطوة الأرقام المستديرة المناسبة للسعر (مقاومات نفسية)."""
    if price < 5:
        return 0.5
    if price < 20:
        return 1.0
    return 2.5


def _round_levels_above(entry: float, n: int) -> list[float]:
    """أرقام مستديرة فوق الدخول (تُعامَل كمقاومات نفسية حقيقية)."""
    step = _round_step(entry)
    out: list[float] = []
    lvl = (math.floor(entry / step) + 1) * step
    guard = 0
    while len(out) < n and guard < 50:
        if lvl > entry * 1.005:
            out.append(round(lvl, 2))
        lvl += step
        guard += 1
    return out


def resistance_targets(entry: float, closed_bars: list[Bar],
                       extra: list[float] | None = None,
                       count: int = 3, max_pct: float = 80.0,
                       min_bar_trades: int = 0) -> list[float]:
    """أهداف = مقاومات حقيقية فقط (لا مضاعفات حسابية):
    قمم 5د المحورية · قمة اليوم داخل-الجلسة · قمم يومية مُمرَّرة (أمس/الأسبوع)
    · أرقام مستديرة. تُدمج المتقاربة (~1.5%) وتُؤخذ الأقرب فوق الدخول، وضمن
    سقف مسافة معقول (max_pct) كي لا تدخل قمة بعيدة جدًا (سهم انهار من فوق).
    min_bar_trades: يستبعد قمم الشموع الرقيقة (طبعة واحدة) من مقاومة اليوم."""
    if entry <= 0:
        return []   # سعر غير صالح → لا أهداف (حارس للاستدعاء المباشر)
    ceiling = entry * (1 + max_pct / 100.0)
    cands: set[float] = set()

    # قمم 5د المحورية فوق الدخول — من شموع ذات سيولة فعلية فقط (لا طبعة رقيقة)
    def _liquid(b: Bar) -> bool:
        return b.v > 0 and (b.n == 0 or b.n >= min_bar_trades)

    if len(closed_bars) >= 3:
        highs = [b.h for b in closed_bars]
        hi_idx, _ = pivots(highs)
        cands |= {highs[i] for i in hi_idx
                  if entry < highs[i] <= ceiling and _liquid(closed_bars[i])}

    # قمة اليوم داخل-الجلسة — من شموع ذات سيولة فعلية فقط (لا طبعة رقيقة)
    if closed_bars:
        liquid = [b for b in closed_bars if _liquid(b)]
        if liquid:
            day_hi = max(b.h for b in liquid)
            if entry < day_hi <= ceiling:
                cands.add(day_hi)

    # مقاومات يومية مُمرَّرة (قمة أمس، قمة 10 أيام...) — ضمن السقف فقط
    for r in (extra or []):
        if r and entry < r <= ceiling:
            cands.add(r)

    # دمج المتقاربة (ضمن ~1.5%) للحفاظ على مستويات متمايزة
    merged: list[float] = []
    for lv in sorted(cands):
        if not merged or lv > merged[-1] * 1.015:
            merged.append(lv)

    # تكملة بأرقام مستديرة (مقاومات نفسية) لضمان العدد — السقف يمنع القمم
    # البعيدة فقط، أما الأرقام المستديرة فقريبة بطبعها (تبدأ فوق الدخول).
    if len(merged) < count:
        for rl in _round_levels_above(entry, count + 6):
            if len(merged) >= count:
                break
            if all(abs(rl - m) / m > 0.015 for m in merged):
                merged.append(rl)
        merged = sorted(merged)

    return [round(t, 4) for t in merged[:count]]


def build_risk_plan(cfg: Config, entry: float,
                    closed_bars_5min: list[Bar],
                    daily_resistances: list[float] | None = None) -> RiskPlan:
    """يبني الوقف (نسبة ثابتة من الدخول) والأهداف (مقاومات حقيقية) من الشارت.
    daily_resistances: مقاومات يومية اختيارية (قمة أمس/الأسبوع) تُدمج كأهداف."""
    if entry <= 0:   # حارس: سعر غير صالح → خطة فارغة بدل أرقام عبثية
        return RiskPlan(stop_price=0.0, stop_pct=0.0, entry_ref=0.0, targets=[],
                        stop_basis="سعر غير صالح")

    # ── الوقف: نسبة ثابتة من الدخول (قرار المستخدم) ──────────────
    # الوقف = الدخول − stop_fixed_pct% بالضبط، لا دعم ولا قصّ.
    stop_pct = cfg.stop_fixed_pct
    stop_price = entry * (1 - stop_pct / 100.0)
    basis = f"ثابت {stop_pct:g}%"

    # ── الأهداف: مقاومات حقيقية فقط (لا مضاعفات حسابية) ──────────
    targets = resistance_targets(entry, closed_bars_5min,
                                 extra=daily_resistances, count=3,
                                 max_pct=cfg.target_max_pct,
                                 min_bar_trades=cfg.min_bar_trades)

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
