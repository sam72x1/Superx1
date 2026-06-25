"""ركيزة الجاهزية الفنية الكلاسيكية (القسم 4) — مقياس 0..100.

تُحسب درجة جزئية [-100,+100] لكل فريم (يومي/أسبوعي/شهري) من:
الاتجاه (±30) · موقع MA50 (±15) · موقع MA200 (±15) · التقاطع (±10) ·
RSI (±8) · MACD (±7) · دايفرجنس (±10) · الحجم (±5)  = ±100.

ثم: ترجيح (يومي 0.45 · أسبوعي 0.35 · شهري 0.20) → تحويل إلى 0..100.
بوّابة المستخدم: classic_score ≥ 70 وإلا رفض.

رَنرات حديثة الإدراج (تاريخ محدود): نتدرّج بأمان على الفريمات المتاحة
ونوسم limited_history.
"""

from __future__ import annotations

from .config import Config
from .indicators import (
    detect_divergence,
    linreg_slope_pct,
    macd as macd_calc,
    rsi as rsi_calc,
    sma,
    trend_label,
)
from .models import Bar, ReadinessResult


def resample(daily: list[Bar], group: int) -> list[Bar]:
    """يجمّع شموعًا يومية إلى أسبوعية (~5) أو شهرية (~21) عند غياب المباشرة."""
    if group <= 1 or not daily:
        return list(daily)
    out: list[Bar] = []
    for i in range(0, len(daily), group):
        chunk = daily[i:i + group]
        if not chunk:
            continue
        out.append(Bar(
            t_ms=chunk[0].t_ms,
            o=chunk[0].o,
            h=max(b.h for b in chunk),
            l=min(b.l for b in chunk),
            c=chunk[-1].c,
            v=sum(b.v for b in chunk),
            vw=0.0,
            n=sum(b.n for b in chunk),
        ))
    return out


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """سلسلة RSI تقريبية (قيمة لكل نقطة من period+1 فصاعدًا) للدايفرجنس."""
    out: list[float] = []
    for i in range(len(closes)):
        window = closes[: i + 1]
        val = rsi_calc(window, period)
        out.append(val if val is not None else 50.0)
    return out


def score_timeframe(closes: list[float], highs: list[float],
                    lows: list[float], volumes: list[float]) -> tuple[float, dict]:
    """درجة فريم واحد [-100,100] + تفاصيل. None-آمنة للتاريخ القصير."""
    detail: dict = {}
    if len(closes) < 5:
        return 0.0, {"insufficient": True}

    score = 0.0

    # الاتجاه ±30
    slope = linreg_slope_pct(closes)
    label = trend_label(slope)
    detail["trend"] = label
    score += max(-30.0, min(30.0, slope * 3.0))

    price = closes[-1]
    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    detail["above_ma50"] = bool(ma50 and price >= ma50)
    detail["above_ma200"] = bool(ma200 and price >= ma200)

    # موقع MA50 ±15
    if ma50:
        score += 15.0 if price >= ma50 else -15.0
    # موقع MA200 ±15
    if ma200:
        score += 15.0 if price >= ma200 else -15.0
    # التقاطع الذهبي/الموت ±10
    golden = bool(ma50 and ma200 and ma50 >= ma200)
    detail["golden_cross"] = golden
    if ma50 and ma200:
        score += 10.0 if golden else -10.0

    # RSI ±8
    r = rsi_calc(closes)
    detail["rsi"] = round(r, 1) if r is not None else None
    if r is not None:
        if r >= 50:
            score += min(8.0, (r - 50) / 20.0 * 8.0)
            if r > 80:  # تشبّع شرائي حاد → تخفيف
                score -= 3.0
        else:
            score -= min(8.0, (50 - r) / 20.0 * 8.0)

    # MACD ±7
    m = macd_calc(closes)
    macd_bull = bool(m and m[0] >= m[1])
    detail["macd_bull"] = macd_bull
    if m:
        score += 7.0 if macd_bull else -7.0

    # دايفرجنس ±10
    div = detect_divergence(closes, _rsi_series(closes))
    detail["divergence"] = div
    if div == "صاعد":
        score += 10.0
    elif div == "هابط":
        score -= 10.0

    # الحجم كمؤكّد ±5 (متوسط آخر 3 ÷ متوسط 20)
    if len(volumes) >= 20:
        recent = sum(volumes[-3:]) / 3.0
        base = sum(volumes[-20:]) / 20.0
        if base > 0:
            ratio = recent / base
            if ratio >= 1.3:
                score += 5.0
            elif ratio <= 0.7:
                score -= 5.0

    detail["ma20"] = ma20
    return max(-100.0, min(100.0, score)), detail


def compute_readiness(cfg: Config, daily: list[Bar],
                      weekly: list[Bar] | None = None,
                      monthly: list[Bar] | None = None) -> ReadinessResult:
    """يجمع درجات الفريمات إلى classic_score 0..100 + pillar_score."""
    weekly = weekly if weekly else resample(daily, 5)
    monthly = monthly if monthly else resample(daily, 21)

    frames = {
        "daily": (daily, 0.45),
        "weekly": (weekly, 0.35),
        "monthly": (monthly, 0.20),
    }

    weighted_sum = 0.0
    weight_total = 0.0
    notes: list[str] = []
    limited = len(daily) < 200  # أقل من ~سنة تداول

    daily_detail: dict = {}
    for name, (bars, w) in frames.items():
        if len(bars) < 5:
            notes.append(f"{name}: تاريخ غير كافٍ")
            continue
        closes = [b.c for b in bars]
        highs = [b.h for b in bars]
        lows = [b.l for b in bars]
        vols = [b.v for b in bars]
        part, detail = score_timeframe(closes, highs, lows, vols)
        if name == "daily":
            daily_detail = detail
        weighted_sum += part * w
        weight_total += w

    if weight_total == 0:
        # لا تاريخ كافٍ على أي فريم → جاهزية صفر، تاريخ محدود
        return ReadinessResult(
            classic_score=0.0, pillar_score=0.0, trend="غير معروف",
            rsi=0.0, macd_bull=False, divergence="لا شيء",
            above_ma50=False, above_ma200=False, golden_cross=False,
            limited_history=True, notes=["لا تاريخ كافٍ"],
        )

    # إعادة تطبيع لو غابت بعض الفريمات
    partial = weighted_sum / weight_total  # في [-100,100]
    classic_score = max(0.0, min(100.0, (partial + 100.0) / 2.0))
    pillar = classic_score / 100.0 * cfg.readiness_pillar_max

    return ReadinessResult(
        classic_score=round(classic_score, 1),
        pillar_score=round(pillar, 2),
        trend=daily_detail.get("trend", "غير معروف"),
        rsi=float(daily_detail.get("rsi") or 0.0),
        macd_bull=bool(daily_detail.get("macd_bull")),
        divergence=daily_detail.get("divergence", "لا شيء"),
        above_ma50=bool(daily_detail.get("above_ma50")),
        above_ma200=bool(daily_detail.get("above_ma200")),
        golden_cross=bool(daily_detail.get("golden_cross")),
        limited_history=limited,
        notes=notes,
    )
