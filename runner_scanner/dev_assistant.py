"""أداة التطوير (Dev Assistant) — تتعلّم من نتائج البوت المتراكمة.

مكيّفة لالماسح الشامل (لا تنسخ مشروع الارتكاز): تحلّل تتبّعات النتائج
(win/loss/timeout) من جدول tracking وتُنتج تقرير أداء بالشرائح المناسبة
لأسهم الزخم:

  • نسبة النجاح الكلية + بالشرائح (جلسة · فلوت · RVol · 5min RVol ·
    الدرجة · الجاهزية · الخبر)
  • 🛑 أنماط الخاسرين (أي شريحة تخسر أكثر)
  • 👻 الفرص الفائتة (مرفوض صعد ≥ نسبة) مجمّعة حسب سبب الرفض = أي بوّابة
    قد تكون متشدّدة
  • 💡 اقتراحات ضبط للبوابات (اقتراح فقط — لا يغيّر إعدادات)

«نجاح» = السهم بلغ الهدف الأول بعد التنبيه. «خسارة» = ضرب الوقف.
«انتهاء نافذة» = ما بلغ أيًّا منهما خلال نافذة المتابعة.

النصوص HTML-آمنة لتيليجرام. اقتراح فقط — لا يلمس الإعدادات (المستخدم يقرّر).
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from . import calibration
from .config import Config
from .textutil import esc   # هروب HTML مشترك (يُعاد تصديره للتوافق)

__all__ = ["esc", "build_dev_report", "export_csvs", "send_report_and_files"]


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
    """يرجّع إحصاء مجموعة: العدد · فائز/خاسر/منتهٍ · نسبة النجاح · متوسط أقصى ربح
    + **الوسيط واعتماد الذيل** (صدق التوزيع): المتوسط وحده يخدع — متوسط ≫ وسيط
    يعني الحافة يحملها ذيل قِلّة من الصفقات، لا الصفقة النموذجية."""
    n = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    timeouts = sum(1 for r in rows if r["result"] == "timeout")
    decisive = wins + losses
    win_rate = (wins / decisive * 100.0) if decisive else None
    gains = sorted((r["max_gain_pct"] or 0) for r in rows)
    avg_gain = (sum(gains) / n) if n else 0.0
    median_gain = gains[n // 2] if n else 0.0     # الصفقة النموذجية
    # اعتماد الذيل: حصّة أعلى 20% من الصفقات من إجمالي القمم (>~60% = هشّة)
    k = max(1, n // 5)
    total = sum(gains)
    tail_share = (sum(gains[-k:]) / total * 100.0) if total > 0 else None
    return {"n": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "win_rate": win_rate, "avg_gain": avg_gain,
            "median_gain": median_gain, "tail_share": tail_share}


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

    head = ["🔬 <b>مساعد التطوير — أداء الماسح الشامل</b>",
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
                       f"{s['avg_gain']:+.0f}% متوسط · {s['median_gain']:+.0f}% وسيط)")
        return out

    # ── الفرص الفائتة (مستقلة عن حجم العيّنة — تظهر فورًا) ─────────
    # القمة وحدها تخدع (FOMO): نعرض معها القاع وكم سهمًا لمس **مسافة الوقف**
    # خلال النافذة — «فائتة» قاعها أعمق من الوقف كانت غالبًا ستُوقَف لا تُربَح.
    def missed_block():
        if not missed:
            return []
        stop_d = cfg.stop_fixed_pct   # مسافة الوقف الثابتة (نفس وقف البطاقة)
        out = [f"\n👻 <b>فرص فائتة (مرفوض صعد ≥{int(cfg.missed_rise_pct)}%)</b>: "
               f"<b>{len(missed)}</b>"]
        # تجميع حسب سبب الرفض = أي بوّابة فوّتت أسهم صاعدة (قمة + قاع معًا)
        by_reason: dict = {}
        for m in missed:
            reason = (m["reject_reason"] or "غير معروف").split("(")[0].strip()
            by_reason.setdefault(reason, []).append(m)
        out.append("   أكثر البوابات تفويتًا (القمة لا تكفي — انظر القاع):")

        def _med(vals):
            s = sorted(vals)
            return s[len(s) // 2] if s else 0.0

        def _order(m):
            # ترتيب القمة/القاع: None=لا بيانات (صفّ قديم) · True=الوقف أولًا
            # · False=القمة أولًا. المقارنة نصوص ISO (قابلة للترتيب معجميًّا).
            pk = m["peak_at"]
            if not pk:
                return None
            sd = m["stop_dist_at"]
            return bool(sd) and sd < pk
        for reason, items in sorted(by_reason.items(),
                                    key=lambda x: -len(x[1]))[:4]:
            g = _med([m["max_gain_pct"] or 0 for m in items])
            d = _med([m["max_draw_pct"] or 0 for m in items])
            hit = sum(1 for m in items if (m["max_draw_pct"] or 0) <= -stop_d)
            out.append(f"   • {esc(reason)}: {len(items)} سهم · وسيط القمة "
                       f"+{g:.0f}% · وسيط القاع {d:.0f}% · "
                       f"لمس مسافة الوقف ({stop_d:g}%): {hit}/{len(items)}")
        # حسم مؤكّد بالترتيب الزمني: كم سهم فائت **لُمس وقفه قبل قمته** فعلًا؟
        # (للصفوف الجديدة فقط — القديمة بلا طوابع تبقى «غير مسجّل»).
        ordered = [m for m in missed if _order(m) is not None]
        if ordered:
            first_stop = sum(1 for m in ordered if _order(m))
            out.append(f"   ⛔ سُتوقَف قبل القمة (مؤكّد بالترتيب الزمني): "
                       f"{first_stop}/{len(ordered)} — القمة FOMO لا تعني ربحًا.")
        out.append("   أقوى الفائتة (قمة/قاع):")
        for m in missed[:6]:
            o = _order(m)
            tag = " ⛔ الوقف أولًا" if o else (" ✅ القمة أولًا" if o is False else "")
            out.append(f"   • {esc(m['ticker'])}: +{m['max_gain_pct']:.0f}% / "
                       f"{(m['max_draw_pct'] or 0):.0f}%{tag} "
                       f"(رُفض: {esc((m['reject_reason'] or '')[:40])})")
        out.append("   ↳ قاع أعمق من الوقف = كانت غالبًا سُتوقَف قبل القمة — "
                   "لا تفتح بوّابة على القمم وحدها.")
        return out

    # ── مقارنة «قبل/بعد» (أسبوع مقابل أسبوع) ──────────────────────
    # يقيس أثر تغييرات الفرز على النتائج الحيّة الفعلية (لا المحاكاة):
    # نسبة الفوز · متوسط أقصى ربح · لمس الوقف — آخر نافذة مقابل السابقة.
    # يُقسَّم حسب trade_date (نصّ ISO فيُقارَن معجميًّا مباشرةً).
    def compare_block():
        win = cfg.dev_compare_window_days
        today = (now.astimezone(timezone.utc) if now.tzinfo else now).date()
        cur_lo = (today - timedelta(days=win)).isoformat()
        prev_lo = (today - timedelta(days=2 * win)).isoformat()
        cur = [r for r in alerts if r["trade_date"] and r["trade_date"] > cur_lo]
        prev = [r for r in alerts if r["trade_date"]
                and prev_lo < r["trade_date"] <= cur_lo]
        if not cur and not prev:
            return []
        sc, sp = _stats(cur), _stats(prev)

        def _wr(s):
            return f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"

        def _arrow(c, p, better_high=True):
            # سهم اتجاه فقط عند توفّر الطرفين (لا نخترع دلالة من عدم)
            if c is None or p is None:
                return ""
            d = c - p
            if abs(d) < 0.5:
                return " ↔️"
            return " 🔼" if (d > 0) == better_high else " 🔽"

        out = [f"\n📅 <b>قبل/بعد (آخر {win} يوم مقابل الـ{win} السابقة)</b>"]
        out.append(f"   الصفقات المحسومة: {sc['n']} مقابل {sp['n']}")
        out.append(f"   نسبة الفوز: {_wr(sc)} مقابل {_wr(sp)}"
                   f"{_arrow(sc['win_rate'], sp['win_rate'])}")
        out.append(f"   متوسط أقصى ربح: {sc['avg_gain']:+.0f}% مقابل "
                   f"{sp['avg_gain']:+.0f}%"
                   f"{_arrow(sc['avg_gain'], sp['avg_gain'])}")
        # لمس الوقف: الأدنى أفضل (better_high=False)
        cur_stop = (sc["losses"] / sc["n"] * 100) if sc["n"] else None
        prev_stop = (sp["losses"] / sp["n"] * 100) if sp["n"] else None
        out.append(f"   لمس الوقف: {sc['losses']}/{sc['n']} مقابل "
                   f"{sp['losses']}/{sp['n']}"
                   f"{_arrow(cur_stop, prev_stop, better_high=False)}")
        # صدق العيّنة: نافذة صغيرة لا تحسم (قاعدة «المحاكاة تخدع» تنطبق على
        # الأرقام الصغيرة أيضًا — تقلّب لا إشارة).
        if min(sc["n"], sp["n"]) < cfg.dev_min_sample:
            out.append("   ↳ ⚠️ عيّنة صغيرة — تقلّب لا إشارة؛ راكِم أسابيع "
                       "أكثر قبل الحكم على أثر التغييرات.")
        else:
            out.append("   ↳ نتائج حيّة فعلية (لا محاكاة) — هذا قياس أثر "
                       "تغييرات الفرز على الأرض.")
        return out

    if len(alerts) < cfg.dev_min_sample:
        head.append(f"⏳ نتائج محسومة قليلة (أقل من {cfg.dev_min_sample}) — "
                    "التشخيص بالشرائح يتراكم. الفرص الفائتة تظهر الآن:")
        head += compare_block()
        head += missed_block()
        head.append("\n⚠️ <i>أداة تطوير ذاتي — ليست توصية.</i>")
        return "\n".join(head)

    overall = _stats(alerts)
    wr = f"{overall['win_rate']:.0f}%" if overall["win_rate"] is not None else "—"
    head.append(f"النجاح الكلي: <b>{wr}</b> "
                f"({overall['wins']}✅/{overall['losses']}🛑/"
                f"{overall['timeouts']}⏳) · متوسط أقصى ربح "
                f"{overall['avg_gain']:+.0f}% · وسيط {overall['median_gain']:+.0f}%")
    # صدق التوزيع (مقتبس من أداة الباكتيست): متوسط ≫ وسيط = الحافة يحملها ذيل
    # قِلّة من الصفقات لا الصفقة النموذجية → المتوسط يخدع.
    if (overall["tail_share"] is not None
            and overall["avg_gain"] >
            overall["median_gain"] * cfg.dev_tail_warn_mult + 3):
        head.append(f"   ⚠️ صدق التوزيع: أعلى 20% من الصفقات = "
                    f"{overall['tail_share']:.0f}% من إجمالي القمم "
                    "(المتوسط يخدع؛ الصفقة النموذجية أضعف — لا تبنِ على المتوسط).")
    head += compare_block()

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
    losses = [r for r in alerts if r["result"] == "loss"]
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
        sugg.append(f"   • {len(rvol_missed)} سهم فاتنا بسبب بوّابة RVol — "
                    f"فكّر بخفض RVOL_MIN (حاليًا {cfg.rvol_min:g}x).")

    # فرص فائتة بسبب الفلوت → ارفع FLOAT_MAX
    float_missed = [m for m in missed if "فلوت" in (m["reject_reason"] or "")]
    if len(float_missed) >= 3:
        sugg.append(f"   • {len(float_missed)} سهم فاتنا بسبب بوّابة الفلوت — "
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

    # ── معايرة كمّية محدّدة (أرقام جاهزة للّصق) ───────────────────
    calib = calibration.format_proposals(
        calibration.propose_calibrations(store, cfg))

    tail = ["", "⚠️ <i>أداة تطوير ذاتي تتعلّم من نتائج البوت — ليست توصية. "
            "الاقتراحات للمراجعة البشرية فقط.</i>"]
    parts = head + body + sugg
    if calib:
        parts.append(calib)
    return "\n".join(parts + tail)


def top_action(store, cfg: Config) -> str:
    """**أهم إجراء واحد الآن** — يلخّص اقتراحات المعايرة الكمّية في سطر قابل
    للتنفيذ («المساعد يقول لك وش تسوي»). يختار الأعلى ثقة (عيّنة أكبر) أولًا.
    اقتراح لا تنفيذ (هوية البوت): يعرض الرقم الجاهز، والمستخدم يقرّر.
    """
    props = calibration.propose_calibrations(store, cfg)
    if not props:
        return ("🎯 <b>أهم إجراء الآن</b>\n"
                "   • لا إجراء عاجل — البيانات متّسقة أو العيّنة غير كافية بعد. "
                "اجمع نتائج أكثر ثم راجِع /report.")
    # الأعلى ثقة أولًا (عالية > متوسطة)، مع الحفاظ على ترتيب الأولوية القائم
    top = sorted(props, key=lambda p: 0 if p.confidence == "عالية" else 1)[0]
    extra = (f"\n   <i>↳ يوجد {len(props) - 1} اقتراح آخر — /report للكل.</i>"
             if len(props) > 1 else "")
    return ("🎯 <b>أهم إجراء الآن (للمراجعة — لا يُطبَّق تلقائيًا)</b>\n"
            + top.line() + "\n"
            "   <i>↳ غيّره في Render → Environment إن اقتنعت، ثم /sha للتأكّد.</i>"
            + extra)


# ── تصدير CSV (ملفات الصفقات والفرص الفائتة) ─────────────────────
def _write_csv(rows: list, path: str) -> str | None:
    """يكتب صفوف sqlite3.Row إلى CSV ويرجّع المسار (أو None لو فاضي)."""
    if not rows:
        return None
    cols = list(rows[0].keys())
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        return path
    except OSError:
        return None


def export_csvs(store, cfg: Config, now: datetime | None = None
                ) -> list[tuple[str, str]]:
    """يصدّر CSV للتتبّعات المحسومة والفرص الفائتة. يرجّع [(مسار، وصف)]."""
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    tmp = tempfile.mkdtemp(prefix="runner_dev_")
    out: list[tuple[str, str]] = []
    p1 = _write_csv(store.fetch_resolved(only_alerts=False),
                    os.path.join(tmp, f"tracking_{day}.csv"))
    if p1:
        out.append((p1, f"📎 التتبّعات المحسومة ونتائجها — {day}"))
    p2 = _write_csv(store.fetch_missed(cfg.missed_rise_pct),
                    os.path.join(tmp, f"missed_{day}.csv"))
    if p2:
        out.append((p2, f"📎 الفرص الفائتة (مرفوض صعد) — {day}"))
    return out


def send_report_and_files(store, cfg: Config, telegram,
                          now: datetime | None = None) -> str:
    """يبني التقرير، يرسله، ثم يرسل ملفات CSV. يرجّع نص التقرير."""
    report = build_dev_report(store, cfg, now)
    telegram.send(report)
    for path, caption in export_csvs(store, cfg, now):
        telegram.send_document(path, caption)
    return report


# ── تشغيل يدوي: python -m runner_scanner.dev_assistant ───────────
def main() -> int:
    from .alerts import TelegramSender
    from .state import Store

    cfg = Config.from_env()
    store = Store(cfg.db_path)
    report = send_report_and_files(store, cfg, TelegramSender(cfg))
    print(report)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
