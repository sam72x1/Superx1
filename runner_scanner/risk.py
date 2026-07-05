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


# ترتيب عرض أنواع الأهداف (منهجية المستخدم: ه١ مقاومة · ه٢ متوسط · ه٣ قمة موجة)
_KIND_ORDER = ("مقاومة", "متوسط ٢٠", "متوسط ٥٠", "قمة تأرجح", "رقم نفسي")


def _label_kinds(kinds: set[str]) -> str:
    """يدمج أنواع مستوى واحد: المعروفة بترتيبها ثم أي نوع ديناميكي (مثل «قمة 4س»)."""
    ordered = [k for k in _KIND_ORDER if k in kinds]
    extra = sorted(k for k in kinds if k not in _KIND_ORDER)
    return "+".join(ordered + extra) or "مقاومة"


def resistance_targets(entry: float, closed_bars: list[Bar],
                       extra: list[float] | None = None,
                       count: int = 3, max_pct: float = 80.0,
                       min_bar_trades: int = 0,
                       ma_levels: dict | None = None,
                       daily_peaks: list[float] | None = None,
                       return_labeled: bool = False):
    """أهداف = مقاومات حقيقية فقط (لا مضاعفات حسابية)، **موسومة بنوعها**
    (منهجية المستخدم):
    · مقاومة: قمم 5د المحورية · قمة اليوم داخل-الجلسة · قمم يومية مُمرَّرة (أمس/الأسبوع)
    · متوسط ٢٠/٥٠: `ma_levels` = {نوع: سعر} (متوسطات يومية فوق الدخول فقط)
    · قمة تأرجح: `daily_peaks` = قمم الموجة السابقة (محورية يومية)
    · رقم نفسي: تكملة للعدد
    تُدمج المتقاربة (~1.5%) ضامّةً أنواعها، وتُؤخذ الأقرب فوق الدخول ضمن سقف
    مسافة معقول (max_pct). min_bar_trades يستبعد قمم الشموع الرقيقة.
    return_labeled=True يرجّع [(سعر، نوع)] بدل [سعر]."""
    if entry <= 0:
        return []   # سعر غير صالح → لا أهداف (حارس للاستدعاء المباشر)
    ceiling = entry * (1 + max_pct / 100.0)
    raw: list[tuple[float, str]] = []

    def _add(level: float | None, kind: str) -> None:
        # مقاومة حقيقية فقط: فوق الدخول وضمن السقف (لا هدف بعيد سخيف)
        if level and entry < level <= ceiling:
            raw.append((float(level), kind))

    # قمم 5د المحورية فوق الدخول — من شموع ذات سيولة فعلية فقط (لا طبعة رقيقة)
    def _liquid(b: Bar) -> bool:
        return b.v > 0 and (b.n == 0 or b.n >= min_bar_trades)

    if len(closed_bars) >= 3:
        highs = [b.h for b in closed_bars]
        hi_idx, _ = pivots(highs)
        for i in hi_idx:
            if _liquid(closed_bars[i]):
                _add(highs[i], "مقاومة")

    # قمة اليوم داخل-الجلسة — من شموع ذات سيولة فعلية فقط
    if closed_bars:
        liquid = [b for b in closed_bars if _liquid(b)]
        if liquid:
            _add(max(b.h for b in liquid), "مقاومة")

    # مقاومات يومية مُمرَّرة (قمة أمس، قمة 10 أيام...)
    for r in (extra or []):
        _add(r, "مقاومة")

    # متوسطات متحركة يومية كأهداف (منهجية المستخدم: ه٢ متوسط ٢٠/٥٠)
    for label, lvl in (ma_levels or {}).items():
        _add(lvl, label)

    # قمم تأرجح يومية سابقة (قمم الموجة السابقة — ه٣ عند المستخدم)
    for pk in (daily_peaks or []):
        _add(pk, "قمة تأرجح")

    # دمج المتقاربة (ضمن ~1.5%) مع ضمّ أنواع كل عنقود
    raw.sort(key=lambda x: x[0])
    merged: list[list] = []   # [سعر, set(أنواع)]
    for lv, kind in raw:
        if merged and lv <= merged[-1][0] * 1.015:
            merged[-1][1].add(kind)
        else:
            merged.append([lv, {kind}])

    # تكملة بأرقام مستديرة (مقاومات نفسية) لضمان العدد — السقف يمنع القمم
    # البعيدة فقط، أما الأرقام المستديرة فقريبة بطبعها (تبدأ فوق الدخول).
    if len(merged) < count:
        for rl in _round_levels_above(entry, count + 6):
            if len(merged) >= count:
                break
            if all(abs(rl - m[0]) / m[0] > 0.015 for m in merged):
                merged.append([rl, {"رقم نفسي"}])
        merged.sort(key=lambda x: x[0])

    picked = merged[:count]
    if return_labeled:
        return [(round(lv, 4), _label_kinds(kinds)) for lv, kinds in picked]
    return [round(lv, 4) for lv, _ in picked]


