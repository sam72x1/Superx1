"""فحص الخبر/المحفّز — إشارة تقوية للدرجة، لا بوّابة (قرار المستخدم).

أخبار Massive مصدرها Benzinga + أسلاك PR، مو شاملة لأسهم الـ small-cap.
لذا: وجود خبر حديث يرفع الدرجة ويوسم «محفّز ✓»؛ غيابه لا يرفض السهم.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Config
from .models import Catalyst

# فئة الخبر السلبي (طرح/تخفيف) — تضرّ السهم فلا تُمنح مكافأة درجة.
NEGATIVE_NEWS = "⚠️ طرح/تخفيف (سلبي)"

# تصنيف الخبر من كلمات مفتاحية في العنوان/الوصف (أخبار Massive إنجليزية).
# الترتيب يهمّ: الأكثر دلالةً أولًا. كل عنصر: (تسمية عربية، كلمات مفتاحية).
_NEWS_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    (NEGATIVE_NEWS,
     ("offering", "dilut", "priced", "registered direct", "atm ",
      "shelf", "warrant", "raise", "private placement")),
    ("💊 موافقة/تجارب سريرية",
     ("fda", "approval", "approve", "clinical", "phase 1", "phase 2",
      "phase 3", "trial", "therapy", "drug", "ind ", "510(k)", "breakthrough")),
    ("🔀 اندماج/استحواذ",
     ("merger", "acqui", "buyout", "to be acquired", "takeover", "tender offer")),
    ("🤝 شراكة",
     ("partner", "collaborat", "teams up", "joint venture", "alliance")),
    ("📑 عقد/صفقة",
     ("contract", "awarded", "purchase order", "deal", "selected by", "wins")),
    ("📈 أرباح/نتائج مالية",
     ("earnings", "revenue", "eps", "beats", "guidance", "quarterly results",
      " q1", " q2", " q3", " q4", "record sales", "preliminary results")),
    ("🚀 منتج/إطلاق",
     ("launch", "unveil", "introduce", "new product", "rollout", "availab")),
    ("🔬 براءة اختراع",
     ("patent",)),
    ("📊 تغطية محلّل",
     ("price target", "upgrade", "downgrade", "initiates coverage", "rating",
      "reiterates")),
]


def classify_news(title: str, description: str = "") -> str:
    """يرجّع تسمية عربية لنوع الخبر من العنوان/الوصف، أو «📰 خبر» افتراضيًا."""
    text = f"{title or ''} {description or ''}".lower()
    for label, keywords in _NEWS_CATEGORIES:
        if any(kw in text for kw in keywords):
            return label
    return "📰 خبر"


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
    # صنّف نوع الخبر للعرض في البطاقة
    catalyst.category = classify_news(catalyst.headline, catalyst.description)
    return catalyst


def catalyst_bonus(cfg: Config, catalyst: Catalyst | None) -> float:
    """مقدار التقوية المضافة للدرجة عند وجود محفّز ضمن النافذة.

    خبر الطرح/التخفيف (سلبي) **لا يُكافأ** — يضرّ السهم لا يدعمه؛ نتركه للخصم
    عبر المحلّل/رادار SEC. نصنّفه هنا إن لم يكن مُصنّفًا (للاستدعاء المباشر).
    """
    if catalyst is None or not catalyst.has_news:
        return 0.0
    category = catalyst.category or classify_news(
        catalyst.headline, catalyst.description)
    if category == NEGATIVE_NEWS:
        return 0.0
    return cfg.catalyst_score_bonus
