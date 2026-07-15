"""البوابات الصارمة (القسم 6) — أي بوابة تفشل = رفض، لا يُعرض.

المستخدم يريد فقط الأسهم بزخم قوي **وجاهزية فنية**. هذي البوابات تصفّي
قبل التحليل المكلف (شموع/تحليل كلاسيكي).

ملاحظات حاسمة من القرارات:
- الفلوت best-effort: لو غاب → لا رفض صامت، بل وسم «float unknown» +
  أولوية أدنى (يُعالَج في scoring). هنا نمرّره فقط بدون رفض.
- RVol يُحسب حسب الجلسة (يُمرَّر محسوبًا مسبقًا من sessions.compute_rvol).
- الامتداد البارابولِك: رفض المنهك.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import Candidate, FloatSource, Session


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    reason_code: str = ""   # كود ثابت للرفض (DEBT-13) — للتصنيف الآلي لا العرض


def check_price(cfg: Config, c: Candidate) -> GateResult:
    p = c.snapshot.last_price
    if p < cfg.price_min:
        return GateResult(False, f"سعر {p:.2f} < {cfg.price_min} (سنتات)",
                          "price_low")
    if p > cfg.price_max:
        return GateResult(False, f"سعر {p:.2f} > {cfg.price_max} (فوق نطاق الأسهم)",
                          "price_high")
    return GateResult(True)


def check_volume(cfg: Config, c: Candidate) -> GateResult:
    """شبكة أمان لسيولة الخروج. **RVol هو مقياس النشاط الأساس** (نسبي/لحظي).

    - معطّلة (VOLUME_GATE_ENABLED=false) → الاعتماد كليًا على RVol.
    - حجم ≤ 0 (artifact بريماركت: سهم صاعد +X% بحجم «صفر» مستحيل منطقيًا)
      → لا رفض، نعتمد على RVol لاحقًا (طلب المستخدم: «لو تروح السيولة → RVol»).
    """
    if not cfg.volume_gate_enabled:
        return GateResult(True, "بوّابة الحجم معطّلة (RVol وحده)")
    # الجلسات الممتدة: day.v حجم جزئي للجلسة (≠ يوم كامل) → لا نقيسه بعتبة اليوم؛
    # RVol الجلسي (واعٍ بالجلسة) هو الحكم. هذا يمنع رفض موفرز بريماركت حقيقيين.
    if c.session in (Session.PREMARKET, Session.AFTERHOURS):
        return GateResult(True, "حجم جزئي للجلسة → نعتمد على RVol الجلسي")
    v = c.snapshot.day_volume
    if v <= 0:
        return GateResult(True, "حجم غير موثوق → نعتمد على RVol")
    if v < cfg.volume_min:
        return GateResult(False, f"حجم {v:,.0f} < {cfg.volume_min:,.0f} (سيولة ضعيفة)",
                          "volume")
    return GateResult(True)


def check_float(cfg: Config, c: Candidate) -> GateResult:
    """فلوت ≤ FLOAT_MAX. لو الفلوت مجهول → يمرّ (best-effort، لا رفض صامت)."""
    if c.float_shares is None or c.float_source is FloatSource.UNKNOWN:
        return GateResult(True, "float unknown")  # يمرّ، تُخفض أولويته لاحقًا
    if c.float_shares > cfg.float_max:
        return GateResult(False, f"فلوت {c.float_shares:,.0f} > {cfg.float_max:,.0f}",
                          "float")
    return GateResult(True)


def check_rvol(cfg: Config, c: Candidate) -> GateResult:
    """RVol ≥ RVOL_MIN. يعتمد على momentum.rvol المحسوب حسب الجلسة.

    BUG-11 (§3): لو غاب الزخم أو كان RVol **مجهولًا** (لا أساس تاريخي موثوق)
    → لا رفض (بيانات مفقودة ≠ صفر نشاط؛ نمط check_vwap). الدرجة تُخفَّض تلقائيًّا
    (rvol≈0 → 0 نقاط زخم) فقد يسقط على بوّابة الدرجة — تدهور آمن لا رفض صامت."""
    if c.momentum is None:
        # لم يُحسب بعد — لا نرفض هنا (تُستدعى البوابة بعد intraday_ta)
        return GateResult(True)
    if not c.momentum.rvol_reliable:
        return GateResult(True)   # مجهول لا صفر → لا رفض (يُخفَّض بالدرجة)
    if c.momentum.rvol < cfg.rvol_min:
        return GateResult(False, f"RVol {c.momentum.rvol:.1f}x < {cfg.rvol_min}x",
                          "rvol")
    return GateResult(True)


# بورصات OTC/pink تُستبعد (نراهن على ناسداك/نيويورك)
_OTC_EXCHANGES = {"OTC", "OTCM", "PSGM", "OTCB", "OTCQ", "OTCQX", "OTCQB",
                  "PINX", "GREY", "XOTC", "EXPM"}


def check_listing(cfg: Config, c: Candidate) -> GateResult:
    """يستبعد غير الأسهم العادية (وارنت/يونت/رايت/ممتاز/ETF) و OTC.

    مجهول النوع/البورصة → يعدّي (فائدة الشك، لا رفض صامت على بيانات ناقصة).
    """
    t = (c.ticker_type or "").upper()
    if t and t not in cfg.allowed_ticker_types:
        return GateResult(False, f"نوع الورقة {t} (ليس سهمًا عاديًا)", "type")
    exch = (c.primary_exchange or "").upper()
    if cfg.exclude_otc and exch and (exch in _OTC_EXCHANGES or "OTC" in exch):
        return GateResult(False, f"بورصة {exch} (OTC)", "exchange")
    return GateResult(True)


def check_vwap(cfg: Config, c: Candidate) -> GateResult:
    """تنبيه **فوق VWAP فقط** (قرار المستخدم بالبيانات، 6 أشهر): شريحة تحت VWAP
    وقت التنبيه 55% نجاح < تعادل 64% (خاسرة صافيًا بوقف −7% ثابت).

    best-effort §4: يُطبَّق فقط على VWAP **موثوق** — لو غاب الزخم أو كان VWAP
    artifact صفريًّا (جلسات ممتدة) → لا رفض (بيانات مفقودة ≠ رفض؛ above_vwap
    يكون False افتراضيًا وقتها فلا نبني عليه). يُعاد الفحص كل دورة: سهم يستعيد
    VWAP يُنبَّه لاحقًا (لا إسقاط دائم)."""
    if not cfg.vwap_gate_enabled:
        return GateResult(True)
    if c.momentum is None or not c.momentum.vwap_reliable:
        return GateResult(True)   # §4: VWAP غير موثوق/غير محسوب → لا نرفض عليه
    if not c.momentum.above_vwap:
        return GateResult(False, "تحت VWAP (شريحة أضعف تاريخيًا 55%)", "vwap")
    return GateResult(True)


def check_entry_change(cfg: Config, c: Candidate) -> GateResult:
    """سقف حركة الدخول (قرار المستخدم بالبيانات، 6 أشهر): لا تنبيه على سهم صعد
    ≥ entry_change_max_pct عن أمس — الدخول بعد صعود كبير شريحة خاسرة متّسقة
    (−2%/صفقة، سالبة 5/6 أشهر)، بينما شريحة 10–20% هي الأقوى (+2.3%).

    أخصّ من البارابولِك (blow-off 120%): هذه عتبة استراتيجية لا سلامة بيانات.
    0 = تعطيل. §5: «≥» آمنة، بلا محارف < أو > حرفية في السبب."""
    if cfg.entry_change_max_pct <= 0:
        return GateResult(True)
    if c.snapshot.change_pct >= cfg.entry_change_max_pct:
        return GateResult(
            False,
            f"حركة متقدّمة {c.snapshot.change_pct:.0f}% "
            f"≥ {cfg.entry_change_max_pct:g}% (شريحة خاسرة تاريخيًا −2%/صفقة)",
            "late_wave")
    return GateResult(True)


def check_parabolic(cfg: Config, c: Candidate) -> GateResult:
    """رفض البارابولِك المنهك (خطر blow-off)."""
    # ابتعاد كبير عن إغلاق أمس
    if c.snapshot.change_pct >= cfg.parabolic_day_change_pct:
        return GateResult(
            False,
            f"بارابولِك: +{c.snapshot.change_pct:.0f}% عن أمس "
            f"≥ {cfg.parabolic_day_change_pct:.0f}% (منهك)",
            "parabolic",
        )
    # ابتعاد كبير عن VWAP (فقط لو VWAP موثوق — لا نرفض/نمرّر على artifact صفري)
    if c.momentum is not None and c.momentum.vwap_reliable and \
            c.momentum.vwap_distance_pct >= cfg.parabolic_vwap_ext_pct:
        return GateResult(
            False,
            f"بارابولِك: +{c.momentum.vwap_distance_pct:.0f}% فوق VWAP "
            f"≥ {cfg.parabolic_vwap_ext_pct:.0f}%",
            "parabolic",
        )
    return GateResult(True)


# بوابات لا تحتاج تحليلًا لحظيًا (تُطبّق مبكرًا قبل جلب الشموع).
PRE_TA_GATES = (check_listing, check_price, check_volume, check_float,
                check_entry_change, check_parabolic)
# بوابات تحتاج نتيجة الزخم (تُطبّق بعد intraday_ta).
POST_TA_GATES = (check_rvol, check_parabolic, check_vwap)


def apply_gates(cfg: Config, c: Candidate, gates) -> GateResult:
    """يطبّق سلسلة بوابات؛ يرجّع أول فشل، أو نجاح."""
    for gate in gates:
        res = gate(cfg, c)
        if not res.passed:
            return res
    return GateResult(True)
