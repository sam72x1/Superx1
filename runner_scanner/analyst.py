"""المحلّل الذكي (Claude) — يقرأ خبر الرَنر وسياقه ويعطي حكمًا.

أضعف حلقة في البوت كانت تصنيف الخبر بالكلمات المفتاحية: «شراكة» قد تكون
تحويلية أو تافهة، و«طرح» يُصنَّف لكن البوت ما يفهم إنه **يقتل الرَنر**.
هذي الطبقة تجعل Claude يقرأ الخبر فعليًا ويقيّم:
  نوع المحفّز · اتجاهه (صعودي/هبوطي/محايد) · جوهريته 1–10 · سطر أطروحة ·
  تحذير الأخبار الهبوطية (طرح/تخفيف/إفلاس/تحذير سيولة).

best-effort: بدون مفتاح Anthropic يرجّع None والبوت يكمل عادي.
"""

from __future__ import annotations

import logging

from .config import Config
from .llm import ClaudeClient
from .models import AnalystResult, Candidate

logger = logging.getLogger(__name__)

_SYSTEM = (
    "أنت محلّل أسهم رَنرات (momentum runners) خبير ومحافظ. تقيّم سهمًا قفز "
    "+20%+ اليوم: هل المحفّز الخبري حقيقي وصعودي ويستحق المطاردة، أم أنه "
    "خبر هبوطي/تافه يجب الحذر منه؟ ركّز خصوصًا على الأخبار التي **تقتل** "
    "الرَنرات: الطرح المخفِّف (offering/dilution/ATM/shelf)، تحذير الاستمرارية "
    "(going concern)، نقص السيولة، أو ضخّ مضلِّل. كن صريحًا ومختصرًا بالعربي."
)

_TOOL = {
    "name": "submit_analysis",
    "description": "قدّم تحليلك المنظّم للرَنر.",
    "input_schema": {
        "type": "object",
        "properties": {
            "catalyst_type": {
                "type": "string",
                "description": "نوع المحفّز بالعربي (أرباح/شراكة/FDA/عقد/طرح...)",
            },
            "direction": {
                "type": "string",
                "enum": ["صعودي", "هبوطي", "محايد"],
                "description": "اتجاه أثر الخبر على السهم",
            },
            "materiality": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "جوهرية المحفّز (1 تافه .. 10 تحويلي)",
            },
            "thesis": {
                "type": "string",
                "description": "سطر أطروحة عربي موجز يدمج الزخم والجاهزية والخبر",
            },
            "warning": {
                "type": "string",
                "description": "تحذير صريح إن كان الخبر هبوطيًا/خطِرًا، وإلا فارغ",
            },
        },
        "required": ["direction", "materiality", "thesis"],
    },
}


def _build_prompt(c: Candidate) -> str:
    s, m, rk = c.snapshot, c.momentum, c.readiness
    cat = c.catalyst
    lines = [
        f"الرمز: {c.ticker}",
        f"الارتفاع اليوم: +{s.change_pct:.1f}% · السعر ${s.last_price:.2f}",
        f"الماركت كاب: {c.market_cap or 'غير معروف'} · الفلوت: {c.float_shares or 'غير معروف'}",
    ]
    if m:
        lines.append(f"الزخم: RVol {m.rvol:.1f}x · 5min RVol {m.rvol_5min:.1f}x "
                     f"· فوق VWAP: {m.above_vwap}")
    if rk:
        lines.append(f"الجاهزية الفنية: {rk.classic_score:.0f}/100 · اتجاه "
                     f"{rk.trend} · شمعة يومية: {rk.candle or '—'}")
    if cat and cat.has_news:
        lines.append(f"\nالخبر — العنوان: {cat.headline}")
        if cat.description:
            lines.append(f"الوصف: {cat.description[:600]}")
        lines.append(f"المصدر: {cat.publisher}")
    else:
        lines.append("\nلا يوجد خبر حديث (محفّز غير مؤكّد).")
    lines.append("\nحلّل وقدّم النتيجة عبر الأداة.")
    return "\n".join(lines)


class ClaudeAnalyst:
    def __init__(self, cfg: Config, client: ClaudeClient | None = None):
        self.cfg = cfg
        self.client = client or ClaudeClient(cfg.anthropic_api_key)

    def analyze(self, c: Candidate) -> AnalystResult | None:
        if not self.cfg.analyst_enabled or not self.client.available:
            return None
        data = self.client.structured(
            self.cfg.analyst_model, _SYSTEM, _build_prompt(c), _TOOL)
        if not data:
            return None
        try:
            return AnalystResult(
                catalyst_type=str(data.get("catalyst_type", "")),
                direction=str(data.get("direction", "")),
                materiality=int(data.get("materiality") or 0),
                thesis=str(data.get("thesis", "")),
                warning=str(data.get("warning", "")),
            )
        except (TypeError, ValueError):
            return None
