"""فحص الخبر/المحفّز — إشارة تقوية للدرجة، لا بوّابة (قرار المستخدم).

أخبار Massive مصدرها Benzinga + أسلاك PR، مو شاملة لرَنرات الـ small-cap.
لذا: وجود خبر حديث يرفع الدرجة ويوسم «محفّز ✓»؛ غيابه لا يرفض السهم.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Config
from .models import Catalyst


def lookback_iso(cfg: Config, now_utc: datetime | None = None) -> str:
    """طابع RFC3339 (UTC) لبداية نافذة «خبر حديث»."""
    now_utc = now_utc or datetime.now(timezone.utc)
    start = now_utc - timedelta(hours=cfg.catalyst_lookback_hours)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_published(published_utc: str) -> datetime | None:
    if not published_utc:
        return None
    txt = published_utc.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate_catalyst(cfg: Config, catalyst: Catalyst | None,
                      now_utc: datetime | None = None) -> Catalyst:
    """يطبّع نتيجة الخبر ويحسب عمره بالساعات.

    يقبل None (لا خبر) ويرجّع Catalyst(has_news=False).
    """
    if catalyst is None or not catalyst.has_news:
        return Catalyst(has_news=False)

    now_utc = now_utc or datetime.now(timezone.utc)
    pub = _parse_published(catalyst.published_utc)
    if pub is not None:
        catalyst.age_hours = max(0.0, (now_utc - pub).total_seconds() / 3600.0)
        # خبر خارج النافذة → نعامله كأنه لا محفّز فعّال
        if catalyst.age_hours > cfg.catalyst_lookback_hours:
            return Catalyst(has_news=False)
    return catalyst


def catalyst_bonus(cfg: Config, catalyst: Catalyst | None) -> float:
    """مقدار التقوية المضافة للدرجة عند وجود محفّز ضمن النافذة."""
    if catalyst is not None and catalyst.has_news:
        return cfg.catalyst_score_bonus
    return 0.0
