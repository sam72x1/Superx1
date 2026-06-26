"""تشريح الفشل — يشرح **لماذا** فشل (أو نجح) سهم بعد التنبيه.

الفكرة المحورية (مطلب المستخدم): أداة التطوير تجمع البيانات الخام لكل سهم
(الجاهزية/الزخم/المحفّز/التخفيف/الشورت + كيف تحرّك السعر ووين انهار)، و**المستشار
الذكي يفسّرها**: «$XYZ فشل لأن... — الدرس...». أداة التطوير «خام»، والمستشار «عقل».

يعمل بثلاث واجهات:
  • لحظي: عند كسر الوقف يُرسَل تشريح فوري (من main).
  • بريفنغ: تشريح كل الفاشلين مرّة آخر اليوم (عبر بيانات advisor).
  • عند الطلب: /why TICKER في المساعد.

best-effort: مع مفتاح Claude يطلع تفسير ذكي؛ بدونه **تفسير قاعدي** من البيانات
نفسها (يشتغل دائمًا). لا يخترع بيانات غير مُسجّلة.
"""

from __future__ import annotations

import logging

from .config import Config
from .llm import ClaudeClient
from .textutil import esc

logger = logging.getLogger(__name__)

_SYSTEM = (
    "أنت محلّل «تشريح ما بعد الصفقة» لبوت أسهم زخم. تُعطى بيانات سهم مُسجّلة "
    "بعد تنبيه (جاهزية فنية، زخم لحظي، محفّز خبري، خطر تخفيف SEC، شورت، وكيف "
    "تحرّك السعر ونتيجته). فسّر **بإيجاز ودقّة بالعربي** لماذا انتهى بهذه النتيجة، "
    "معتمدًا على البيانات المعطاة فقط (لا تخترع أرقامًا). ثم درس عملي قابل للتطبيق "
    "على التنبيهات القادمة. أنت تُبلغ وتعلّم فقط — لا توصية ولا تنفيذ."
)

_TOOL = {
    "name": "submit_postmortem",
    "description": "يقدّم سبب النتيجة والدرس المستفاد.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cause": {"type": "string",
                      "description": "سبب النتيجة الأرجح بإيجاز (جملة-جملتان)"},
            "lesson": {"type": "string",
                       "description": "درس عملي موجز للتنبيهات القادمة"},
        },
        "required": ["cause", "lesson"],
    },
}


