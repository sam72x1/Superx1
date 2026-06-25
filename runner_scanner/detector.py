"""كشف عبور +20% عن إغلاق أمس من Full Market Snapshot.

المبدأ (القسم 10): لا نستخدم قائمة gainers الجاهزة (تستبعد الحجم <10K
فتخفي الرَنرات المبكّرة). بدلها نمسح السنابشوت الكامل ونفلتر بأنفسنا.
"""

from __future__ import annotations

from .config import Config
from .models import SnapshotEntry


def detect_runners(cfg: Config, snapshot: list[SnapshotEntry]) -> list[SnapshotEntry]:
    """يرجّع المداخل اللي كسرت +20% (أو العتبة المضبوطة) عن إغلاق أمس.

    يتجاهل المداخل غير الصالحة (بدون سعر/إغلاق أمس — بيانات نافذة المسح
    3:30–4:00ص). يرتّب تنازليًا حسب نسبة التغيّر.
    """
    runners = [
        e for e in snapshot
        if e.is_valid and e.change_pct >= cfg.trigger_change_pct
    ]
    runners.sort(key=lambda e: e.change_pct, reverse=True)
    return runners
