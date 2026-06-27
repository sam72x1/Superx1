"""معايرة العتبات A/B — يجرّب عتبات مختلفة على نفس البيانات التاريخية ويرتّب
حسب النجاح، ليطلع لك في الويكند: «أعلى تركيبة عتبات أعطت أفضل نتيجة تاريخيًا».

المبدأ (هوية البوت): **يقترح ولا يطبّق**. يطلع الأرقام + اسم متغيّر البيئة
المقترح، وأنت تقرّر تغيّره في Render أو لا (العتبات قرار المستخدم بالبيانات).

⚠️ نفس ضمانة الباكتيست: لا تسرّب مستقبل (يعيد استخدام محرّك backtest كما هو).
الكفاءة: نلفّ عميل الأساس بمذكّر (_MemoClient) فالجلب الشبكي يحصل **مرة واحدة**
ويُعاد استخدامه عبر كل تمريرات A/B (التقييم متكرّر، الشبكة لا).

النطاق: نغيّر **عتبة واحدة كل مرة** (الباقي ثابت على الأساس) — أوضح وأصدق من
بحث شبكي كامل (يتجنّب إفراط الملاءمة على عيّنة صغيرة). المحاور:
- الجاهزية الفنية (TECH_READINESS_MIN)
- سقف الفلوت (FLOAT_MAX)
- حدّ البارابولِك (PARABOLIC_DAY_CHANGE_PCT)
"""

from __future__ import annotations

import logging
from dataclasses import replace

from .backtest import run_backtest, trading_days
from .config import Config

logger = logging.getLogger(__name__)


