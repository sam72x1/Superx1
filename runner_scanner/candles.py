"""كشف نماذج الشموع اليابانية (مرجع التحليل الكلاسيكي).

المبدأ الحاكم من المرجع: **الشمعة بلا سياق ضعيفة** — فالنماذج الانعكاسية
تُعتبر فقط في سياقها (انعكاس هبوطي بعد اتجاه صاعد/عند مقاومة، وانعكاس
صعودي بعد اتجاه هابط/عند دعم).

`candle_signal(bars)` يرجّع (إشارة في [-1..+1]، الاسم):
  موجب = صعودي (يرفع الجاهزية) · سالب = هبوطي (يخفضها — تحذير قمة للرَنر).
نأخذ أقوى نموذج على آخر 1–3 شموع. للرَنر (في صعود) أهمّها نماذج القمة
الهبوطية (الشهاب/الابتلاع الهابط/نجمة المساء/الغربان...).
"""

from __future__ import annotations

from typing import Sequence

from .indicators import linreg_slope_pct
from .models import Bar


def _body(b: Bar) -> float:
    return abs(b.c - b.o)


def _rng(b: Bar) -> float:
    return (b.h - b.l) or 1e-9


def _upper(b: Bar) -> float:
    return b.h - max(b.o, b.c)


def _lower(b: Bar) -> float:
    return min(b.o, b.c) - b.l


def _green(b: Bar) -> bool:
    return b.c > b.o


def _red(b: Bar) -> bool:
    return b.c < b.o


def _is_doji(b: Bar) -> bool:
    return _body(b) <= 0.1 * _rng(b)


def _trend_before(closes: Sequence[float], k: int) -> str:
    """اتجاه ما قبل النموذج (الذي يبدأ عند العنصر -k). up/down/flat."""
    end = len(closes) - k
    seg = closes[max(0, end - 7):end]
    if len(seg) < 3:
        return "flat"
    slope = linreg_slope_pct(seg)
    if slope > 1.0:
        return "up"
    if slope < -1.0:
        return "down"
    return "flat"


def candle_signal(bars: Sequence[Bar]) -> tuple[float, str]:
    """أقوى إشارة شمعية على آخر 1–3 شموع (مع سياق الاتجاه)."""
    if len(bars) < 4:
        return 0.0, ""
    closes = [b.c for b in bars]
    b1, b2, b3 = bars[-3], bars[-2], bars[-1]  # الأقدم → الأحدث
    last = b3

    best_sig, best_name = 0.0, ""

    def consider(sig: float, name: str) -> None:
        nonlocal best_sig, best_name
        if abs(sig) > abs(best_sig):
            best_sig, best_name = sig, name

    # ── ثلاثية (الأقوى) ──────────────────────────────────────────
    up_before3 = _trend_before(closes, 3) == "up"
    down_before3 = _trend_before(closes, 3) == "down"
    # نجمة المساء: صاعدة كبيرة · نجمة صغيرة · هابطة تغلق عميقًا بالأولى
    if (up_before3 and _green(b1) and _body(b2) < _body(b1) * 0.5
            and _red(b3) and b3.c < (b1.o + b1.c) / 2):
        consider(-1.0, "نجمة المساء")
    # نجمة الصباح
    if (down_before3 and _red(b1) and _body(b2) < _body(b1) * 0.5
            and _green(b3) and b3.c > (b1.o + b1.c) / 2):
        consider(1.0, "نجمة الصباح")
    # ثلاثة غربان سود
    if (_red(b1) and _red(b2) and _red(b3)
            and b2.c < b1.c and b3.c < b2.c
            and _lower(b2) < _body(b2) * 0.3 and _lower(b3) < _body(b3) * 0.3):
        consider(-0.9, "ثلاثة غربان سود")
    # ثلاثة جنود بيض
    if (_green(b1) and _green(b2) and _green(b3)
            and b2.c > b1.c and b3.c > b2.c
            and _upper(b2) < _body(b2) * 0.3 and _upper(b3) < _body(b3) * 0.3):
        consider(0.9, "ثلاثة جنود بيض")

    # ── ثنائية ───────────────────────────────────────────────────
    up_before2 = _trend_before(closes, 2) == "up"
    down_before2 = _trend_before(closes, 2) == "down"
    # ابتلاع هابط: صاعدة صغيرة ثم هابطة تبتلعها
    if (up_before2 and _green(b2) and _red(b3)
            and b3.o >= b2.c and b3.c <= b2.o and _body(b3) > _body(b2)):
        consider(-0.8, "ابتلاع هابط")
    # ابتلاع صاعد
    if (down_before2 and _red(b2) and _green(b3)
            and b3.o <= b2.c and b3.c >= b2.o and _body(b3) > _body(b2)):
        consider(0.8, "ابتلاع صاعد")
    # غطاء داكن: صاعدة كبيرة ثم هابطة تفتح فوق وتغلق تحت منتصفها
    if (up_before2 and _green(b2) and _red(b3) and b3.o > b2.h
            and b3.c < (b2.o + b2.c) / 2 and b3.c > b2.o):
        consider(-0.7, "غطاء داكن")
    # خط الثقب
    if (down_before2 and _red(b2) and _green(b3) and b3.o < b2.l
            and b3.c > (b2.o + b2.c) / 2 and b3.c < b2.o):
        consider(0.7, "خط الثقب")

    # ── أحادية (آخر شمعة) ────────────────────────────────────────
    up_before1 = _trend_before(closes, 1) == "up"
    down_before1 = _trend_before(closes, 1) == "down"
    body, up_sh, lo_sh = _body(last), _upper(last), _lower(last)
    small_body = body <= 0.35 * _rng(last)
    # شهاب: جسم صغير سفلي + ظل علوي طويل، بعد صعود
    if (up_before1 and small_body and up_sh >= 2 * body
            and lo_sh <= body):
        consider(-0.6, "شهاب")
    # مطرقة: جسم صغير علوي + ظل سفلي طويل، بعد هبوط
    if (down_before1 and small_body and lo_sh >= 2 * body
            and up_sh <= body):
        consider(0.6, "مطرقة")
    # شاهد القبر (دوجي ظل علوي) بعد صعود
    if (up_before1 and _is_doji(last) and up_sh >= 2 * _rng(last) * 0.4
            and lo_sh <= _rng(last) * 0.1):
        consider(-0.6, "شاهد القبر")
    # اليعسوب (دوجي ظل سفلي) بعد هبوط
    if (down_before1 and _is_doji(last) and lo_sh >= 2 * _rng(last) * 0.4
            and up_sh <= _rng(last) * 0.1):
        consider(0.6, "اليعسوب")
    # رجل مشنوق: شكل المطرقة لكن بعد صعود = هبوطي
    if (up_before1 and small_body and lo_sh >= 2 * body and up_sh <= body):
        consider(-0.55, "رجل مشنوق")
    # ماروبوزو (زخم): جسم كامل بلا ظلال
    if up_sh <= 0.05 * _rng(last) and lo_sh <= 0.05 * _rng(last) \
            and body >= 0.9 * _rng(last):
        consider(0.6 if _green(last) else -0.5,
                 "ماروبوزو صاعد" if _green(last) else "ماروبوزو هابط")

    return round(best_sig, 2), best_name
