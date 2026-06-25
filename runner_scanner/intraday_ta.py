"""ركيزة الزخم اللحظي (/50) — القسم 5.

تُحسب من شموع 5د (والدقيقة لـ VWAP الجلسي) + السنابشوت:
RVol (حسب الجلسة) · زخم آخر 5د% · موقع من VWAP · اتساع المدى ·
تأكيد الحجم (متصاعد لا متناقص) · 5min RVol (العمود البارز في scanner).
"""

from __future__ import annotations

from .config import Config
from .indicators import session_vwap
from .models import Bar, MomentumResult, Session, SnapshotEntry
from .sessions import compute_rvol


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_momentum(
    cfg: Config,
    snap: SnapshotEntry,
    session: Session,
    bars_5min: list[Bar],
    bars_1min: list[Bar] | None = None,
    avg_daily_volume: float = 0.0,
    avg_premarket_volume: float | None = None,
    elapsed_fraction: float | None = None,
) -> MomentumResult:
    """يبني MomentumResult بدرجة 0..momentum_pillar_max."""
    notes: list[str] = []

    # ── VWAP الجلسي: من شموع الدقيقة، وإلا تقريب من السنابشوت ──────
    vwap = session_vwap(bars_1min) if bars_1min else None
    if vwap is None:
        vwap = snap.day_vwap or snap.last_price
        notes.append("VWAP تقريبي (snapshot)")
    price = snap.last_price
    vwap_dist = ((price - vwap) / vwap * 100.0) if vwap else 0.0
    above_vwap = price >= vwap if vwap else False

    # ── زخم آخر 5 دقائق ──────────────────────────────────────────
    change_5min = 0.0
    rvol_5min = 0.0
    volume_rising = False
    if bars_5min:
        last = bars_5min[-1]
        if last.o > 0:
            change_5min = (last.c - last.o) / last.o * 100.0
        vols = [b.v for b in bars_5min if b.v > 0]
        if len(vols) >= 2:
            avg_vol = _avg(vols[:-1]) or _avg(vols)
            rvol_5min = (last.v / avg_vol) if avg_vol > 0 else 0.0
        if len(vols) >= 3:
            # متصاعد: آخر 3 شموع في ميل صاعد للحجم
            volume_rising = vols[-1] >= vols[-2] >= vols[-3]

    # ── RVol حسب الجلسة ──────────────────────────────────────────
    rvol = compute_rvol(
        cfg, session,
        cumulative_volume=snap.day_volume,
        avg_daily_volume=avg_daily_volume,
        elapsed_fraction=elapsed_fraction,
        avg_premarket_volume=avg_premarket_volume,
    )

    # ── الدرجة (مجموع المكوّنات ≤ momentum_pillar_max) ────────────
    score = 0.0
    cap = cfg.momentum_pillar_max

    # RVol الجلسي حتى 20 نقطة (مقياس: rvol_min → نصف، 3×rvol_min → كامل)
    if cfg.rvol_min > 0:
        rvol_ratio = rvol / cfg.rvol_min
        score += min(20.0, max(0.0, (rvol_ratio - 1.0) * 10.0 + 10.0)) if rvol >= cfg.rvol_min \
            else max(0.0, rvol_ratio * 10.0)

    # 5min RVol حتى 12 نقطة (5x → نصف، 20x+ → كامل)
    score += min(12.0, rvol_5min / 20.0 * 12.0)

    # موقع من VWAP حتى 10 نقاط (فوق + مسافة معقولة 0..15% مثالي)
    if above_vwap:
        if vwap_dist <= 15.0:
            score += 10.0
        elif vwap_dist <= 25.0:
            score += 6.0
        else:
            score += 2.0  # ممتد، خطر (يُعاقَب أكثر في البوابات)
    else:
        notes.append("تحت VWAP")

    # زخم 5د موجب حتى 5 نقاط
    if change_5min > 0:
        score += min(5.0, change_5min / 3.0 * 5.0)

    # تأكيد الحجم المتصاعد 3 نقاط
    if volume_rising:
        score += 3.0
    else:
        notes.append("حجم غير متصاعد")

    score = max(0.0, min(cap, score))

    return MomentumResult(
        score=round(score, 2),
        rvol=round(rvol, 2),
        rvol_5min=round(rvol_5min, 2),
        change_5min_pct=round(change_5min, 2),
        vwap_distance_pct=round(vwap_dist, 2),
        above_vwap=above_vwap,
        volume_rising=volume_rising,
        notes=notes,
    )
