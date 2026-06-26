"""نماذج البيانات المشتركة (dataclasses) — عقود بين الوحدات.

كل وحدة تتعامل مع هذي الأنواع بدل قواميس خام، عشان الكود يبقى متماسك
وقابل للاختبار.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Session(str, Enum):
    """الجلسة الحالية للسوق الأمريكي (بتوقيت ET)."""

    PREMARKET = "بريماركت"
    REGULAR = "رسمي"
    AFTERHOURS = "أفترهاوس"
    CLOSED = "مغلق"


class HaltState(str, Enum):
    """حالة توقّف التداول للسهم."""

    NORMAL = "طبيعي"
    HALTED = "متوقّف"          # LULD pause حالي
    RESUMED = "مستأنف"        # استأنف لكن لسه ضمن نافذة التجاهل
    T12 = "T12"               # توقّف إفصاح → استبعاد نهائي


class FloatSource(str, Enum):
    """مصدر قيمة الفلوت (للمعايرة والشفافية)."""

    FLOAT_ENDPOINT = "float-endpoint"      # /stocks/vX/float (الأدقّ)
    SHARES_OUTSTANDING = "shares-outstanding"  # fallback — ليس فلوت حقيقي
    UNKNOWN = "unknown"


@dataclass
class SnapshotEntry:
    """مدخل واحد من Full Market Snapshot (مبسّط ومُطبّع)."""

    ticker: str
    last_price: float
    prev_close: float
    day_open: float
    day_high: float
    day_low: float
    day_volume: float
    day_vwap: float          # vw من شمعة اليوم (تقريب، مو session-anchored)
    change_pct: float        # todays_change_perc عن إغلاق أمس
    updated_ns: int = 0      # طابع زمني بالنانوثانية إن وُجد

    @property
    def is_valid(self) -> bool:
        """صحيح فقط لو فيه سعر وإغلاق أمس (نتجنّب بيانات نافذة المسح 3:30-4ص)."""
        return self.last_price > 0 and self.prev_close > 0


@dataclass
class Bar:
    """شمعة (aggregate bar) واحدة."""

    t_ms: int        # طابع بداية النافذة بالمللي ثانية (ET)
    o: float
    h: float
    l: float
    c: float
    v: float
    vw: float = 0.0  # VWAP لهذي الشمعة فقط
    n: int = 0       # عدد الصفقات


@dataclass
class MomentumResult:
    """ناتج ركيزة الزخم اللحظي (/50)."""

    score: float                      # 0..momentum_pillar_max
    rvol: float                       # RVol حسب الجلسة
    rvol_5min: float                  # RVol خمس دقائق (عمود مهم في scanner)
    change_5min_pct: float            # تغيّر آخر 5د%
    vwap_distance_pct: float          # بُعد السعر عن VWAP%
    above_vwap: bool
    volume_rising: bool               # أحجام متصاعدة لا متناقصة
    notes: list[str] = field(default_factory=list)


@dataclass
class ReadinessResult:
    """ناتج ركيزة الجاهزية الفنية الكلاسيكية."""

    classic_score: float              # 0..100 (مقياس المستخدم؛ البوّابة ≥60)
    pillar_score: float               # 0..readiness_pillar_max (للدمج)
    trend: str                        # صاعد/هابط/عرضي (يومي)
    rsi: float
    macd_bull: bool
    divergence: str                   # صاعد/هابط/لا شيء
    above_ma50: bool
    above_ma200: bool
    golden_cross: bool
    adx: float = 0.0                  # قوة الاتجاه (>25 فعّال)
    stoch_rsi: float = 0.0            # ستوكاستك RSI (0..1)
    bb_pct_b: float = 0.5             # بولينجر %B
    candle: str = ""                  # أبرز نموذج شمعي يومي
    wave: str = ""                    # بنية الموجة (دافعة/تصحيحية)
    limited_history: bool = False     # سهم حديث الإدراج → تاريخ محدود
    notes: list[str] = field(default_factory=list)


@dataclass
class Catalyst:
    """ناتج فحص الخبر/المحفّز (إشارة تقوية لا بوّابة)."""

    has_news: bool
    headline: str = ""
    publisher: str = ""
    url: str = ""
    published_utc: str = ""
    description: str = ""
    category: str = ""               # تصنيف الخبر (أرباح/شراكة/...) للعرض
    age_hours: Optional[float] = None


@dataclass
class AnalystResult:
    """ناتج المحلّل الذكي (Claude) — تقييم المحفّز والأطروحة."""

    catalyst_type: str = ""
    direction: str = ""        # صعودي/هبوطي/محايد
    materiality: int = 0       # 1..10
    thesis: str = ""
    warning: str = ""

    @property
    def is_bearish(self) -> bool:
        return self.direction == "هبوطي" or bool(self.warning)


@dataclass
class DilutionResult:
    """ناتج رادار التخفيف (SEC EDGAR) — خطر طرح/تخفيف قادم يضرّ السهم.

    التخفيف (إصدار أسهم جديدة عبر S-1/S-3/424B/ATM) **يضرّ** السهم الصاعد
    تمامًا مثل الشورت: يزيد المعروض ويضغط السعر. نرصده ونحذّر منه، ولا
    نكافئ أبدًا. best-effort: تعذّر الجلب = None (لا يضرّ ولا ينفع).
    """

    risk: str = "لا"               # مرتفع/متوسط/منخفض/لا
    forms: list[str] = field(default_factory=list)   # أنواع النماذج الحديثة
    latest_form: str = ""          # أحدث نموذج تخفيفي
    latest_date: str = ""          # تاريخه (YYYY-MM-DD)
    note: str = ""                 # سطر تحذير بشري للبطاقة
    source: str = "SEC EDGAR"

    @property
    def is_active(self) -> bool:
        """خطر فعّال (طرح جارٍ/وشيك) يستحق خصم درجة."""
        return self.risk in ("مرتفع", "متوسط")


@dataclass
class RiskPlan:
    """الوقف والأهداف ومستويات الدعم ومنطقة الشراء."""

    stop_price: float
    stop_pct: float                   # مسافة الوقف عن الدخول%
    entry_ref: float                  # السعر المرجعي للدخول (آخر سعر)
    targets: list[float] = field(default_factory=list)
    stop_basis: str = ""              # "دعم 5د" أو "سقف نسبة"
    support_near: Optional[float] = None   # الدعم الأقرب تحت السعر (الدخول)
    support_deep: Optional[float] = None   # الدعم الأعمق
    buy_low: Optional[float] = None        # بداية منطقة الشراء
    buy_high: Optional[float] = None       # نهاية منطقة الشراء


@dataclass
class Candidate:
    """مرشّح سهم يمرّ في خط المعالجة. يتراكم عليه الحقول مرحلةً بمرحلة."""

    snapshot: SnapshotEntry
    session: Session = Session.CLOSED
    halt_state: HaltState = HaltState.NORMAL

    # تصنيف الورقة (للبوّابة)
    ticker_type: str = ""           # CS/ADRC/WARRANT/UNIT/...
    primary_exchange: str = ""      # XNAS/XNYS/OTC...
    is_champion: bool = False       # بطل فترة موروث (أولوية متابعة)

    # الفلوت
    float_shares: Optional[float] = None
    float_source: FloatSource = FloatSource.UNKNOWN
    market_cap: Optional[float] = None
    short_pct: Optional[float] = None      # نسبة الشورت من الفلوت% (best-effort)
    short_vol_pct: Optional[float] = None  # نسبة حجم الشورت اليومي% (best-effort)
    short_source: str = ""                  # مصدر بيانات الشورت

    # نتائج التحليل
    momentum: Optional[MomentumResult] = None
    readiness: Optional[ReadinessResult] = None
    catalyst: Optional[Catalyst] = None
    risk: Optional[RiskPlan] = None
    analyst: Optional[AnalystResult] = None   # تقييم Claude الذكي
    dilution: Optional["DilutionResult"] = None   # رادار التخفيف (SEC)

    # الدرجة
    final_score: float = 0.0
    rejected_reason: Optional[str] = None   # لو رُفض، سبب الرفض (للـ closed-loop)

    @property
    def ticker(self) -> str:
        return self.snapshot.ticker

    @property
    def is_rejected(self) -> bool:
        return self.rejected_reason is not None

    def reject(self, reason: str) -> "Candidate":
        self.rejected_reason = reason
        return self
