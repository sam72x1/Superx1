"""كشف الجلسة (بريماركت/رسمي/أفترهاوس) بتوقيت نيويورك + مساعدات RVol.

نقطة حرجة من تحقّق الـ API:
- السنابشوت يُمسح يوميًا 3:30–4:00ص ET → نمنع المسح قبل 4:00ص.
- RVol لازم يُحسب حسب الجلسة (حجم البريماركت مقابل متوسط بريماركت)،
  وإلا أي قفزة رقيقة تبان «ضخمة» كذبًا.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import market_calendar
from .config import Config
from .models import Bar, Session

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
    """يحدّد الجلسة الحالية. يحترم الويكند والعطلات والإغلاق المبكر."""
    dt = dt or now_et()
    # 5 = السبت، 6 = الأحد
    if dt.weekday() >= 5:
        return Session.CLOSED
    if market_calendar.is_holiday(dt.date()):   # عطلة كاملة
        return Session.CLOSED

    # إغلاق مبكر (نصف يوم): الجلسة الرسمية تنتهي 1م، بلا أفترهاوس
    if market_calendar.is_early_close(dt.date()):
        regular_end = market_calendar.EARLY_CLOSE_HOUR
        afterhours_end = market_calendar.EARLY_CLOSE_HOUR
    else:
        regular_end = cfg.regular_end_hour
        afterhours_end = cfg.afterhours_end_hour

    h = _hour_float(dt)
    if cfg.premarket_start_hour <= h < cfg.regular_start_hour:
        return Session.PREMARKET
    if cfg.regular_start_hour <= h < regular_end:
        return Session.REGULAR
    if regular_end <= h < afterhours_end:
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


def session_volume_baselines(
    cfg: Config, hourly_bars: list[Bar], today_et: str | None = None,
) -> tuple[float | None, float | None]:
    """متوسط حجم **البريماركت** و**الأفترهاوس** الفعلي من شموع الساعة التاريخية.

    يصنّف كل شمعة ساعة حسب توقيت ET، يجمّع حجم كل جلسة لكل يوم، ثم يتوسّط
    عبر الأيام (يستثني يوم اليوم لتجنّب التحيّز بالبيانات الجزئية).
    يرجّع (متوسط بريماركت، متوسط أفترهاوس) أو None لكلٍّ عند غياب البيانات.
    """
    if not hourly_bars:
        return None, None
    pre: dict[str, float] = defaultdict(float)
    aft: dict[str, float] = defaultdict(float)
    for b in hourly_bars:
        if b.v <= 0 or b.t_ms <= 0:
            continue
        dt = datetime.fromtimestamp(b.t_ms / 1000, tz=timezone.utc).astimezone(ET)
        day = dt.strftime("%Y-%m-%d")
        if today_et and day == today_et:
            continue   # استثناء اليوم (بيانات جزئية)
        h = _hour_float(dt)
        if cfg.premarket_start_hour <= h < cfg.regular_start_hour:
            pre[day] += b.v
        elif cfg.regular_end_hour <= h < cfg.afterhours_end_hour:
            aft[day] += b.v
    avg_pre = sum(pre.values()) / len(pre) if pre else None
    avg_aft = sum(aft.values()) / len(aft) if aft else None
    return avg_pre, avg_aft


def _session_window(cfg: Config, session: Session) -> tuple[float, float]:
    """حدود الساعة (ET) للجلسة، لتصفية الشموع التي تخصّها."""
    if session is Session.PREMARKET:
        return cfg.premarket_start_hour, cfg.regular_start_hour
    if session is Session.REGULAR:
        return cfg.regular_start_hour, cfg.regular_end_hour
    if session is Session.AFTERHOURS:
        return cfg.regular_end_hour, cfg.afterhours_end_hour
    return 0.0, 24.0


def session_cumulative_volume(cfg: Config, session: Session,
                              bars: list[Bar]) -> float:
    """الحجم التراكمي **الفعلي** للجلسة الحالية من الشموع (لا من snap.day_volume
    الذي قد يكون صفرًا/قديمًا في البريماركت). يصفّي الشموع ضمن نافذة الجلسة (ET).
    """
    if not bars:
        return 0.0
    lo, hi = _session_window(cfg, session)
    total = 0.0
    for b in bars:
        if b.v <= 0 or b.t_ms <= 0:
            continue
        dt = datetime.fromtimestamp(b.t_ms / 1000, tz=timezone.utc).astimezone(ET)
        h = _hour_float(dt)
        if lo <= h < hi:
            total += b.v
    return total


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

    # حدّ أدنى لمتوسط يومي «موثوق» قبل الاعتماد عليه في تقدير baseline الجلسة،
    # وإلا فمتوسط جزئي صغير (شمعة واحدة) يصنع baseline ضئيلًا → RVol منفوخ كذبًا.
    daily_ok = avg_daily_volume >= cfg.volume_min * 0.1

    if session is Session.PREMARKET:
        baseline = avg_premarket_volume
        if not baseline or baseline <= 0:
            if not daily_ok:
                return 0.0   # لا أساس موثوق → لا نفبرك RVol
            baseline = avg_daily_volume * 0.03   # البريماركت ~3% من اليوم
        return cumulative_volume / baseline

    if session is Session.AFTERHOURS:
        baseline = avg_afterhours_volume
        if not baseline or baseline <= 0:
            if not daily_ok:
                return 0.0
            baseline = avg_daily_volume * 0.05
        return cumulative_volume / baseline

    return 0.0
