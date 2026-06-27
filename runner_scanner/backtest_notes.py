"""ملاحظات الباكتيست — طبقة تحليل تُرسَل مع التقرير: «وش يعني هذا ووش أسوي؟».

التقرير الخام (أرقام + قمع ترشيح) مهمّ لكنه لا يفسّر. هذي الطبقة تقرأ النتائج
+ القمع + المعايرة وتكتب ملاحظات عملية: هل العيّنة كافية؟ أين أكبر تسرّب وهل
هو قيد منهجي في الباكتيست أم صرامة استراتيجية؟ توصيات **للمراجعة فقط**.

best-effort: مع مفتاح Claude → تحليل أعمق. بلا مفتاح → ملاحظات قاعدية حتمية
(تصل دائمًا) — هوية البوت: يلاحظ ويقترح، لا ينفّذ.
"""

from __future__ import annotations

import logging

from .config import Config
from .llm import ClaudeClient
from .textutil import esc

logger = logging.getLogger(__name__)

_SYSTEM = (
    "أنت محلّل باكتيست خبير ومحافظ لبوت أسهم. تُعطى ملخّص نتائج باكتيست تاريخي "
    "وقمع ترشيح (أين مات المرشّحون). اكتب «ملاحظات الباكتيست» بالعربي بإيجاز "
    "ونقاط: (1) هل العيّنة كافية للثقة بالنِّسَب؟ (2) أين أكبر تسرّب في القمع، "
    "وهل هو قيد منهجي في الباكتيست (مثل دخول مبكّر عند أول عبور 5د يخفض RVol "
    "لأن الحجم التراكمي ضئيل) أم صرامة استراتيجية حقيقية؟ (3) توصيات عملية "
    "**للمراجعة فقط** (أنت لا تنفّذ شيئًا بنفسك). اعتمد على البيانات المعطاة "
    "فقط دون اختراع أرقام."
)


def _facts_text(res, grid) -> str:
    """حقائق مضغوطة تُغذّى لـ Claude (أو تُلخَّص قاعديًا)."""
    s = res.stats()
    f = res.funnel or {}
    wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"
    lines = [
        f"المدى: {res.start} → {res.end} ({res.days} يوم تداول)",
        f"تنبيهات: {s['alerts']} · نجاح {wr} "
        f"({s['wins']}✅/{s['losses']}🛑/{s['timeouts']}⏳) · "
        f"متوسط أقصى ربح {s['avg_gain']:+.0f}%",
    ]
    if f.get("considered"):
        lines.append(
            f"قمع: اعتُبر {f['considered']} · نقص شموع5د {f.get('no_5min', 0)} "
            f"· ما ثبّت إغلاق {f.get('no_trigger', 0)} · "
            f"رُفض {f.get('rejected', 0)} · نجا {f.get('alerts', 0)}")
        rr = f.get("reject_reasons") or {}
        if rr:
            lines.append("أسباب الرفض: " + " · ".join(
                f"{k}:{v}" for k, v in sorted(rr.items(), key=lambda x: -x[1])))
    if grid and grid.get("best"):
        b = grid["best"]
        lines.append(
            f"أفضل تركيبة (معايرة): {b['env']}={int(b['value'])} → "
            f"{b['win_rate']:.0f}% مقابل {b['baseline_win_rate']:.0f}% الأساس")
    return "\n".join(lines)


def _rule_notes(cfg: Config, res, grid) -> str:
    """ملاحظات قاعدية حتمية (بلا Claude) — تفسّر القمع وتقترح."""
    s = res.stats()
    f = res.funnel or {}
    decisive = s["wins"] + s["losses"]
    out = []

    # ── حجم العيّنة ──
    if decisive < cfg.backtest_grid_min_decisive:
        out.append(f"• العيّنة صغيرة ({decisive} محسومة) — النِّسَب مؤشّر أوّلي "
                   "لا قرار. لا تغيّر عتبات بعد.")
    else:
        out.append(f"• العيّنة معقولة ({decisive} محسومة) — يمكن البدء بالوثوق "
                   "بالنِّسَب بحذر.")

    # ── أكبر تسرّب في القمع ──
    if f.get("considered"):
        leaks = [("نقص شموع 5د تاريخية", f.get("no_5min", 0)),
                 ("ما ثبّتوا إغلاق 5د عند الحدّ", f.get("no_trigger", 0)),
                 ("رُفضوا بالبوّابات", f.get("rejected", 0))]
        leaks.sort(key=lambda x: -x[1])
        top, cnt = leaks[0]
        if cnt:
            out.append(f"• أكبر تسرّب: {top} ({cnt} من {f['considered']} اعتُبروا).")
        rr = f.get("reject_reasons") or {}
        if rr:
            gate, gc = max(rr.items(), key=lambda x: x[1])
            out.append(f"• أكثر بوّابة ترفض: {gate} ({gc}).")
            if gate == "RVol":
                out.append("  ↳ غالبًا قيد منهجي: الباكتيست يدخل عند أول عبور "
                           "5د فالحجم التراكمي ضئيل → RVol منخفض (لا يعكس الحي). "
                           "الإصلاح في محرّك الباكتيست لا في عتبتك.")

    # ── المعايرة ──
    if grid and grid.get("best"):
        b = grid["best"]
        out.append(f"• المعايرة تقترح (للمراجعة): {b['env']}={int(b['value'])} "
                   f"→ {b['win_rate']:.0f}% مقابل {b['baseline_win_rate']:.0f}%.")
    elif grid:
        out.append("• المعايرة: ما فيه تركيبة تتفوّق على الأساس بعيّنة كافية — "
                   "الأساس جيّد أو العيّنة صغيرة.")

    return "\n".join(out)


def build_notes(cfg: Config, res, grid=None,
                client: ClaudeClient | None = None) -> str:
    """يبني رسالة «ملاحظات الباكتيست». AI إن توفّر المفتاح، وإلا قاعدية."""
    client = client or ClaudeClient(cfg.anthropic_api_key)
    if cfg.backtest_notes_enabled and client.available:
        prompt = (f"نتائج الباكتيست:\n{_facts_text(res, grid)}\n\n"
                  "اكتب ملاحظات الباكتيست كمحلّل.")
        text = client.chat(cfg.anthropic_model, _SYSTEM, prompt, max_tokens=1200)
        if text:
            # نص Claude حرّ → يُهرَّب قبل لفّه بوسوم HTML الثابتة
            return (f"🧠 <b>ملاحظات الباكتيست</b>\n\n{esc(text)}\n\n"
                    "<i>— تحليل آلي (توصيات للمراجعة فقط).</i>")

    # fallback قاعدي (يصل دائمًا)
    return (f"🧠 <b>ملاحظات الباكتيست</b>\n{_rule_notes(cfg, res, grid)}\n"
            "<i>(فعّل ANTHROPIC_API_KEY لتحليل أعمق.)</i>")
