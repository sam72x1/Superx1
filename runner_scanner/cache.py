"""كاش يومي بسيط للبيانات البطيئة (لا تتغيّر خلال اليوم).

الأسهم المرفوضة والأبطال يُعاد تحليلها كل دورة → بدون كاش نعيد جلب
نفس الـ overview/شموع اليومي/الساعة/الفلوت تكرارًا كل 45ث (هدر نداءات
وبطء). نكاش هذي لكل (سهم/يوم)، ونُبقي الـ 5د/1د/الأخبار طازجة.

يُمسح تلقائيًا عند تغيّر يوم التداول.
"""

from __future__ import annotations

from typing import Callable


class DailyCache:
    """كاش مفاتيح نصّية يُعاد ضبطه عند تغيّر اليوم."""

    def __init__(self) -> None:
        self._day: str | None = None
        self._store: dict[str, object] = {}

    def get(self, day: str, key: str, fetch: Callable[[], object]) -> object:
        """يرجّع القيمة المخزّنة أو يجلبها ويخزّنها. day = يوم التداول (ET)."""
        if day != self._day:
            self._day = day
            self._store.clear()
        if key not in self._store:
            self._store[key] = fetch()
        return self._store[key]

    def clear(self) -> None:
        self._store.clear()
        self._day = None
