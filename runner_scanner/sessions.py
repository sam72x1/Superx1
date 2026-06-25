"""كشف الجلسة (بريماركت/رسمي/أفترهاوس) بتوقيت نيويورك + مساعدات RVol.

نقطة حرجة من تحقّق الـ API:
- السنابشوت يُمسح يوميًا 3:30–4:00ص ET → نمنع المسح قبل 4:00ص.
- RVol لازم يُحسب حسب الجلسة (حجم البريماركت مقابل متوسط بريماركت)،
  وإلا أي قفزة رقيقة تبان «ضخمة» كذبًا.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import Config
from .models import Session

ET = ZoneInfo("America/New_York")

# كسور الجلسة المعتادة من اليوم التداولي الكامل (بريماركت+رسمي+أفترهاوس)،
# تُستخدم لتطبيع متوسط الحجم اليومي إلى «متوسط متوقّع حتى هذي اللحظة».
# قيم تقريبية محافِظة؛ قابلة للمعايرة لاحقًا.
_REGULAR_MINUTES = 390.0   # 6.5 ساعة × 60


def now_et() -> datetime:
    """الوقت الحالي بتوقيت نيويورك (يحترم التوقيت الصيفي تلقائيًا)."""
    return datetime.now(ET)


def _hour_float(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def classify_session(cfg: Config, dt: datetime | None = None) -> Session:
    """يحدّد الجلسة الحالية. الأحد–السبت: يُعامل الويكند كمغلق."""
    dt = dt or now_et()
    # 5 = السبت، 6 = الأحد
    if dt.weekday() >= 5:
        return Session.CLOSED

    h = _hour_float(dt)
    if cfg.premarket_start_hour <= h < cfg.regular_start_hour:
        return Session.PREMARKET
    if cfg.regular_start_hour <= h < cfg.regular_end_hour:
        return Session.REGULAR
    if cfg.regular_end_hour <= h < cfg.afterhours_end_hour:
        return Session.AFTERHOURS
    return Session.CLOSED


def is_scanning_window(cfg: Config, dt: datetime | None = None) -> bool:
    """هل نحن داخل نافذة مسح صالحة؟ (أي جلسة غير مغلقة).

    يضمن أيضًا أننا بعد 4:00ص ET (بعد إعادة ملء السنابشوت).
    """
    return classify_session(cfg, dt) is not Session.CLOSED


def session_elapsed_fraction(cfg: Config, dt: datetime | None = None) -> float:
    """كسر اليوم الرسمي المنقضي [~0.02 .. 1.0] لتطبيع RVol أثناء الجلسة الرسمية.

    مثال: الساعة 10:30ص (مرّت ساعة من 9:30) → ~0.154.
    نقصّ الكسر بحد أدنى صغير لتجنّب القسمة على صفر أول الجلسة.
    """
    dt = dt or now_et()
    h = _hour_float(dt)
    elapsed_min = (h - cfg.regular_start_hour) * 60.0
    frac = elapsed_min / _REGULAR_MINUTES
    return max(0.02, min(1.0, frac))


def compute_rvol(
    cfg: Config,
    session: Session,
    cumulative_volume: float,
    avg_daily_volume: float,
    elapsed_fraction: float | None = None,
    avg_premarket_volume: float | None = None,
    avg_afterhours_volume: float | None = None,
    dt: datetime | None = None,
) -> float:
    """يحسب RVol حسب الجلسة.

    - رسمي: حجم اليوم حتى الآن ÷ (متوسط يومي × كسر الجلسة المنقضي).
    - بريماركت: حجم البريماركت ÷ متوسط حجم البريماركت (لا اليومي).
    - أفترهاوس: حجم الأفترهاوس ÷ متوسط حجم الأفترهاوس.

    لو غاب المتوسط الخاص بالجلسة، نسقط بأمان لتقدير من المتوسط اليومي
    (مع تطبيع تقريبي) بدل إرجاع رقم مضلّل.
    """
    if avg_daily_volume <= 0 and not (avg_premarket_volume or avg_afterhours_volume):
        return 0.0

    if session is Session.REGULAR:
        frac = elapsed_fraction if elapsed_fraction is not None else \
            session_elapsed_fraction(cfg, dt)
        expected = max(1.0, avg_daily_volume * frac)
        return cumulative_volume / expected

    if session is Session.PREMARKET:
        baseline = avg_premarket_volume
        if not baseline or baseline <= 0:
            # تقدير محافظ: البريماركت ~3% من اليوم في المتوسط.
            baseline = max(1.0, avg_daily_volume * 0.03)
        return cumulative_volume / baseline

    if session is Session.AFTERHOURS:
        baseline = avg_afterhours_volume
        if not baseline or baseline <= 0:
            baseline = max(1.0, avg_daily_volume * 0.05)
        return cumulative_volume / baseline

    return 0.0
