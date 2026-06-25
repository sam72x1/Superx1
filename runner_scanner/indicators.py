"""مؤشرات فنية بايثون نقية — بدون numpy/pandas (خفّة + قابلية اختبار).

تعمل على قوائم أسعار/شموع. كل دالة فاشلة-آمنة: ترجّع قيمة محايدة عند
نقص البيانات بدل رفع استثناء (رَنرات حديثة الإدراج = تاريخ محدود).
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
