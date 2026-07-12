"""كاش يومي بسيط للبيانات البطيئة (لا تتغيّر خلال اليوم).

الأسهم المرفوضة والأبطال يُعاد تحليلها كل دورة → بدون كاش نعيد جلب
نفس الـ overview/شموع اليومي/الساعة/الفلوت تكرارًا كل 45ث (هدر نداءات
وبطء). نكاش هذي لكل (سهم/يوم)، ونُبقي الـ 5د/1د طازجة.

الخبر حالة وسطى: يتغيّر خلال اليوم لكن نظرته الخلفية 48 ساعة، فنداؤه كل
دورة لكل مرشّح هدر (آلاف النداءات المتطابقة يوميًا). نكاشه بحبيبة ثوانٍ
(TTL) عبر `get_ttl` — محفّز عمره بضع دقائق مقبول.

يُمسح تلقائيًا عند تغيّر يوم التداول.
"""

from __future__ import annotations

import time
from typing import Callable


class DailyCache:
    """كاش مفاتيح نصّية يُعاد ضبطه عند تغيّر اليوم (ومدخلات TTL بجانبه)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._day: str | None = None
        self._store: dict[str, object] = {}
        # مدخلات TTL منفصلة: key → (value, stamped_at) — لا تختلط بمخزن اليوم.
        self._ttl: dict[str, tuple[object, float]] = {}
        self._clock = clock

    def _roll_day(self, day: str) -> None:
        """يمسح كل شيء عند تغيّر يوم التداول (بما فيه مدخلات TTL)."""
        if day != self._day:
            self._day = day
            self._store.clear()
            self._ttl.clear()

    def get(self, day: str, key: str, fetch: Callable[[], object]) -> object:
        """يرجّع القيمة المخزّنة أو يجلبها ويخزّنها. day = يوم التداول (ET)."""
        self._roll_day(day)
        if key not in self._store:
            self._store[key] = fetch()
        return self._store[key]

    def get_ttl(self, day: str, key: str, ttl_sec: float,
                fetch: Callable[[], object]) -> object:
        """كاش بحبيبة ثوانٍ (TTL): يجلب من جديد فقط إن مضى ≥ ttl_sec على آخر
        جلب لنفس المفتاح. للبيانات التي تتغيّر خلال اليوم لكن نداءها كل دورة
        هدر (الخبر). يُمسح كذلك عند تغيّر اليوم."""
        self._roll_day(day)
        hit = self._ttl.get(key)
        now = self._clock()
        if hit is not None and (now - hit[1]) < ttl_sec:
            return hit[0]
        value = fetch()
        self._ttl[key] = (value, now)
        return value

    def clear(self) -> None:
        self._store.clear()
        self._ttl.clear()
        self._day = None