def _get(row, key, default=None):
    """قراءة آمنة من sqlite3.Row أو dict (عمود قد يكون None/غائبًا)."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return v if v is not None else default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _facts(row) -> str:
    """سطور حقائق مقروءة من صفّ التتبّع (للذكاء وللقاعدة)."""
    L = []
    res = _get(row, "result", "") or "مفتوح"
    L.append(f"الرمز: {_get(row, 'ticker', '?')} · الجلسة: {_get(row, 'session', '?')}")
    L.append(f"النتيجة: {res} · أقصى ربح +{_num(_get(row, 'max_gain_pct', 0)) or 0:.0f}%"
             f" · أقصى تراجع {_num(_get(row, 'max_draw_pct', 0)) or 0:.0f}%")
    entry = _num(_get(row, "first_price"))
    stop = _num(_get(row, "stop_price"))
    if entry:
        L.append(f"الدخول: {entry:.2f} · الوقف: {stop:.2f}" if stop
                 else f"الدخول: {entry:.2f}")
    L.append(f"درجة: {_num(_get(row, 'score', 0)) or 0:.0f}/100 · "
             f"جاهزية فنية: {_num(_get(row, 'readiness', 0)) or 0:.0f}/100 · "
             f"زخم: {_num(_get(row, 'momentum', 0)) or 0:.0f}")
    rvol = _num(_get(row, "rvol"))
    rvol5 = _num(_get(row, "rvol_5min"))
    if rvol is not None or rvol5 is not None:
        L.append(f"RVol: {rvol or 0:.1f}x · 5min RVol: {rvol5 or 0:.1f}x")
    L.append("محفّز خبري: " + ("نعم — " + (_get(row, "catalyst_head", "") or "")
                              if _get(row, "had_news") else "لا يوجد"))
    dr = _get(row, "dilution_risk")
    if dr and dr != "لا":
        L.append(f"خطر تخفيف (SEC): {dr}")
    sp = _num(_get(row, "short_pct"))
    if sp is not None:
        L.append(f"الشورت من الفلوت: {sp:.0f}%")
    ad = _get(row, "analyst_dir")
    if ad:
        L.append(f"حكم المحلّل المسبق على المحفّز: {ad}")
    return "\n".join(L)


def _rule_reason(row) -> tuple[str, str]:
    """تفسير قاعدي (بلا ذكاء) من البيانات — يشتغل دائمًا."""
    res = _get(row, "result", "")
    reasons: list[str] = []
    dr = _get(row, "dilution_risk")
    sp = _num(_get(row, "short_pct"))
    had_news = _get(row, "had_news")
    momentum = _num(_get(row, "momentum")) or 0
    readiness = _num(_get(row, "readiness")) or 0
    max_gain = _num(_get(row, "max_gain_pct")) or 0

    if dr and dr != "لا":
        reasons.append(f"خطر تخفيف {dr} (طرح/إصدار أسهم يضغط السعر)")
    if sp is not None and sp >= 20:
        reasons.append(f"شورت مرتفع {sp:.0f}% (ضغط بيعي)")
    if not had_news:
        reasons.append("بلا محفّز خبري يدعم الاستمرار")
    if momentum and momentum < 30:
        reasons.append("زخم لحظي ضعيف نسبيًا")

    if res == "loss":
        if max_gain < 3:
            reasons.append("انعكس بسرعة بعد الدخول دون أن يعطي مجالًا")
        head = "كسر الوقف"
        lesson = ("راقِب التخفيف/الشورت وقوّة الزخم قبل الدخول؛ "
                  "السهم الجاهز فنيًا لا يكفي وحده.")
    elif res == "timeout":
        reasons.append("لم يصل الهدف ولا كسر الوقف خلال النافذة (زخم خفت)")
        head = "انتهت النافذة بلا حسم"
        lesson = "قد تكون الأهداف بعيدة أو نافذة المتابعة قصيرة لهذا النمط."
    elif res == "win":
        head = "بلغ هدفه"
        good = []
        if had_news:
            good.append("محفّز خبري داعم")
        if readiness >= 80:
            good.append("جاهزية فنية عالية")
        if momentum >= 35:
            good.append("زخم لحظي قوي")
        return ("اكتمل بنجاح: " + ("، ".join(good) if good else "زخم + جاهزية متوافقان"),
                "كرّر اشتراطات هذا النمط الناجح.")
    else:
        head = "ما زال مفتوحًا"
        lesson = "النتيجة لم تُحسم بعد."

    cause = f"{head} — " + ("؛ ".join(reasons) if reasons else "أسباب غير واضحة من البيانات")
    return cause, lesson


def explain(cfg: Config, row, client: ClaudeClient | None = None
            ) -> tuple[str, str]:
    """يرجّع (السبب، الدرس). ذكاء Claude إن توفّر، وإلا تفسير قاعدي."""
    client = client or ClaudeClient(cfg.anthropic_api_key)
    if getattr(cfg, "postmortem_enabled", True) and client.available:
        try:
            out = client.structured(
                cfg.analyst_model, _SYSTEM,
                f"بيانات السهم:\n{_facts(row)}\n\nشرّح النتيجة وأعطِ الدرس.",
                _TOOL, max_tokens=350)
            if out and out.get("cause"):
                return out["cause"], out.get("lesson", "")
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("تشريح Claude فشل: %s", exc)
    return _rule_reason(row)


def build_failure_message(cfg: Config, row, client: ClaudeClient | None = None
                          ) -> str:
    """رسالة تيليجرام لحظية عند فشل سهم (HTML)."""
    cause, lesson = explain(cfg, row, client)
    tkr = _get(row, "ticker", "?")
    res = _get(row, "result", "")
    head = "كسر الوقف ⛔" if res == "loss" else "انتهت النافذة بلا حسم ⏳"
    out = [f"🔍 <b>تشريح ${esc(tkr)}</b> — {head}",
           f"السبب: {esc(cause)}"]
    if lesson:
        out.append(f"الدرس: {esc(lesson)}")
    out.append("<i>— تشريح آلي للتعلّم، ليس توصية.</i>")
    return "\n".join(out)


def build_why_message(cfg: Config, row, client: ClaudeClient | None = None
                      ) -> str:
    """رد /why TICKER — يشرح نتيجة سهم أيًّا كانت (نجاح/فشل/مفتوح)."""
    cause, lesson = explain(cfg, row, client)
    tkr = _get(row, "ticker", "?")
    res = _get(row, "result", "") or "مفتوح"
    label = {"win": "نجاح ✅", "loss": "خسارة 🛑",
             "timeout": "بلا حسم ⏳"}.get(res, "مفتوح ⏳")
    return (f"🔍 <b>${esc(tkr)}</b> — النتيجة: {label}\n"
            f"السبب: {esc(cause)}\n" + (f"الدرس: {esc(lesson)}\n" if lesson else "")
            + "<i>— تشريح آلي، ليس توصية.</i>")
