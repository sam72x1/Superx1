"""أداة التطوير (Dev Assistant) — تتعلّم من نتائج البوت المتراكمة.

مكيّفة لماسح الرَنرات (لا تنسخ مشروع الارتكاز): تحلّل تتبّعات النتائج
(win/loss/timeout) من جدول tracking وتُنتج تقرير أداء بالشرائح المناسبة
لرَنرات الزخم:

  • نسبة النجاح الكلية + بالشرائح (جلسة · فلوت · RVol · 5min RVol ·
    الدرجة · الجاهزية · الخبر)
  • 🛑 أنماط الخاسرين (أي شريحة تخسر أكثر)
  • 👻 الفرص الفائتة (مرفوض صعد ≥ نسبة) مجمّعة حسب سبب الرفض = أي بوّابة
    قد تكون متشدّدة
  • 💡 اقتراحات ضبط للبوابات (اقتراح فقط — لا يغيّر إعدادات)

«نجاح» = الرَنر بلغ الهدف الأول بعد التنبيه. «خسارة» = ضرب الوقف.
«انتهاء نافذة» = ما بلغ أيًّا منهما خلال نافذة المتابعة.

النصوص HTML-آمنة لتيليجرام. اقتراح فقط — لا يلمس الإعدادات (المستخدم يقرّر).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .config import Config


def esc(s) -> str:
    """تعقيم النصوص الخارجية حتى لا تكسر HTML تيليجرام."""
    return (str(s).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _human(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


# ── إحصاء مجموعة نتائج ────────────────────────────────────────────
def _stats(rows: list) -> dict:
    """يرجّع إحصاء مجموعة: العدد، فائز/خاسر/منتهٍ، نسبة النجاح، متوسط أقصى ربح."""
    n = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    timeouts = sum(1 for r in rows if r["outcome"] == "timeout")
    decisive = wins + losses
    win_rate = (wins / decisive * 100.0) if decisive else None
    avg_gain = (sum((r["max_gain_pct"] or 0) for r in rows) / n) if n else 0.0
    return {"n": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "win_rate": win_rate, "avg_gain": avg_gain}


def _bucket(v, edges):
    if v is None:
        return None
    for lo, hi, lbl in edges:
        if lo <= v < hi:
            return lbl
    return None


# ── بناء التقرير ──────────────────────────────────────────────────
def build_dev_report(store, cfg: Config, now: datetime | None = None) -> str:
    """يبني تقرير أداء HTML من جدول tracking. store = state.Store."""
    now = now or datetime.now(timezone.utc)
    alerts = [r for r in store.fetch_resolved(only_alerts=True)]
    missed = list(store.fetch_missed(cfg.missed_rise_pct))

    head = ["🔬 <b>مساعد التطوير — أداء ماسح الرَنرات</b>",
            f"تنبيهات محسومة متراكمة: <b>{len(alerts)}</b>"]

    SEG_MIN_N = 3

    def seg(title, keyfn, sort_by_winrate=True):
        groups: dict = {}
        for r in alerts:
            k = keyfn(r)
            if k is None:
                continue
            groups.setdefault(k, []).append(r)
        items = [(k, _stats(v)) for k, v in groups.items()
                 if len(v) >= SEG_MIN_N]
        if not items:
            return []
        if sort_by_winrate:
            items.sort(key=lambda x: -(x[1]["win_rate"] or -1))
        out = [f"\n📊 <b>{title}</b>"]
        for k, s in items:
            wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"
            out.append(f"   • {esc(str(k))}: نجاح {wr} "
                       f"({s['wins']}✅/{s['losses']}🛑/{s['timeouts']}⏳ · "
                       f"{s['avg_gain']:+.0f}% متوسط)")
        return out

    # ── الفرص الفائتة (مستقلة عن حجم العيّنة — تظهر فورًا) ─────────
    def missed_block():
        if not missed:
            return []
        out = [f"\n👻 <b>فرص فائتة (مرفوض صعد ≥{int(cfg.missed_rise_pct)}%)</b>: "
               f"<b>{len(missed)}</b>"]
        # تجميع حسب سبب الرفض = أي بوّابة فوّتت رَنرات صاعدة
        by_reason: dict = {}
        for m in missed:
            reason = (m["reject_reason"] or "غير معروف").split("(")[0].strip()
            by_reason.setdefault(reason, []).append(m)
        out.append("   أكثر البوابات تفويتًا:")
        for reason, items in sorted(by_reason.items(),
                                    key=lambda x: -len(x[1]))[:4]:
            out.append(f"   • {esc(reason)}: {len(items)} سهم")
        out.append("   أقوى الفائتة:")
        for m in missed[:6]:
            out.append(f"   • {esc(m['ticker'])}: +{m['max_gain_pct']:.0f}% "
                       f"(رُفض: {esc((m['reject_reason'] or '')[:40])})")
        out.append("   ↳ راجِع هذي البوابات — قد تكون متشدّدة.")
        return out

    if len(alerts) < cfg.dev_min_sample:
        head.append(f"⏳ نتائج محسومة قليلة (أقل من {cfg.dev_min_sample}) — "
                    "التشخيص بالشرائح يتراكم. الفرص الفائتة تظهر الآن:")
        head += missed_block()
        head.append("\n⚠️ <i>أداة تطوير ذاتي — ليست توصية.</i>")
        return "\n".join(head)

    overall = _stats(alerts)
    wr = f"{overall['win_rate']:.0f}%" if overall["win_rate"] is not None else "—"
    head.append(f"النجاح الكلي: <b>{wr}</b> "
                f"({overall['wins']}✅/{overall['losses']}🛑/"
                f"{overall['timeouts']}⏳) · متوسط أقصى ربح "
                f"{overall['avg_gain']:+.0f}%")

    body = []
    body += seg("حسب الجلسة", lambda r: r["session"])
    body += seg("حسب الخبر/المحفّز",
                lambda r: "بمحفّز 📰" if r["had_news"] else "بلا محفّز")
    body += seg("حسب الفلوت", lambda r: _bucket(
        (r["float_shares"] or 0) / 1e6 if r["float_shares"] else None,
        [(0, 5, "أقل من 5م"), (5, 10, "5-10م"), (10, 1e9, "أكثر من 10م")]))
    body += seg("حسب RVol", lambda r: _bucket(
        r["rvol"], [(0, 8, "5-8x"), (8, 15, "8-15x"), (15, 1e9, "15x أو أكثر")]))
    body += seg("حسب 5min RVol", lambda r: _bucket(
        r["rvol_5min"], [(0, 5, "أقل من 5x"), (5, 15, "5-15x"),
                         (15, 1e9, "15x أو أكثر")]))
    body += seg("حسب الدرجة", lambda r: _bucket(
        r["score"], [(0, 70, "60-70"), (70, 80, "70-80"), (80, 90, "80-90"),
                     (90, 1e9, "90-100")]))
    body += seg("حسب الجاهزية الفنية", lambda r: _bucket(
        r["readiness"], [(70, 80, "70-80"), (80, 90, "80-90"),
                         (90, 1e9, "90-100")]))

    # ── أنماط الخاسرين ───────────────────────────────────────────
    losses = [r for r in alerts if r["outcome"] == "loss"]
    if losses:
        fails = ["\n🛑 <b>أنماط الخاسرين</b>"]
        sess_cnt: dict = {}
        for r in losses:
            sess_cnt[r["session"]] = sess_cnt.get(r["session"], 0) + 1
        top = sorted(sess_cnt.items(), key=lambda x: -x[1])[:3]
        fails.append("   أكثر الجلسات خسارة: "
                     + "، ".join(f"{esc(str(k))} ({v})" for k, v in top))
        no_news_loss = sum(1 for r in losses if not r["had_news"])
        fails.append(f"   {no_news_loss}/{len(losses)} من الخاسرين بلا محفّز")
        body += fails

    body += missed_block()

    # ── اقتراحات ضبط (اقتراح فقط) ────────────────────────────────
    sugg = ["\n💡 <b>اقتراحات ضبط (للمراجعة فقط — لا تُطبّق تلقائيًا)</b>"]
    base_wr = overall["win_rate"] or 0

    # الخبر: لو النجاح بمحفّز أعلى بوضوح
    with_news = _stats([r for r in alerts if r["had_news"]])
    no_news = _stats([r for r in alerts if not r["had_news"]])
    if (with_news["n"] >= 5 and no_news["n"] >= 5
            and with_news["win_rate"] is not None
            and no_news["win_rate"] is not None
            and with_news["win_rate"] - no_news["win_rate"] >= 20):
        sugg.append(f"   • نجاح «بمحفّز» ({with_news['win_rate']:.0f}%) أعلى "
                    f"بوضوح من «بلا محفّز» ({no_news['win_rate']:.0f}%) — "
                    "فكّر برفع وزن الخبر في الدرجة أو جعله بوّابة.")

    # فرص فائتة بسبب RVol → خفّض RVOL_MIN
    rvol_missed = [m for m in missed if "RVol" in (m["reject_reason"] or "")]
    if len(rvol_missed) >= 3:
        sugg.append(f"   • {len(rvol_missed)} رَنر فاتنا بسبب بوّابة RVol — "
                    f"فكّر بخفض RVOL_MIN (حاليًا {cfg.rvol_min:g}x).")

    # فرص فائتة بسبب الفلوت → ارفع FLOAT_MAX
    float_missed = [m for m in missed if "فلوت" in (m["reject_reason"] or "")]
    if len(float_missed) >= 3:
        sugg.append(f"   • {len(float_missed)} رَنر فاتنا بسبب بوّابة الفلوت — "
                    f"فكّر برفع FLOAT_MAX (حاليًا {_human(cfg.float_max)}).")

    # شريحة جاهزية عالية لكن نجاحها متدنٍّ؟ (نادر — مؤشّر للمراجعة)
    high_ready = _stats([r for r in alerts if (r["readiness"] or 0) >= 90])
    if (high_ready["n"] >= 5 and high_ready["win_rate"] is not None
            and high_ready["win_rate"] < base_wr - 15):
        sugg.append("   • جاهزية 90+ نجاحها أقل من المعدل — "
                    "الجاهزية وحدها لا تكفي، راجِع وزن الزخم.")

    # غلبة انتهاء النوافذ → الأهداف بعيدة
    if overall["timeouts"] >= max(5, overall["n"] // 2):
        sugg.append("   • أغلب التنبيهات تنتهي نافذتها بلا حسم — قد تكون "
                    "الأهداف بعيدة أو نافذة المتابعة قصيرة.")

    if len(sugg) == 1:
        sugg.append("   • لا نمط واضح بعد — البيانات متّسقة أو غير كافية.")

    tail = ["", "⚠️ <i>أداة تطوير ذاتي تتعلّم من نتائج البوت — ليست توصية. "
            "الاقتراحات للمراجعة البشرية فقط.</i>"]
    return "\n".join(head + body + sugg + tail)


# ── تشغيل يدوي: python -m runner_scanner.dev_assistant ───────────
def main() -> int:
    from .alerts import TelegramSender
    from .state import Store

    cfg = Config.from_env()
    store = Store(cfg.db_path)
    report = build_dev_report(store, cfg)
    print(report)
    if not cfg.dry_run and cfg.telegram_bot_token and cfg.telegram_chat_id:
        TelegramSender(cfg).send(report)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
