"""بديل خفيف لموجات إليوت — تمييز البنية الدافعة من التصحيحية.

المرجع يؤكّد أن **عدّ إليوت الكامل ذاتي وغير موثوق آليًا**، ويربط موجاته
بنظرية داو (القمم/القيعان). فبدل العدّ الكامل، نقرأ **هيكل السوق** عبر
تسلسل القمم/القيعان المحورية:

  HH + HL = بنية **دافعة صاعدة** (impulse up) → جاهزية أعلى.
  LH + LL = بنية **دافعة هابطة** (impulse down) → جاهزية أدنى.
  متداخلة/مختلطة = **تصحيحية/عرضية** (corrective) → محايد.

هذا يلتقط ما لا يلتقطه ميل الانحدار وحده (سعر صاعد لكن بنية متذبذبة).
"""

from __future__ import annotations

from typing import Sequence

from .indicators import pivots
from .models import Bar


def wave_structure(bars: Sequence[Bar]) -> tuple[float, str]:
    """يرجّع (إشارة في [-1..+1]، التصنيف) من تسلسل القمم/القيعان."""
    if len(bars) < 12:
        return 0.0, ""
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    hi_idx, _ = pivots(highs)
    _, lo_idx = pivots(lows)
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return 0.0, ""

    hh = highs[hi_idx[-1]] > highs[hi_idx[-2]]   # قمة أعلى
    lh = highs[hi_idx[-1]] < highs[hi_idx[-2]]   # قمة أدنى
    hl = lows[lo_idx[-1]] > lows[lo_idx[-2]]      # قاع أعلى
    ll = lows[lo_idx[-1]] < lows[lo_idx[-2]]      # قاع أدنى

    if hh and hl:
        return 1.0, "دافعة صاعدة"
    if lh and ll:
        return -1.0, "دافعة هابطة"
    return 0.0, "تصحيحية"