# ── مذكّر يشارك الجلب الشبكي عبر كل تمريرات A/B ───────────────────
class _MemoClient:
    """يغلّف عميل الأساس ويحفظ ردّ كل نداء شبكي (الاسم+الوسائط) في الذاكرة.

    تمريرات A/B تطلب نفس البيانات (نفس المرشّحين/الشموع/التاريخ) لأن العتبات
    التي نغيّرها لا تؤثّر على الجلب — فجلب واحد يكفي للجميع.
    """

    def __init__(self, base):
        self._base = base
        self._cache: dict = {}

    def __getattr__(self, name):
        # يُستدعى فقط لما لا يُوجد المُعرّف على المثيل (أي لطرق العميل)
        attr = getattr(self._base, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            if key not in self._cache:
                self._cache[key] = attr(*args, **kwargs)
            return self._cache[key]

        return wrapped


def memoized(base):
    """يلفّ عميلًا بمذكّر (idempotent) ليُشارَك بين الباكتيست والمعايرة."""
    return base if isinstance(base, _MemoClient) else _MemoClient(base)


# ── إحصاءات تركيبة واحدة ─────────────────────────────────────────
def _variant_stats(res) -> dict:
    s = res.stats()
    s["decisive"] = s["wins"] + s["losses"]
    return s


def _fmt_num(v: float) -> str:
    """عرض مختصر: 40000000 → 40M، 65 → 65."""
    return f"{v / 1e6:.0f}M" if v >= 1e6 else f"{v:.0f}"


def _env_value(v: float) -> str:
    """القيمة كما تُكتب في متغيّر البيئة (FLOAT_MAX=60000000)."""
    return str(int(v))


# ── تشغيل الشبكة (محور واحد متغيّر كل مرة) ────────────────────────
def run_grid(cfg: Config, base, start: str, end: str, progress=None) -> dict:
    """يرجّع نتيجة منظّمة: الأساس + لكل محور تركيباته + أفضل اقتراح."""
    memo = memoized(base)
    cache: dict = {}

    def run(readiness: float, fmax: float, para: float) -> dict:
        key = (readiness, fmax, para)
        if key not in cache:
            vcfg = replace(cfg, tech_readiness_min=readiness,
                           float_max=fmax, parabolic_day_change_pct=para)
            cache[key] = _variant_stats(run_backtest(vcfg, memo, start, end))
        return cache[key]

    base_vals = (cfg.tech_readiness_min, cfg.float_max,
                 cfg.parabolic_day_change_pct)
    baseline = run(*base_vals)

    axis_defs = [
        ("TECH_READINESS_MIN", "الجاهزية الفنية", 0, cfg.backtest_grid_readiness),
        ("FLOAT_MAX", "سقف الفلوت", 1, cfg.backtest_grid_float_max),
        ("PARABOLIC_DAY_CHANGE_PCT", "حدّ البارابولِك", 2,
         cfg.backtest_grid_parabolic),
    ]
    axes = []
    for i, (env, title, idx, values) in enumerate(axis_defs, 1):
        variants = []
        for v in values:
            args = list(base_vals)
            args[idx] = v
            variants.append({
                "value": v, "stats": run(*args),
                "is_baseline": v == base_vals[idx]})
        axes.append({"env": env, "title": title,
                     "baseline_value": base_vals[idx], "variants": variants})
        if progress:
            progress(i, len(axis_defs), env)

    best = _pick_best(axes, baseline, cfg)
    return {
        "start": start, "end": end, "days": len(trading_days(start, end)),
        "baseline": baseline, "baseline_vals": base_vals, "axes": axes,
        "best": best, "min_decisive": cfg.backtest_grid_min_decisive,
    }


def _pick_best(axes: list[dict], baseline: dict, cfg: Config):
    """أعلى تركيبة نجاحًا بعيّنة كافية وتفوّق على الأساس بهامش (وإلا None)."""
    base_wr = baseline["win_rate"]
    if base_wr is None:
        return None
    best = None
    for ax in axes:
        for v in ax["variants"]:
            if v["is_baseline"]:
                continue
            st = v["stats"]
            wr = st["win_rate"]
            if wr is None or st["decisive"] < cfg.backtest_grid_min_decisive:
                continue
            if wr < base_wr + cfg.backtest_grid_min_edge:
                continue
            if best is None or wr > best["win_rate"]:
                best = {"env": ax["env"], "title": ax["title"],
                        "value": v["value"], "win_rate": wr,
                        "decisive": st["decisive"], "baseline_win_rate": base_wr}
    return best


# ── التقرير (عربي، آمن HTML — لا نص خارجي) ────────────────────────
def format_grid_report(grid: dict) -> str:
    b = grid["baseline"]
    rd, fm, pa = grid["baseline_vals"]
    md = grid["min_decisive"]

    def wr(s) -> str:
        return f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"

    lines = [
        f"🧪 <b>معايرة العتبات (A/B)</b> {grid['start']} → {grid['end']} "
        f"({grid['days']} يوم تداول)",
        f"الأساس الحالي: جاهزية≥{rd:.0f} · فلوت≤{_fmt_num(fm)} · "
        f"بارابولِك≥{pa:.0f}",
        f"  → نجاح {wr(b)} ({b['wins']}✅/{b['losses']}🛑/{b['timeouts']}⏳) · "
        f"{b['decisive']} محسومة من {b['alerts']} صفقة",
        "",
        "🔬 <i>غيّرنا عتبة واحدة كل مرة (الباقي ثابت):</i>",
    ]

    base_wr = b["win_rate"]
    for ax in grid["axes"]:
        lines.append(f"\n<b>{ax['title']}</b> (<code>{ax['env']}</code>):")
        for v in ax["variants"]:
            st = v["stats"]
            w = st["win_rate"]
            if v["is_baseline"]:
                arrow, tag = "", " ← الأساس"
            elif w is None or base_wr is None:
                arrow, tag = "", ""
            elif w > base_wr:
                arrow, tag = "⬆️", ""
            elif w < base_wr:
                arrow, tag = "⬇️", ""
            else:
                arrow, tag = "➡️", ""
            small = (" ⚠️عيّنة صغيرة"
                     if (not v["is_baseline"] and st["decisive"] < md) else "")
            lines.append(
                f"  • {_fmt_num(v['value'])} → {wr(st)} "
                f"({st['wins']}✅/{st['losses']}🛑) {arrow}{tag}{small}")

    lines.append("")
    best = grid["best"]
    if best:
        lines.append(f"💡 <b>الأعلى نجاحًا</b> (عيّنة ≥{md} محسومة):")
        lines.append(
            f"  <code>{best['env']}={_env_value(best['value'])}</code> → "
            f"{best['win_rate']:.0f}% (مقابل {best['baseline_win_rate']:.0f}% "
            f"الحالي)")
    else:
        lines.append(
            "💡 ما فيه تركيبة تتفوّق على الأساس بهامش وعيّنة كافية — "
            "الأساس الحالي جيّد، أو العيّنة صغيرة.")

    lines += [
        "",
        "⚠️ <b>اقتراحات للمراجعة فقط — البوت ما يغيّر شيئًا تلقائيًا.</b>",
        "غيّرها بنفسك في Render (متغيّرات البيئة) لو قرّرت، وراقب بعدها.",
        "تقدير تاريخي على عيّنة محدودة — لا يضمن المستقبل (احذر إفراط الملاءمة).",
    ]
    return "\n".join(lines)
