"""تقويم سوق نيويورك: العطلات الرسمية وأيام الإغلاق المبكر.

يُحسب لأي سنة برمجيًا (بما فيها الجمعة العظيمة عبر حساب عيد الفصح)،
فلا حاجة لقائمة ثابتة تنتهي صلاحيتها.

- عطلة كاملة → السوق مغلق طوال اليوم.
- إغلاق مبكر (نصف يوم) → الجلسة الرسمية تنتهي 1:00م ET، بلا أفترهاوس.

قاعدة «اليوم المُلاحَظ» (NYSE): العطلة يوم الأحد → تُلاحَظ الاثنين؛
يوم السبت → تُلاحَظ الجمعة السابقة.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

EARLY_CLOSE_HOUR = 13.0   # 1:00م ET في أيام نصف-اليوم


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """التكرار رقم n ليوم weekday (Mon=0) في الشهر."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """آخر تكرار ليوم weekday في الشهر."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """عيد الفصح (خوارزمية غريغوريان المجهولة) — لحساب الجمعة العظيمة."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """اليوم المُلاحَظ: الأحد → الاثنين، السبت → الجمعة."""
    if d.weekday() == 6:        # الأحد
        return d + timedelta(days=1)
    if d.weekday() == 5:        # السبت
        return d - timedelta(days=1)
    return d


@lru_cache(maxsize=16)
def holidays(year: int) -> frozenset[date]:
    """عطلات NYSE الكاملة (مُلاحَظة) للسنة."""
    h = {
        _observed(date(year, 1, 1)),                 # رأس السنة
        _nth_weekday(year, 1, 0, 3),                 # MLK (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),                 # واشنطن (3rd Mon Feb)
        _easter(year) - timedelta(days=2),           # الجمعة العظيمة
        _last_weekday(year, 5, 0),                   # ميموريال (آخر اثنين مايو)
        _observed(date(year, 6, 19)),                # جونتينث
        _observed(date(year, 7, 4)),                 # الاستقلال
        _nth_weekday(year, 9, 0, 1),                 # العمال (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),                # الشكر (4th Thu Nov)
        _observed(date(year, 12, 25)),               # الكريسماس
    }
    return frozenset(h)


@lru_cache(maxsize=16)
def early_closes(year: int) -> frozenset[date]:
    """أيام الإغلاق المبكر (1م ET): قبيل الاستقلال، الجمعة السوداء، عيد الميلاد."""
    out: set[date] = set()
    # اليوم السابق للاستقلال (3 يوليو) إن كان يوم تداول
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5:
        out.add(jul3)
    # الجمعة السوداء (اليوم التالي للشكر)
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
    # عشية الكريسماس (24 ديسمبر) إن كانت يوم تداول
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5:
        out.add(dec24)
    # نستبعد ما يصادف عطلة كاملة
    return frozenset(out - holidays(year))


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


def is_early_close(d: date) -> bool:
    return d in early_closes(d.year)
