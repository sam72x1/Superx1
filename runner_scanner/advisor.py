"""المستشار الذكي — بريفنغ نهاية الجلسة («العين اللي ما تنام»).

يجمع بيانات اليوم (تنبيهات/نتائج/فرص فائتة/صحة البوت/حالة ريندر) ويطلب
من Claude كتابة بريفنغ عربي طبيعي كأنه مستشار أمين سُلِّم المشروع: ماذا
حدث، أبرز الملاحظات، توصيات (**للمراجعة فقط — لا تنفيذ تلقائي**)، وتنبيهات.

بدون مفتاح Claude → بريفنغ مُجدوَل مبسّط (نصّي) كي يصل شيء دائمًا.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import Config
from .llm import ClaudeClient
from .state import trade_date_str
from .textutil import esc

logger = logging.getLogger(__name__)

_SYSTEM = (
    "أنت مستشار تداول شخصي للمستخدم، أمين ومحافظ، سُلّمت إدارة متابعة بوت "
    "أسهم نيابةً عنه. اكتب «بريفنغ نهاية الجلسة» بالعربي بنبرة مستشار "
    "موثوق: لخّص ماذا حدث اليوم، أبرز الملاحظات والدروس، توصيات عملية "
    "**للمراجعة فقط** (أنت لا تنفّذ شيئًا بنفسك أبدًا، فقط تنبّه وتقترح)، "
    "وأي مخاطر/تنبيهات. كن موجزًا ومباشرًا ومفيدًا، بلا حشو. استخدم نقاطًا. "
    "**مهم:** إن وُجد فاشلون، أضِف قسمًا «🔍 تشريح الفشل» تشرح فيه لكل سهم فاشل "
    "**لماذا فشل** (تخفيف؟ شورت؟ بلا محفّز؟ زخم ضعيف؟ انعكاس سريع؟) ودرسًا منه، "
    "معتمدًا على البيانات المعطاة فقط دون اختراع أرقام."
)


def _summarize_day(cfg: Config, store, now: datetime) -> dict:
    day = trade_date_str(now)
    rows = store.fetch_day(day)
    alerts = [r for r in rows if r["is_alert"]]
    wins = [r for r in alerts if r["result"] == "win"]
    losses = [r for r in alerts if r["result"] == "loss"]
    timeouts = [r for r in alerts if r["result"] == "timeout"]
    missed = [r for r in store.fetch_missed(cfg.missed_rise_pct)
              if r["trade_date"] == day]
    return {
        "day": day, "processed": len(rows), "alerts": alerts,
        "wins": wins, "losses": losses, "timeouts": timeouts, "missed": missed,
    }


def _data_text(summary: dict, health_faults, render_summary: str) -> str:
    s = summary
    lines = [
        f"التاريخ: {s['day']}",
        f"مرشّحون عُولجوا: {s['processed']} · تنبيهات: {len(s['alerts'])}",
        f"نتائج التنبيهات: {len(s['wins'])} نجاح · {len(s['losses'])} خسارة · "
        f"{len(s['timeouts'])} بلا حسم",
    ]
    if s["alerts"]:
        lines.append("التنبيهات:")
        for r in s["alerts"][:12]:
            gain = r["max_gain_pct"] or 0
            lines.append(f"  • {r['ticker']}: درجة {r['score']:.0f} · نتيجة "
                         f"{r['result'] or 'مفتوح'} · أقصى ربح +{gain:.0f}%")
    # الفاشلون مع حقائقهم الكاملة (لتشريح «لماذا فشل») — قلب البريفنغ
    failures = (s["losses"] or []) + (s["timeouts"] or [])
    if failures:
        from . import postmortem
        lines.append("الفاشلون (حقائق للتشريح):")
        for r in failures[:8]:
            lines.append("  ▪ " + postmortem._facts(r).replace("\n", " · "))
    if s["missed"]:
        lines.append("فرص فائتة (مرفوض صعد):")
        for r in s["missed"][:8]:
            lines.append(f"  • {r['ticker']}: +{r['max_gain_pct']:.0f}% "
                         f"(رُفض: {r['reject_reason']})")
    faults = list(health_faults or [])
    lines.append(f"صحة البوت: {'أعطال: ' + ', '.join(faults) if faults else 'سليم ✅'}")
    lines.append(render_summary)
    return "\n".join(lines)


def build_briefing(cfg: Config, store, render_summary: str = "",
                   health_faults=None, now: datetime | None = None,
                   client: ClaudeClient | None = None) -> str:
    """يبني بريفنغ نهاية الجلسة (نص). AI إن توفّر المفتاح، وإلا مبسّط."""
    now = now or datetime.now(timezone.utc)
    summary = _summarize_day(cfg, store, now)
    data = _data_text(summary, health_faults, render_summary)

    client = client or ClaudeClient(cfg.anthropic_api_key)
    if cfg.advisor_enabled and client.available:
        prompt = (f"بيانات جلسة اليوم:\n{data}\n\n"
                  "اكتب بريفنغ نهاية الجلسة كمستشار.")
        text = client.chat(cfg.anthropic_model, _SYSTEM, prompt, max_tokens=900)
        if text:
            # نص Claude حرّ → يُهرَّب قبل لفّه بوسوم HTML الثابتة
            return (f"🌙 <b>بريفنغ نهاية الجلسة — {summary['day']}</b>\n\n"
                    f"{esc(text)}\n\n<i>— مستشارك الآلي (توصيات للمراجعة فقط).</i>")

    # fallback مبسّط (بلا Claude)
    s = summary
    return (
        f"🌙 <b>بريفنغ نهاية الجلسة — {s['day']}</b>\n"
        f"تنبيهات: {len(s['alerts'])} ({len(s['wins'])}✅/{len(s['losses'])}🛑/"
        f"{len(s['timeouts'])}⏳) · فرص فائتة: {len(s['missed'])}\n"
        f"{render_summary}\n"
        "<i>(فعّل ANTHROPIC_API_KEY لبريفنغ ذكي مفصّل.)</i>"
    )
