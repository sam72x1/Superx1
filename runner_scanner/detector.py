"""كشف عبور +20% عن إغلاق أمس من Full Market Snapshot.

المبدأ (القسم 10): لا نستخدم قائمة gainers الجاهزة (تستبعد الحجم <10K
فتخفي الأسهم المبكّرة). بدلها نمسح السنابشوت الكامل ونفلتر بأنفسنا.

نقّي قبل قصّ الـ15 (وإلا تأخذ التشوّهات والمشتقات مقاعد بلا تعويض):
- **سقف التغيّر** يسقط تشوّه الانقسام العكسي (سهم يطلع +800% بيانةً لا حركةً).
- **استبعاد المشتقات** بالرمز: الوارنتات/اليونتات/الحقوق (لاحقة بعد فاصل،
  أو 5 أحرف تنتهي بـ W/U/R حسب عُرف ناسداك). الفلتر الموثوق (نوع الورقة)
  يكمّل لاحقًا في البوّابة.
"""

from __future__ import annotations

from .config import Config
from .models import SnapshotEntry

# لواحق المشتقات بعد فاصل صريح (ABC.WS / ABC.U / ABC.RT). A/B = فئات عادية.
_DERIV_DOT_SUFFIXES = {"WS", "WT", "W", "U", "UN", "R", "RT", "RW", "RTW"}
# الحرف الخامس في رموز ناسداك: W=وارنت، U=يونت، R=رايت
_DERIV_LAST_CHARS = {"W", "U", "R"}


def looks_like_derivative(symbol: str) -> bool:
    """تخمين رخيص: هل الرمز وارنت/يونت/رايت (لا سهم عادي)؟"""
    s = (symbol or "").upper()
    for sep in (".", "+", "=", "/", " "):
        if sep in s:
            suffix = s.split(sep, 1)[1]
            if suffix in _DERIV_DOT_SUFFIXES:
                return True
    if len(s) == 5 and s.isalpha() and s[-1] in _DERIV_LAST_CHARS:
        return True
    return False


def detect_runners(cfg: Config, snapshot: list[SnapshotEntry]) -> list[SnapshotEntry]:
    """يرجّع المداخل اللي كسرت العتبة (وتحت السقف)، مرتّبة تنازليًا.

    يتجاهل: غير الصالح (نافذة المسح)، فوق السقف (تشوّه انقسام)، والمشتقات.
    """
    runners = []
    for e in snapshot:
        if not e.is_valid:
            continue
        if not (cfg.trigger_change_pct <= e.change_pct <= cfg.max_change_pct):
            continue
        if cfg.filter_derivatives and looks_like_derivative(e.ticker):
            continue
        runners.append(e)
    runners.sort(key=lambda e: e.change_pct, reverse=True)
    return runners
