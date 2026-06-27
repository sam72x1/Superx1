"""ركيزة الجاهزية الفنية الكلاسيكية — مقياس 0..100 (مدرسة التحليل الكلاسيكي).

درجة جزئية [-100,+100] لكل فريم (يومي/أسبوعي/شهري)، ميزانية موزّعة على
مكوّنات المدرسة الكلاسيكية:

  الاتجاه (داو) ±22 · موقع MA50 ±12 · موقع MA200 ±12 · التقاطع ±8 ·
  RSI ±6 · MACD ±6 · Stochastic RSI ±4 · دايفرجنس ±8 · ADX/DMI ±5 ·
  بولينجر %B ±4 · نماذج الشموع ±5 · البنية الموجية ±4 · الحجم ±4 = ±100.

ترجيح متعدّد الأطر (Top-Down): شهري 0.15 · أسبوعي 0.30 · يومي 0.40 ·
ساعة 0.15 → 0..100. (الأكبر يحكم، والساعة جسر للتنفيذ اللحظي.)
بوّابة المستخدم: classic_score ≥ 60 وإلا رفض.

أسهم حديثة الإدراج (تاريخ محدود): نتدرّج بأمان على المتاح ونوسم.
"""

from __future__ import annotations

from .candles import candle_signal
from .waves import wave_structure
from .config import Config
from .indicators import (
    adx_dmi,
    bollinger_pct_b,
    detect_divergence,
    linreg_slope_pct,
    macd as macd_calc,
    rsi as rsi_calc,
    rsi_series,
    sma,
    stoch_rsi as stoch_rsi_calc,
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


def score_timeframe(bars: list[Bar]) -> tuple[float, dict]:
    """درجة فريم واحد [-100,100] + تفاصيل. None-آمنة للتاريخ القصير."""
    detail: dict = {}
    if len(bars) < 5:
        return 0.0, {"insufficient": True}

    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    volumes = [b.v for b in bars]
    price = closes[-1]
    score = 0.0

    # ── الاتجاه (داو) ±22 ────────────────────────────────────────
    slope = linreg_slope_pct(closes)
    detail["trend"] = trend_label(slope)
    score += max(-22.0, min(22.0, slope * 2.2))

    # ── المتوسطات والتقاطع ±32 ───────────────────────────────────
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    detail["above_ma50"] = bool(ma50 and price >= ma50)
    detail["above_ma200"] = bool(ma200 and price >= ma200)
    if ma50:
        score += 12.0 if price >= ma50 else -12.0
    if ma200:
        score += 12.0 if price >= ma200 else -12.0
    golden = bool(ma50 and ma200 and ma50 >= ma200)
    detail["golden_cross"] = golden
    if ma50 and ma200:
        score += 8.0 if golden else -8.0

    # ── RSI ±6 ───────────────────────────────────────────────────
    r = rsi_calc(closes)
    detail["rsi"] = round(r, 1) if r is not None else None
    if r is not None:
        if r >= 50:
            score += min(6.0, (r - 50) / 20.0 * 6.0)
            if r > 80:
                score -= 2.0   # تشبّع شرائي حاد
        else:
            score -= min(6.0, (50 - r) / 20.0 * 6.0)

    # ── MACD ±6 ──────────────────────────────────────────────────
    m = macd_calc(closes)
    macd_bull = bool(m and m[0] >= m[1])
    detail["macd_bull"] = macd_bull
    if m:
        score += 6.0 if macd_bull else -6.0

    # ── Stochastic RSI ±4 ────────────────────────────────────────
    sr = stoch_rsi_calc(closes)
    detail["stoch_rsi"] = round(sr, 2) if sr is not None else None
    if sr is not None:
        c = max(-4.0, min(4.0, (sr - 0.5) * 8.0))
        if sr > 0.9:           # تشبّع متطرّف → تخفيف
            c -= 2.0
        score += c

    # ── دايفرجنس ±8 ──────────────────────────────────────────────
    div = detect_divergence(closes, rsi_series(closes))
    detail["divergence"] = div
    if div == "صاعد":
        score += 8.0
    elif div == "هابط":
        score -= 8.0

    # ── ADX / DMI ±5 (قوة + وجهة) ───────────────────────────────
    adx_res = adx_dmi(highs, lows, closes)
    if adx_res is not None:
        adx_val, plus_di, minus_di = adx_res
        detail["adx"] = round(adx_val, 1)
        strength = min(1.0, adx_val / 30.0)
        if plus_di > minus_di:
            contrib = 5.0 * strength
            if adx_val > 45:      # قوي جدًا → قرب إنهاك
                contrib -= 1.5
            score += contrib
        else:
            score -= 5.0 * strength

    # ── بولينجر %B ±4 ────────────────────────────────────────────
    pb = bollinger_pct_b(closes)
    if pb is not None:
        detail["bb_pct_b"] = round(pb, 2)
        if pb > 1.05:             # فوق الباند العلوي = ممتد
            score -= 2.0
        else:
            score += max(-4.0, min(4.0, (pb - 0.5) * 8.0))

    # ── نماذج الشموع ±5 (بسياق الاتجاه) ─────────────────────────
    csig, cname = candle_signal(bars)
    detail["candle"] = cname
    score += csig * 5.0

    # ── البنية الموجية ±4 (بديل إليوت: دافعة/تصحيحية عبر HH/HL) ──
    wsig, wname = wave_structure(bars)
    detail["wave"] = wname
    score += wsig * 4.0

    # ── الحجم كمؤكّد ±4 ──────────────────────────────────────────
    if len(volumes) >= 20:
        recent = sum(volumes[-3:]) / 3.0
        base = sum(volumes[-20:]) / 20.0
        if base > 0:
            ratio = recent / base
            if ratio >= 1.3:
                score += 4.0
            elif ratio <= 0.7:
                score -= 4.0

    return max(-100.0, min(100.0, score)), detail


def compute_readiness(cfg: Config, daily: list[Bar],
                      weekly: list[Bar] | None = None,
                      monthly: list[Bar] | None = None,
                      hourly: list[Bar] | None = None,
                      frame_cache: dict | None = None) -> ReadinessResult:
    """يجمع درجات الأطر إلى classic_score 0..100 + pillar_score.

    Top-Down: شهري/أسبوعي = سياق، يومي = الإعداد، ساعة = جسر للتنفيذ.
    الساعة اختيارية؛ عند غيابها يُعاد تطبيع الأوزان تلقائيًا.

    frame_cache (اختياري، للباكتيست): الأطر شهري/أسبوعي/يومي **ثابتة خلال اليوم**
    (تُبنى من نفس اليومي)، فنخزّن درجتها مرة لكل (سهم/يوم) ونعيد استخدامها عبر
    شموع المسح المتكرّر بدل إعادة حسابها الثقيل كل شمعة. الساعة تبقى لحظية.
    **بلا أي أثر على النتيجة** (نفس المدخلات الثابتة → نفس الدرجة).
    """
    weekly = weekly if weekly else resample(daily, 5)
    monthly = monthly if monthly else resample(daily, 21)

    frames = {
        "monthly": (monthly, 0.15),
        "weekly": (weekly, 0.30),
        "daily": (daily, 0.40),
        "hourly": (hourly or [], 0.15),
    }

    weighted_sum = 0.0
    weight_total = 0.0
    notes: list[str] = []
    limited = len(daily) < 200

    daily_detail: dict = {}
    for name, (bars, w) in frames.items():
        if not bars:
            continue   # إطار غير مُمرَّر (مثلًا الساعة) — تطبيع تلقائي
        if len(bars) < 5:
            notes.append(f"{name}: تاريخ غير كافٍ")
            continue
        # الأطر الثابتة خلال اليوم تُكاش؛ الساعة (متغيّرة) تُحسب دائمًا.
        if frame_cache is not None and name != "hourly":
            cached = frame_cache.get(name)
            if cached is None:
                cached = score_timeframe(bars)
                frame_cache[name] = cached
            part, detail = cached
        else:
            part, detail = score_timeframe(bars)
        if name == "daily":
            daily_detail = detail
        weighted_sum += part * w
        weight_total += w

    if weight_total == 0:
        return ReadinessResult(
            classic_score=0.0, pillar_score=0.0, trend="غير معروف",
            rsi=0.0, macd_bull=False, divergence="لا شيء",
            above_ma50=False, above_ma200=False, golden_cross=False,
            limited_history=True, notes=["لا تاريخ كافٍ"],
        )

    partial = weighted_sum / weight_total
    classic_score = max(0.0, min(100.0, (partial + 100.0) / 2.0))
    # تاريخ قصير جدًا: معظم المؤشرات (MA50/200/RSI/MACD/ADX) لا تُحسب فترجع
    # درجة ~50 «محايدة المظهر» من غياب البيانات لا من حياد فني حقيقي. لا نؤكّد
    # جاهزية لا نملك بياناتها → نخفض الثقة بوضوح (يُسقطها تحت العتبة عادة).
    if len(daily) < cfg.min_history_bars:
        classic_score = round(classic_score * 0.5, 1)
        notes.append(f"تاريخ قصير (<{cfg.min_history_bars} يوم) — جاهزية غير مؤكَّدة")
    pillar = classic_score / 100.0 * cfg.readiness_pillar_max

    candle = daily_detail.get("candle", "")
    if candle:
        notes.append(f"شمعة يومية: {candle}")

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
        adx=float(daily_detail.get("adx") or 0.0),
        stoch_rsi=float(daily_detail.get("stoch_rsi") or 0.0),
        bb_pct_b=float(daily_detail.get("bb_pct_b") or 0.5),
        candle=candle,
        wave=daily_detail.get("wave", ""),
        limited_history=limited,
        notes=notes,
    )
