"""المعايرة التلقائية — يقترح أرقامًا محدّدة للعتبات من نتائج البوت.

الفلسفة (مبدأ المستخدم): **يقترح ويعلّم، لا يغيّر شيئًا بنفسه**. كل اقتراح
يحمل: المتغيّر، القيمة الحالية، القيمة المقترحة، السبب من البيانات، ودرجة
الثقة (حسب حجم العيّنة). المستخدم يقرّر ويعدّل env في Render يدويًا.

يختلف عن «اقتراحات» dev_assistant العامة بأنه **كمّي ومحدّد**: يحسب نسبة
نجاح كل شريحة بحدّ أدنى للعيّنة، ويقترح رقمًا جديدًا قابلًا للّصق مباشرة.

النتيجة (result): win=بلغ هدفًا · loss=ضرب الوقف · timeout=نافذة بلا حسم.
الحسم = win+loss فقط (timeout لا يدخل نسبة النجاح).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config

# حدود حجم العيّنة لمستوى الثقة في اقتراح.
_MIN_CONFIDENT = 8     # عيّنة كافية لثقة «عالية»
_MIN_MEDIUM = 4        # أقلّ من هذا لا نقترح أصلًا
_MIN_MISSED = 3        # فرص فائتة بنفس البوّابة قبل اقتراح تخفيفها


@dataclass
class CalibrationProposal:
    """اقتراح معايرة واحد (للمراجعة البشرية فقط — لا يُطبَّق تلقائيًا)."""

    env: str                 # اسم متغيّر البيئة
    current: float           # القيمة الحالية
    proposed: float          # القيمة المقترحة
    reason: str              # السبب من البيانات
    confidence: str          # عالية/متوسطة

    def line(self) -> str:
        cur = f"{self.current:g}"
        new = f"{self.proposed:g}"
        return (f"   • <b>{self.env}</b>: {cur} → <b>{new}</b> "
                f"({self.confidence})\n     ↳ {self.reason}")


def _win_rate(rows: list) -> tuple[float | None, int]:
    """(نسبة نجاح%، عدد المحسومين) لمجموعة. None لو لا حسم."""
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    decisive = wins + losses
    if decisive == 0:
        return None, 0
    return wins / decisive * 100.0, decisive


def _conf(n: int) -> str | None:
    if n >= _MIN_CONFIDENT:
        return "عالية"
    if n >= _MIN_MEDIUM:
        return "متوسطة"
    return None


def propose_calibrations(store, cfg: Config) -> list[CalibrationProposal]:
    """يحلّل النتائج المتراكمة ويرجّع اقتراحات معايرة كمّية (قد تكون فارغة)."""
    alerts = list(store.fetch_resolved(only_alerts=True))
    missed = list(store.fetch_missed(cfg.missed_rise_pct))
    props: list[CalibrationProposal] = []

    base_wr, base_n = _win_rate(alerts)
    if base_wr is None or base_n < _MIN_MEDIUM:
        return props   # لا بيانات كافية لأي اقتراح ذي معنى

    # ── 1) RVOL_MIN: الشريحة الدنيا ضعيفة → ارفع العتبة ───────────
    low_rv = [r for r in alerts
              if r["rvol"] is not None and r["rvol"] < cfg.rvol_min + 3]
    low_wr, low_n = _win_rate(low_rv)
    conf = _conf(low_n)
    if low_wr is not None and conf and low_wr <= base_wr - 20:
        props.append(CalibrationProposal(
            env="RVOL_MIN", current=cfg.rvol_min,
            proposed=round(cfg.rvol_min + 2),
            reason=(f"شريحة RVol المنخفضة (<{cfg.rvol_min + 3:g}x) نجاحها "
                    f"{low_wr:.0f}% مقابل {base_wr:.0f}% للكل ({low_n} محسوم)."),
            confidence=conf))
    else:
        # وإلا: فرص فائتة كثيرة بسبب بوّابة RVol → خفّضها قليلًا
        rv_missed = [m for m in missed if "RVol" in (m["reject_reason"] or "")]
        if len(rv_missed) >= _MIN_MISSED:
            props.append(CalibrationProposal(
                env="RVOL_MIN", current=cfg.rvol_min,
                proposed=max(1.0, round(cfg.rvol_min - 1)),
                reason=(f"{len(rv_missed)} سهم صاعد فاتنا ببوّابة RVol "
                        "دون ضعف واضح في الشريحة المنخفضة."),
                confidence="متوسطة"))

    # ── 2) TECH_READINESS_MIN: الشريحة فوق العتبة مباشرة (10 نقاط) تخسر →
    # ارفع العتبة 10 نقاط (مثال: 60 والشريحة 60-70 ضعيفة → رجّعها إلى 70).
    if cfg.tech_readiness_min < 90:
        hi = cfg.tech_readiness_min + 10
        band = [r for r in alerts if r["readiness"] is not None
                and cfg.tech_readiness_min <= r["readiness"] < hi]
        b_wr, b_n = _win_rate(band)
        conf = _conf(b_n)
        if b_wr is not None and conf and b_wr <= base_wr - 20:
            props.append(CalibrationProposal(
                env="TECH_READINESS_MIN", current=cfg.tech_readiness_min,
                proposed=round(hi),
                reason=(f"الجاهزية {cfg.tech_readiness_min:g}-{hi:g} نجاحها "
                        f"{b_wr:.0f}% فقط مقابل {base_wr:.0f}% للكل ({b_n} محسوم) "
                        f"— رفع العتبة إلى {hi:g} يصفّي الشريحة الضعيفة."),
                confidence=conf))

    # ── 3) ALERT_SCORE_MIN: الشريحة فوق العتبة مباشرة تخسر → ارفع ─
    lo, hi = cfg.alert_score_min, cfg.alert_score_min + 10
    near = [r for r in alerts if r["score"] is not None and lo <= r["score"] < hi]
    n_wr, n_n = _win_rate(near)
    conf = _conf(n_n)
    if n_wr is not None and conf and n_wr <= base_wr - 20:
        props.append(CalibrationProposal(
            env="ALERT_SCORE_MIN", current=cfg.alert_score_min,
            proposed=round(cfg.alert_score_min + 5),
            reason=(f"درجات {lo:g}-{hi:g} (فوق العتبة مباشرة) نجاحها "
                    f"{n_wr:.0f}% ({n_n} محسوم) — رفعها يرفع الجودة."),
            confidence=conf))

    # ── 4) FLOAT_MAX: فرص فائتة ببوّابة الفلوت → ارفع السقف ───────
    fl_missed = [m for m in missed if "فلوت" in (m["reject_reason"] or "")]
    if len(fl_missed) >= _MIN_MISSED:
        props.append(CalibrationProposal(
            env="FLOAT_MAX", current=cfg.float_max,
            proposed=round(cfg.float_max * 1.5),
            reason=(f"{len(fl_missed)} سهم صاعد فاتنا ببوّابة الفلوت — "
                    "توسيع السقف 1.5× يلتقط متوسطات الفلوت الصاعدة."),
            confidence="متوسطة"))

    # ── 5) OUTCOME_WINDOW_MIN: غلبة انتهاء النوافذ → وسّع النافذة ─
    timeouts = sum(1 for r in alerts if r["result"] == "timeout")
    total = len(alerts)
    if total >= _MIN_CONFIDENT and timeouts >= total * 0.5:
        props.append(CalibrationProposal(
            env="OUTCOME_WINDOW_MIN", current=cfg.outcome_window_min,
            proposed=round(cfg.outcome_window_min * 1.5),
            reason=(f"{timeouts}/{total} تنبيه انتهت نافذته بلا حسم — "
                    "النافذة قد تكون أقصر من زمن وصول الأهداف."),
            confidence="عالية" if total >= 12 else "متوسطة"))

    # ── 6) CATALYST_SCORE_BONUS: «بمحفّز» يتفوّق بوضوح → ارفع وزنه ─
    with_news = [r for r in alerts if r["had_news"]]
    no_news = [r for r in alerts if not r["had_news"]]
    wn_wr, wn_n = _win_rate(with_news)
    nn_wr, nn_n = _win_rate(no_news)
    if (wn_wr is not None and nn_wr is not None
            and wn_n >= _MIN_MEDIUM and nn_n >= _MIN_MEDIUM
            and wn_wr - nn_wr >= 20):
        props.append(CalibrationProposal(
            env="CATALYST_SCORE_BONUS", current=cfg.catalyst_score_bonus,
            proposed=round(cfg.catalyst_score_bonus + 4),
            reason=(f"«بمحفّز» ينجح {wn_wr:.0f}% مقابل {nn_wr:.0f}% «بلا محفّز» "
                    f"— رفع وزن الخبر يقدّم الأقوى."),
            confidence=_conf(min(wn_n, nn_n)) or "متوسطة"))

    return props


def format_proposals(props: list[CalibrationProposal]) -> str:
    """يبني قسم المعايرة (HTML تيليجرام). فارغ لو لا اقتراحات."""
    if not props:
        return ""
    lines = ["\n⚙️ <b>معايرة مقترحة (أرقام جاهزة — للمراجعة، لا تُطبَّق تلقائيًا)</b>"]
    lines += [p.line() for p in props]
    lines.append("   <i>↳ عدّلها يدويًا في Render → Environment إن اقتنعت.</i>")
    return "\n".join(lines)