def build_risk_plan(cfg: Config, entry: float,
                    closed_bars_5min: list[Bar],
                    daily_resistances: list[float] | None = None,
                    ma_levels: dict | None = None,
                    daily_peaks: list[float] | None = None) -> RiskPlan:
    """يبني الوقف (نسبة ثابتة من الدخول) والأهداف (مقاومات حقيقية موسومة) من الشارت.
    daily_resistances: مقاومات يومية (قمة أمس/الأسبوع) تُدمج كأهداف.
    ma_levels: متوسطات ٢٠/٥٠ يومية كأهداف. daily_peaks: قمم موجة سابقة."""
    if entry <= 0:   # حارس: سعر غير صالح → خطة فارغة بدل أرقام عبثية
        return RiskPlan(stop_price=0.0, stop_pct=0.0, entry_ref=0.0, targets=[],
                        stop_basis="سعر غير صالح")

    # ── الوقف: نسبة ثابتة من الدخول (قرار المستخدم) ──────────────
    # الوقف = الدخول − stop_fixed_pct% بالضبط، لا دعم ولا قصّ.
    stop_pct = cfg.stop_fixed_pct
    stop_price = entry * (1 - stop_pct / 100.0)
    basis = f"ثابت {stop_pct:g}%"

    # ── الأهداف: مقاومات حقيقية موسومة بنوعها (منهجية المستخدم) ──
    # مستويات موسومة إضافية: المتوسطات + «قمة آخر N ساعة» (نافذة متدحرجة داخل-
    # اليوم من شموع 5د المغلقة) — تُدمج كمرشّح مقاومة إن كانت فوق الدخول.
    levels = dict(ma_levels or {})
    hrs = cfg.target_recent_high_hours
    if hrs > 0 and closed_bars_5min:
        n = max(1, int(hrs * 12))          # 12 شمعة 5د لكل ساعة
        window = [b for b in closed_bars_5min[-n:] if b.v > 0]
        if window:
            levels[f"قمة {hrs:g}س"] = max(b.h for b in window)
    labeled = resistance_targets(entry, closed_bars_5min,
                                 extra=daily_resistances, count=3,
                                 max_pct=cfg.target_max_pct,
                                 min_bar_trades=cfg.min_bar_trades,
                                 ma_levels=levels, daily_peaks=daily_peaks,
                                 return_labeled=True)
    targets = [lv for lv, _ in labeled]
    target_kinds = [k for _, k in labeled]

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
        target_kinds=target_kinds,
        stop_basis=basis,
        support_near=support_near,
        support_deep=support_deep,
        buy_low=buy_low,
        buy_high=buy_high,
        ma20=(ma_levels or {}).get("متوسط ٢٠"),
        ma50=(ma_levels or {}).get("متوسط ٥٠"),
    )
