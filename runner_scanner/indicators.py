"""مؤشرات فنية بايثون نقية — بدون numpy/pandas (خفّة + قابلية اختبار).

تعمل على قوائم أسعار/شموع. كل دالة فاشلة-آمنة: ترجّع قيمة محايدة عند
نقص البيانات بدل رفع استثناء (أسهم حديثة الإدراج = تاريخ محدود).
"""

from __future__ import annotations

from typing import Sequence

from .models import Bar


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """سلسلة EMA كاملة. فارغة لو البيانات أقل من الفترة."""
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """RSI (Wilder). يرجّع None لو التاريخ أقل من period+1."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    # المتوسط الأولي على أول period تغيّرات
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # التنعيم على البقية
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[float, float] | None:
    """يرجّع (خط MACD، خط الإشارة) أو None لو التاريخ قصير."""
    if len(values) < slow + signal:
        return None
    fast_e = ema_series(values, fast)
    slow_e = ema_series(values, slow)
    if not fast_e or not slow_e:
        return None
    # محاذاة الذيول (slow أقصر بفارق slow-fast)
    n = min(len(fast_e), len(slow_e))
    macd_line = [fast_e[-n + i] - slow_e[-n + i] for i in range(n)]
    sig = ema_series(macd_line, signal)
    if not sig:
        return None
    return macd_line[-1], sig[-1]


def linreg_slope_pct(values: Sequence[float]) -> float:
    """ميل الانحدار الخطي كنسبة% من متوسط السعر (تقدير اتجاه Dow).

    موجب = صاعد، سالب = هابط. صفر لو أقل من نقطتين.
    """
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0 or mean_y == 0:
        return 0.0
    slope = num / den
    # نطبّع: ميل لكل شمعة ÷ متوسط السعر × 100، مضروب في n لتمثيل المدى الكلي
    return (slope / mean_y) * 100.0 * n


def pivots(values: Sequence[float], left: int = 2, right: int = 2) -> tuple[list[int], list[int]]:
    """قمم وقيعان محورية. يرجّع (مؤشرات القمم، مؤشرات القيعان)."""
    highs, lows = [], []
    n = len(values)
    for i in range(left, n - right):
        window = values[i - left:i + right + 1]
        if values[i] == max(window) and window.count(values[i]) == 1:
            highs.append(i)
        if values[i] == min(window) and window.count(values[i]) == 1:
            lows.append(i)
    return highs, lows


def trend_label(slope_pct: float, flat_band: float = 1.0) -> str:
    if slope_pct > flat_band:
        return "صاعد"
    if slope_pct < -flat_band:
        return "هابط"
    return "عرضي"


def detect_divergence(closes: Sequence[float], rsis: Sequence[float]) -> str:
    """دايفرجنس بسيط على آخر قاعين/قمتين. صاعد/هابط/لا شيء."""
    if len(closes) < 10 or len(rsis) < 10:
        return "لا شيء"
    _, price_lows = pivots(closes)
    price_highs, _ = pivots(closes)
    # صاعد: سعر قاع أدنى + RSI قاع أعلى
    if len(price_lows) >= 2:
        a, b = price_lows[-2], price_lows[-1]
        if b < len(rsis) and a < len(rsis):
            if closes[b] < closes[a] and rsis[b] > rsis[a]:
                return "صاعد"
    # هابط: سعر قمة أعلى + RSI قمة أدنى
    if len(price_highs) >= 2:
        a, b = price_highs[-2], price_highs[-1]
        if b < len(rsis) and a < len(rsis):
            if closes[b] > closes[a] and rsis[b] < rsis[a]:
                return "هابط"
    return "لا شيء"


def rsi_series(closes: Sequence[float], period: int = 14) -> list[float]:
    """سلسلة RSI (قيمة لكل نقطة؛ 50 حيث التاريخ غير كافٍ)."""
    out: list[float] = []
    for i in range(len(closes)):
        val = rsi(closes[: i + 1], period)
        out.append(val if val is not None else 50.0)
    return out


def stoch_rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Stochastic RSI (0..1): موقع RSI الحالي ضمن مدى آخر period قيمة.
    ≥0.8 تشبّع شرائي · ≤0.2 تشبّع بيعي."""
    if len(closes) < period * 2:
        return None
    rs = rsi_series(closes, period)[-period:]
    lo, hi = min(rs), max(rs)
    if hi - lo < 1e-9:
        return 0.5
    return (rs[-1] - lo) / (hi - lo)


def bollinger_pct_b(closes: Sequence[float], period: int = 20,
                    k: float = 2.0) -> float | None:
    """%B = موقع السعر بين الباندَين (>1 فوق العلوي = ممتد، <0 تحت السفلي)."""
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mid = sum(window) / period
    sd = (sum((c - mid) ** 2 for c in window) / period) ** 0.5
    upper, lower = mid + k * sd, mid - k * sd
    if upper - lower < 1e-9:
        return 0.5
    return (closes[-1] - lower) / (upper - lower)


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """تنعيم وايلدر التراكمي (يُستخدم في ADX)."""
    if len(values) < period:
        return []
    s = sum(values[:period])
    out = [s]
    for v in values[period:]:
        s = s - s / period + v
        out.append(s)
    return out


def adx_dmi(highs: Sequence[float], lows: Sequence[float],
            closes: Sequence[float], period: int = 14
            ) -> tuple[float, float, float] | None:
    """يرجّع (ADX، +DI، −DI) بطريقة وايلدر. ADX يقيس القوة (لا الوجهة):
    >25 اتجاه فعّال · >40 قوي جدًا. +DI>−DI = تحيّز صاعد."""
    n = len(closes)
    if n < period * 2 + 1:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = _wilder_smooth(trs, period)
    pdm = _wilder_smooth(plus_dm, period)
    mdm = _wilder_smooth(minus_dm, period)
    if not atr or len(atr) != len(pdm):
        return None
    plus_di = [100 * pdm[i] / atr[i] if atr[i] > 0 else 0.0
               for i in range(len(atr))]
    minus_di = [100 * mdm[i] / atr[i] if atr[i] > 0 else 0.0
                for i in range(len(atr))]
    dx = []
    for i in range(len(atr)):
        denom = plus_di[i] + minus_di[i]
        dx.append(100 * abs(plus_di[i] - minus_di[i]) / denom if denom > 0 else 0.0)
    if len(dx) < period:
        return None
    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period
    return adx, plus_di[-1], minus_di[-1]


def session_vwap(bars: Sequence[Bar]) -> float | None:
    """VWAP جلسي مُجمَّع من شموع الدقيقة (sum(typical×vol)/sum(vol))."""
    tot_pv, tot_v = 0.0, 0.0
    for b in bars:
        if b.v <= 0:
            continue
        typical = (b.h + b.l + b.c) / 3.0
        tot_pv += typical * b.v
        tot_v += b.v
    if tot_v <= 0:
        return None
    return tot_pv / tot_v
