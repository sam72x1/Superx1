"""مُختبِر تاريخي (Backtester) — يعيد تشغيل البوت على الماضي لقياس الحافة.

الفكرة: بدل انتظار أسابيع من البيانات الحيّة، نعيد تمثيل أيام تداول ماضية عبر
**نفس** خط المعالجة (process_candidate) ونقيس النتائج (نجاح/خسارة/بلا حسم).

⚠️ **مبدأ حاسم — لا تسرّب مستقبل (no-lookahead):** عند تقييم مرشّح في لحظة T من
يوم ماضٍ، لا يرى الكود إلا بيانات **حتى T** (يومي قبل اليوم · شموع حتى T · أخبار
حتى T). النتيجة تُقاس من شموع **بعد T** فقط. أي خرق لهذا يعطي نتائج متفائلة كاذبة.

النطاق (v1): يختبر **الاستراتيجية الفنية الأساسية** (كشف + بوّابات + ركيزتان +
وقف/أهداف). يتخطّى طبقات Claude/SEC/الشورت (تُقيَّم حيًّا). تقريبات موثّقة:
- «أعلى 15» اللحظي (BUG-02، مُصلَح): المجمّع الأوّلي = أعلى backtest_top_n بقمة
  اليوم (تقريب رخيص، غير متسرّب — chg_high ≥ trigger شرط لازم)، ثم يُرتَّب داخله
  عند **كل شمعة 5د** بالتغيّر اللحظي (إغلاق ≤ asof) ويُقصّ إلى top_n_runners —
  مطابقة detect_runners الحي (لا ترتيب قمة-اليوم المتسرّب). التنبيه فقط لمن دخل
  أعلى-15 لحظتها؛ قياس الظل يبقى لكل المرفوضين. **قيد متبقٍّ:** المجمّع الأوّلي
  محدود بقمة-اليوم top_n، فسهم لم يدخل أعلى backtest_top_n بقمته قد يفوت (نادر).
- نوع الورقة/الفلوت من الحاضر (نادرًا يتغيّران) — best-effort.
- داخل الشمعة: لو لمست الهدف والوقف معًا نَعُدّها **خسارة** (تحفّظ ضد التفاؤل).

التشغيل: python -m runner_scanner.backtest --start 2025-01-02 --end 2025-01-31
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import detector, market_calendar
from .catalyst import NEGATIVE_NEWS
from .config import Config
from .massive_client import MassiveClient, MassiveError
from .models import Bar, Session, SnapshotEntry
from .pipeline import (
    _closed_daily, _daily_ma_and_peaks, daily_resistance_targets,
    process_candidate,
)
from .risk import build_risk_plan
from .sessions import ET, classify_session, is_opening_range
from .textutil import esc

logger = logging.getLogger(__name__)

_BAR5_MS = 5 * 60_000   # طول شمعة 5 دقائق بالمللي (لمدّ قصّ 1د حتى إغلاقها)


# ── محوّل «حتى لحظة T» (يمنع تسرّب المستقبل) ──────────────────────
class AsOfClient:
    """يغلّف MassiveClient ويقصّ كل البيانات حتى لحظة الباكتيست (asof_ms).

    يومي: قبل يوم الباكتيست حصرًا. شموع اليوم: حتى asof. أخبار: حتى asof.
    overview/float: من الحاضر (best-effort، نادرًا يتغيّران).
    """

    def __init__(self, base: MassiveClient, date_str: str, asof_ms: int,
                 bars_5min: list[Bar], bars_1min: list[Bar],
                 static_cache: dict, news_fetch_limit: int = 50):
        self._base = base
        self._date = date_str
        self._asof = asof_ms
        self._5 = bars_5min        # مقصوصة مسبقًا حتى asof
        self._1 = bars_1min
        self._static = static_cache
        self._news_limit = news_fetch_limit

    def _cached(self, key, fetch):
        if key not in self._static:
            self._static[key] = fetch()
        return self._static[key]

    def ticker_overview(self, ticker):
        return self._cached(f"ov:{ticker}", lambda: self._base.ticker_overview(ticker))

    def float_endpoint(self, ticker):
        return self._cached(f"fl:{ticker}", lambda: self._base.float_endpoint(ticker))

    def bars_5min(self, ticker, start, end):
        return list(self._5)

    def bars_1min(self, ticker, start, end):
        return list(self._1)

    def bars_daily(self, ticker, start, end):
        # يومي قبل يوم الباكتيست حصرًا (مكاش لكل سهم/تاريخ) ثم نُلحق شمعة اليوم
        # **الجزئية** المعاد بناؤها «حتى T» من شموع 5د المقصوصة — كي تطابق قائمة
        # اليومي شكلَ ما يراه الحي (يومي تاريخي + شمعة يوم جزئية زائلة).
        # BUG-04: بعد نقل الجاهزية إلى الأيام المغلقة، يستبعد _closed_daily في الـ
        # pipeline هذه الشمعة الجزئية من **كل** المستهلكين (جاهزية + متوسطات +
        # مقاومات) في الموضعين — فيبقى الباكتيست مطابقًا للحي.
        # بلا تسرّب: كل قيمها من شموع ≤ asof. عُمرها <20h (طابعها = أول 5د اليوم).
        bars = self._cached(
            f"d:{ticker}:{self._date}",
            lambda: self._base.bars_daily(ticker, start, self._date))
        out = [b for b in bars if _bar_date(b) < self._date]
        if self._5:
            five = self._5
            out.append(Bar(
                t_ms=five[0].t_ms, o=five[0].o,
                h=max(x.h for x in five), l=min(x.l for x in five),
                c=five[-1].c, v=sum(x.v for x in five), vw=0.0,
                n=sum(x.n for x in five)))
        return out

    # مدة نافذة الشمعة بالمللي ثانية لكل timespan (لفلترة اكتمال النافذة)
    _SPAN_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}

    def aggregates(self, ticker, multiplier, timespan, start, end, **kw):
        bars = self._cached(
            f"agg:{ticker}:{multiplier}:{timespan}:{self._date}",
            lambda: self._base.aggregates(ticker, multiplier, timespan,
                                          start, self._date, **kw))
        # t_ms هو **بداية** النافذة لا نهايتها: الشمعة الجارية (بدأت قبل asof
        # وتنتهي بعده) مجلوبة تاريخيًّا **مكتملة** — تمريرها كما هي يسرّب حتى
        # ~ساعة من المستقبل لإطار الساعة في الجاهزية. نُبقي فقط ما اكتملت
        # نافذته قبل asof، ونعيد بناء الشمعة الجارية **جزئيًّا** من شموع 5د
        # المقصوصة — كما يراها البوت الحي لحظتها تمامًا.
        span_ms = self._SPAN_MS.get(timespan, 3_600_000) * max(1, multiplier)
        out = [b for b in bars if b.t_ms + span_ms <= self._asof]
        ws = self._asof - (self._asof % span_ms)   # بداية النافذة الجارية
        part = [x for x in self._5 if x.t_ms >= ws]
        if part:
            out.append(Bar(
                t_ms=ws, o=part[0].o,
                h=max(x.h for x in part), l=min(x.l for x in part),
                c=part[-1].c, v=sum(x.v for x in part), vw=0.0,
                n=sum(x.n for x in part)))
        return out

    def latest_news(self, ticker, published_gte_utc, limit=5):
        # كاش يوم كامل ثم فلترة محلية بلا تسرّب (PERF-20): نجلب قائمة اليوم مرّة
        # واحدة (الشموع تُقيَّم تصاعديًّا فأول نداء = أبكر gte = أوسع نافذة سُفلى)،
        # ثم نصفّي لكل شمعة بـ gte الشمعة ≤ published ≤ asof.
        # ⚠️ حاسم: نُمرّر سقفًا علويًا = **نهاية يوم الباكتيست** للجلب المكاش. بدونه
        # كان الخادم (order=desc) يرجّع أحدث `limit` خبرًا منذ gte = أخبار قرب «الآن»
        # الحقيقي (كلها بعد asof)، فتحجبها الفلترة المحلية ويرجع None دائمًا — أخبار
        # الأيام التالية تزحم القائمة. السقف عند نهاية اليوم يغطّي كل asof اليوم
        # ويستبعد المستقبل؛ الفلترة `published ≤ asof` تحفظ لا-التسرّب لكل شمعة.
        lte = _iso_utc(datetime.fromtimestamp(self._asof / 1000, tz=timezone.utc))
        y, mo, dd = (int(x) for x in self._date.split("-"))
        day_end_lte = _iso_utc(datetime(y, mo, dd, tzinfo=ET) + timedelta(days=1))
        results = self._cached(
            f"news:{ticker}:{self._date}",
            lambda: self._base.news_results(
                ticker, published_gte_utc, limit=self._news_limit,
                published_lte_utc=day_end_lte))
        hits = [r for r in results
                if published_gte_utc <= (r.get("published_utc") or "") <= lte]
        return MassiveClient.catalyst_from_item(hits[0]) if hits else None


def _bar_date(b: Bar) -> str:
    return datetime.fromtimestamp(b.t_ms / 1000, tz=timezone.utc) \
        .astimezone(ET).strftime("%Y-%m-%d")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_snapshot(ticker: str, prev_close: float,
                    bars: list[Bar]) -> SnapshotEntry | None:
    """سنابشوت «حتى T» من شموع اليوم حتى لحظة الزناد."""
    if not bars or prev_close <= 0:
        return None
    last = bars[-1].c
    pv = sum(((b.h + b.l + b.c) / 3.0) * b.v for b in bars if b.v > 0)
    tv = sum(b.v for b in bars if b.v > 0)
    return SnapshotEntry(
        ticker=ticker, last_price=last, prev_close=prev_close,
        day_open=bars[0].o, day_high=max(b.h for b in bars),
        day_low=min(b.l for b in bars), day_volume=tv,
        day_vwap=(pv / tv if tv > 0 else 0.0),
        change_pct=(last - prev_close) / prev_close * 100.0,
        updated_ns=bars[-1].t_ms * 1_000_000)


# ── محاكاة النتيجة من شموع ما بعد الدخول ──────────────────────────
def simulate_outcome(entry: float, risk, post_bars: list[Bar],
                     asof_ms: int, window_min: float
                     ) -> tuple[str, float, float, int]:
    """يرجّع (result, max_gain%, max_draw%, target_level).
    - result: الخروج عند أول هدف1/وقف (تحفّظ: الهدف+الوقف بنفس الشمعة=خسارة).
    - target_level: أعلى هدف (1..3) لمسه السعر دون أن يُكسر الوقف قبله (0 لو
      لا شيء) — لقياس «هل يستحق الإمساك للأهداف الأعلى؟». سيناريو الإمساك
      يفترض وقفًا واقيًا؛ فبعد كسر الوقف يتجمّد العدّ ولا يُنسب هدف أعلى لاحق."""
    if not risk or not risk.targets or entry <= 0:
        return "timeout", 0.0, 0.0, 0
    targets = risk.targets
    t1 = targets[0]
    stop = risk.stop_price
    deadline = asof_ms + window_min * 60_000
    high = low = entry
    result = "timeout"
    decided = False
    tgt_level = 0
    frozen = False                      # بعد فوزٍ كُسر وقفه: جمّد القمة وعدّ الأهداف
    for b in post_bars:
        if b.t_ms > deadline:
            break
        # القاع يتحدّث دائمًا: max_draw مقياس excursion (أقصى سحب على النافذة)
        # ويبقى كذلك عمدًا — بخلاف القمة التي تُجمَّد عند الخروج (لا يُطبَّق حيًّا).
        low = min(low, b.l)
        # سيناريو الإمساك يفترض وقفًا واقيًا؛ أول شمعة تكسر الوقف بعد الفوز تُجمّد
        # القمة وعدّ الأهداف ابتداءً منها (لا هدف أعلى يُنسب بعد كسر الوقف).
        if decided and result == "win" and stop and b.l <= stop:
            frozen = True
        if not frozen:
            high = max(high, b.h)
        if not decided:
            if stop and b.l <= stop:    # تحفّظ: الوقف أولًا حتى لو لمس الهدف
                result = "loss"
                decided = True
                # BUG-16: جمّد القمة عند الخروج بخسارة أيضًا — وإلا نمت high على
                # كامل النافذة بعد كسر الوقف فنفخت «متوسط أقصى ربح» بالخاسرات
                # (ربح لم يُمسكه المتداول قط). كان التجميد للرابحين فقط.
                frozen = True
                continue                # خرجنا بخسارة → لا نحسب أهدافًا بعدها
            if b.h >= t1:
                result = "win"
                decided = True
        if result != "loss" and not frozen:   # أعلى هدف لُمس (سيناريو الإمساك)
            for i, tg in enumerate(targets, 1):
                if b.h >= tg and i > tgt_level:
                    tgt_level = i
    return (result, (high - entry) / entry * 100.0,
            (low - entry) / entry * 100.0, tgt_level)


def _eow_close_pct(entry: float, post_bars: list[Bar],
                   asof_ms: int, window_min: float) -> float:
    """MEAS-28: نسبة إغلاق **آخر شمعة داخل نافذة النتيجة** مقابل الدخول — قياس
    أصدق لصفقات الـtimeout من تجميدها على 0 (كأن الخروج على سعر الدخول تمامًا).
    حقل قياس فقط: لا يغيّر أي قرار/عتبة/نتيجة. لا-تسرّب: يقتصر على شموع ≤ نهاية
    النافذة. بلا شمعة داخل النافذة = 0 (لا بيانات → لا ادّعاء)."""
    if entry <= 0:
        return 0.0
    deadline = asof_ms + window_min * 60_000
    last_close = None
    for b in post_bars:
        if b.t_ms > deadline:
            break
        last_close = b.c
    if last_close is None:
        return 0.0
    return (last_close - entry) / entry * 100.0


def _loss_gap_fill_pct(entry: float, stop: float, post_bars: list[Bar],
                       asof_ms: int, window_min: float) -> float:
    """MEAS-29: نسبة الخسارة عند **تعبئة صادقة للفجوة** — لو فتحت شمعة كسر الوقف
    تحته (فجوة)، فالتعبئة الواقعية عند فتحها لا عند سعر الوقف (الحيّ لا يتعبّأ
    عند سعر مرّ منه قفزًا هبوطيًّا). يُستدعى لصفقة خاسرة فقط، ويطابق أوّل شمعة كسر
    يقرّرها simulate_outcome (أوّل b.l ≤ الوقف). قياس فقط — لا يغيّر النتيجة."""
    if entry <= 0 or not stop:
        return 0.0
    deadline = asof_ms + window_min * 60_000
    for b in post_bars:
        if b.t_ms > deadline:
            break
        if b.l <= stop:
            fill = min(b.o, stop)   # فجوة: الفتح تحت الوقف → التعبئة عند الفتح
            return (fill - entry) / entry * 100.0
    return 0.0


def partial_exit_realized(entry: float, risk, post_bars: list[Bar],
                          asof_ms: int, window_min: float,
                          fraction: float = 0.5) -> float:
    """ربح **الخروج الجزئي** المحاكى-المسار (للقياس فقط، لا يغيّر الفرز ولا الحيّ):
    بيع نسبة `fraction` عند الهدف1، رفع الوقف للتعادل (الدخول)، وإمساك الباقي حتى
    أول هدف أعلى (t2/t3) ربحًا أو الرجوع للتعادل. يحاكي **المسار فعليًّا** لا «هل
    لُمست القمة» — تحفّظ يمنع المبالغة: داخل الشمعة الواحدة التعادل يسبق الهدف الأعلى،
    والوقف قبل الهدف1 = خسارة كاملة (لا خروج جزئي). نفس قاعدة لا-تسرّب-المستقبل."""
    if not risk or not risk.targets or entry <= 0:
        return 0.0
    targets = risk.targets
    t1 = targets[0]
    higher = targets[1:]
    stop = risk.stop_price
    deadline = asof_ms + window_min * 60_000
    t1_pct = (t1 - entry) / entry * 100.0
    phase1 = True                     # قبل بلوغ الهدف1
    held_pct: float | None = None     # ربح النصف المُمسَك بعد الهدف1
    for b in post_bars:
        if b.t_ms > deadline:
            break
        if phase1:
            if stop and b.l <= stop:          # وقف قبل الهدف1 → خسارة كاملة
                return (stop - entry) / entry * 100.0
            if b.h >= t1:                      # بلغ الهدف1 → مرحلة الإمساك
                phase1 = False
            continue                           # شمعة الاختراق: لا نقيس أعلى داخلها (تحفّظ)
        # مرحلة 2: النصف مُمسَك، الوقف = التعادل (الدخول)
        if b.l <= entry:                       # رجع للتعادل → النصف الثاني 0%
            held_pct = 0.0
            break
        for tg in reversed(higher):            # أعلى هدف بلغه (الأبعد أولًا)
            if b.h >= tg:
                held_pct = (tg - entry) / entry * 100.0
                break
        if held_pct is not None:
            break
    if phase1:                                 # لم يبلغ الهدف1 ولا الوقف → ⏳=0
        return 0.0
    if held_pct is None:                       # بلغ الهدف1 لكن النصف لم يُحسم → تحفّظ 0
        held_pct = 0.0
    return fraction * t1_pct + (1.0 - fraction) * held_pct


def trailing_exit_realized(entry: float, risk, post_bars: list[Bar],
                           asof_ms: int, window_min: float,
                           trail_pct: float = 5.0) -> float:
    """ربح **الوقف المتعقّب** المحاكى-المسار (للقياس فقط، لا يغيّر الفرز ولا الحيّ):
    قبل الهدف1 الوقف الأصلي (كسره = خسارة كاملة)؛ بعد بلوغ الهدف1 يتبع الوقف
    القمة: `max(الدخول, القمة×(1−trail%))`. يقيس هل يلتقط فجوة «قمة الفائز مقابل
    خروج الهدف1». تحفّظ يمنع المبالغة: (1) شمعة الاختراق لا تُقاس قمتها؛ (2) داخل
    كل شمعة نفحص القاع ضد الوقف الحالي **قبل** رفع الوقف من قمتها (نفترض الهبوط
    أولًا)؛ (3) عند انتهاء النافذة والصفقة ممسوكة، المحقّق = مستوى الوقف المتعقّب
    المقفول (مضمون ≤ آخر سعر) لا آخر سعر. نفس قاعدة لا-تسرّب-المستقبل."""
    if not risk or not risk.targets or entry <= 0:
        return 0.0
    t1 = risk.targets[0]
    stop = risk.stop_price
    deadline = asof_ms + window_min * 60_000
    phase1 = True
    peak = t1                          # يُضبط عند بلوغ الهدف1 (لا نقيس شمعة الاختراق)
    for b in post_bars:
        if b.t_ms > deadline:
            break
        if phase1:
            if stop and b.l <= stop:               # وقف قبل الهدف1 → خسارة كاملة
                return (stop - entry) / entry * 100.0
            if b.h >= t1:                           # بلغ الهدف1 → مرحلة التعقّب
                phase1 = False
                peak = t1
            continue                                # شمعة الاختراق: لا نقيس قمتها (تحفّظ)
        trail = max(entry, peak * (1.0 - trail_pct / 100.0))
        if b.l <= trail:                            # لمس الوقف المتعقّب → خروج مقفول
            return (trail - entry) / entry * 100.0
        peak = max(peak, b.h)                        # ارفع القمة للشمعة التالية فقط
    if phase1:                                       # لم يبلغ الهدف1 ولا الوقف → ⏳=0
        return 0.0
    # النافذة انتهت والصفقة ممسوكة → الوقف المتعقّب الحالي (المقفول، ≤ آخر سعر)
    return (max(entry, peak * (1.0 - trail_pct / 100.0)) - entry) / entry * 100.0


def wide_target1_realized(entry: float, risk, post_bars: list[Bar],
                          asof_ms: int, window_min: float,
                          min_rr: float = 0.5) -> float:
    """ربح افتراضي لو **تخطّينا الهدف الأقرب التافه إلى أقرب مقاومة حقيقية**
    بعائد/مخاطرة ≥ min_rr (قياس فقط، لا حيّ). مطابق لقاعدة «الأهداف مقاومات
    حقيقية فقط»: الهدف الفعّال = أول هدف في القائمة (كلها مقاومات حقيقية)
    عائد/مخاطرته ≥ min_rr، فإن لم تكفِ أي مقاومة = الأبعد المتاح. **لا مستوى
    اصطناعي** (المضاعِف الثابت entry×(1+..) يخالف القاعدة فأُزيل). ثم نقيس:
    بلوغ الهدف الفعّال = ربح · كسر الوقف = خسارة كاملة · انتهاء النافذة = 0.
    للصفقات التي هدفها الأول أصلًا ≥ min_rr لا يتغيّر شيء. يكشف: هل تخطّي
    الهدف القريب لمقاومة أبعد حقيقية يرفع التوقّع أم يحوّل إصابات سهلة إلى
    انتهاء وقت؟ تحفّظ: الوقف أولًا داخل الشمعة. لا-تسرّب-المستقبل."""
    if not risk or not risk.targets or entry <= 0 or not risk.stop_pct:
        return 0.0
    stop = risk.stop_price
    stop_frac = risk.stop_pct / 100.0

    def _rr(t: float) -> float:
        return ((t - entry) / entry) / stop_frac if stop_frac else 0.0

    # أقرب مقاومة حقيقية بعائد/مخاطرة ≥ العتبة (تخطّي الأهداف الأقرب التافهة)
    wide = next((t for t in risk.targets if _rr(t) >= min_rr), risk.targets[-1])
    deadline = asof_ms + window_min * 60_000
    for b in post_bars:
        if b.t_ms > deadline:
            break
        if stop and b.l <= stop:          # الوقف أولًا (تحفّظ داخل الشمعة)
            return (stop - entry) / entry * 100.0
        if b.h >= wide:                    # بلغ الهدف الموسّع → ربح
            return (wide - entry) / entry * 100.0
    return 0.0                             # لم يبلغ الهدف الموسّع ولا الوقف → ⏳=0


def ratchet_exit_realized(entry: float, risk, post_bars: list[Bar],
                          asof_ms: int, window_min: float) -> float:
    """ربح **ترقية الوقف مع كل هدف** المحاكى-المسار (قياس فقط، لا حيّ — يقيس طلب
    المستخدم): الوقف يبدأ أصليًا؛ بعد بلوغ الهدف1 يرتفع للتعادل، وبعد كل هدف تالٍ
    للهدف السابق. الخروج عند لمس الوقف الحالي (المُرقّى) أو بلوغ الهدف الأخير ربحًا.
    يكشف: هل الترقية ترفع التوقّع (تحمي الربح وتحمل للأعلى) أم تخسر ربح الهدف1
    لصفقات لا تُكمل؟ تحفّظ: داخل كل شمعة نفحص القاع ضد الوقف الحالي **قبل** ترقيته
    بأهداف تلك الشمعة (الهبوط أولًا). انتهاء النافذة ممسوكًا = الوقف المُرقّى المقفول
    (≤ آخر سعر). نفس قاعدة لا-تسرّب-المستقبل."""
    if not risk or not risk.targets or entry <= 0:
        return 0.0
    targets = risk.targets
    deadline = asof_ms + window_min * 60_000
    level = 0                              # عدد الأهداف المُحقّقة → يحدّد الوقف الحالي
    cur_stop = risk.stop_price
    for b in post_bars:
        if b.t_ms > deadline:
            break
        if cur_stop and b.l <= cur_stop:   # الوقف الحالي (المُرقّى) أولًا (تحفّظ)
            return (cur_stop - entry) / entry * 100.0
        while level < len(targets) and b.h >= targets[level]:
            level += 1
            if level == len(targets):      # الهدف الأخير → خروج كامل ربحًا
                return (targets[-1] - entry) / entry * 100.0
            cur_stop = entry if level == 1 else targets[level - 2]
    if level == 0:                         # لا هدف ولا وقف → ⏳=0
        return 0.0
    return (cur_stop - entry) / entry * 100.0   # ممسوك حتى النافذة → المقفول


# ── تقويم أيام التداول ────────────────────────────────────────────
def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not market_calendar.is_holiday(d)


def trading_days(start: str, end: str) -> list[str]:
    d = date.fromisoformat(start)
    last = date.fromisoformat(end)
    out = []
    while d <= last:
        if _is_trading_day(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _prev_trading_day(day: str) -> str:
    d = date.fromisoformat(day) - timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def _prev_close_map(grouped: list[dict]) -> dict[str, float]:
    out = {}
    for r in grouped:
        t, c = r.get("T"), r.get("c")
        if t and c:
            out[t] = float(c)
    return out


def _day_candidates(cfg: Config, grouped: list[dict],
                    prev_close: dict[str, float]) -> list[tuple[str, float]]:
    """أعلى N صعودًا في قمة اليوم (proxy لـ top-gainers) — مرشّحو الباكتيست.

    N = backtest_top_n (منفصل عن top_n_runners الحي): الحي يغطّي 3 جلسات
    فيوسّع المجمّع هنا ليقارب اتحاد قادتها (القمة اليومية تشمل الجلسات الممتدة).
    """
    cands = []
    for r in grouped:
        t, h = r.get("T"), r.get("h")
        pc = prev_close.get(t)
        if not t or not pc or pc <= 0 or not h:
            continue
        chg_high = (float(h) - pc) / pc * 100.0
        # حدّ أدنى فقط (شرط لازم غير-تسرّب: سهم لم تبلغ قمته اليومية الحدّ لا يمكن
        # أن يعبره أيّ إغلاق شمعة). السقف (max_change_pct) وبوّابة السعر يُطبَّقان
        # **لحظيًّا** داخل _eval_candidate (detector + gates) على سعر اللحظة كالحي —
        # تطبيقهما هنا على قمة/إغلاق اليوم تسرّبٌ يستبعد أكبر الرابحين بأثر رجعي
        # (سهم نبّه عليه الحي عند +60% ثم تجاوزت قمته السقف لاحقًا، أو أغلق فوق $30).
        if chg_high < cfg.trigger_change_pct:
            continue
        if cfg.filter_derivatives and detector.looks_like_derivative(t):
            continue
        cands.append((t, chg_high))
    cands.sort(key=lambda x: -x[1])
    return cands[:cfg.backtest_top_n]


# ── قمع الترشيح (تشخيص: أين يموت المرشّحون؟) ──────────────────────
# يجيب عن سؤال «ليش العدد قليل؟»: كم اعتُبر، كم فُقد لنقص بيانات تاريخية،
# كم رُفض وبأي بوّابة. مب منطق تداول — تشخيص فقط (لا يغيّر النتائج).
def new_funnel() -> dict:
    return {"considered": 0, "no_5min": 0, "no_trigger": 0,
            "bad_snapshot": 0, "premarket_only": 0, "not_ranked": 0, "error": 0,
            # DIAG-26: كم مرشّحًا لمسته بوّابة الترتيب اللحظي (تأخّر تنبيه أو
            # حجب انتهى برفض) — مستقلّ عن التصنيف النهائي، يكشف أثرًا يخفيه القمع.
            "rank_gate_affected": 0,
            "rejected": 0, "alerts": 0,
            "reject_reasons": {}, "shadow": []}


def _news_label(cand) -> str:
    """تصنيف الخبر للباكتيست: «إيجابي» (مُكافأ) · «سلبي» (طرح/تخفيف) · «بلا».
    مهمّ: المكافأة +8 تُمنح للإيجابي فقط، فالفصل يقيس أثرها الحقيقي لا «أي خبر»."""
    cat = getattr(cand, "catalyst", None)
    if not (cat and cat.has_news):
        return "بلا"
    return "سلبي" if cat.category == NEGATIVE_NEWS else "إيجابي"


# DEBT-13: خريطة الكود الثابت → الخانة (الطريق المفضّل، بلا هشاشة نصّ عربي).
_CODE_BUCKET = {
    "rvol": "RVol", "float": "فلوت", "readiness": "جاهزية",
    "momentum": "زخم ضعيف", "score": "درجة", "vwap": "تحت VWAP",
    "parabolic": "بارابولِك", "late_wave": "حركة متقدّمة",
    "price_low": "سعر تحت الحد", "price_high": "سعر فوق الحد",
    "type": "نوع/بورصة", "exchange": "نوع/بورصة", "volume": "حجم",
    "min_profit": "ربح صغير", "t12": "توقّف", "halt": "توقّف",
    "bars": "نقص شموع", "dilution": "تخفيف SEC", "bearish": "محفّز هبوطي",
    "incomplete": "أخرى",
}


def _reject_bucket(reason: str, code: str = "") -> str:
    """يصنّف سبب الرفض لفئة موجزة (لتجميع «أكثر بوّابة ترفض»). يفضّل الكود الثابت
    (DEBT-13) إن وُجد، ويرتدّ لمطابقة النصّ العربي للصفوف القديمة (code فارغ)."""
    if code and code in _CODE_BUCKET:
        return _CODE_BUCKET[code]
    r = reason or ""
    # الترتيب مهمّ: المفاتيح الأدقّ أولًا (كلّ سبب سعر يحوي «سعر»، فنميّز «سنتات»
    # و«فوق نطاق» قبل «سعر» العام)؛ و«جاهزية» و«درجة» مفصولتان (كانتا مدموجتين).
    pairs = [("فلوت", "فلوت"), ("RVol", "RVol"), ("بارابولِك", "بارابولِك"),
             ("حركة متقدّمة", "حركة متقدّمة"),
             ("تحت VWAP", "تحت VWAP"),
             # DEBT-13: رفض الزخم («زخم {n} < …») كان يقع في «أخرى» — بوّابة كاملة
             # عمياء في القمع. الآن له خانته (النصّ الوحيد المبتدئ بـ«زخم»).
             ("زخم", "زخم ضعيف"),
             ("جاهزية", "جاهزية"), ("درجة", "درجة"),
             ("نوع الورقة", "نوع/بورصة"), ("بورصة", "نوع/بورصة"),
             ("سنتات", "سعر تحت الحد"), ("فوق نطاق", "سعر فوق الحد"),
             ("سعر", "سعر"), ("حجم", "حجم"), ("الشموع", "نقص شموع"),
             ("يستحق المخاطرة", "ربح صغير"),
             ("T12", "توقّف"), ("توقّف", "توقّف"),
             ("تخفيف", "تخفيف SEC"), ("هبوطي", "محفّز هبوطي")]
    for needle, label in pairs:
        if needle in r:
            return label
    return "أخرى"


def _eval_candidate(cfg: Config, base: MassiveClient, day: str,
                    static_cache: dict, pc: float, ticker: str,
                    full5: list[Bar] | None = None,
                    full1: list[Bar] | None = None,
                    allowed_asof: set | None = None) -> dict:
    """يقيّم مرشّحًا واحدًا (آمن للتشغيل المتوازي) → نتيجة موسومة.

    يحاكي المسح المتكرّر للبوت الحي: يفحص عند كل شمعة 5د يكون فيها رنرًا حتى
    **أول نجاح** (تنبيه واحد/سهم/يوم) — المرفوض يُعاد فحصه مع تراكم الحجم، لا
    يُسقَط للأبد عند أول عبور. لا يلمس حالة مشتركة (التجميع لاحقًا تسلسليًّا).

    full5/full1: شموع مجلوبة مسبقًا (لبناء ترتيب المجمّع اللحظي مرّة واحدة —
    BUG-02)؛ إن غابت تُجلب هنا. allowed_asof: مجموعة طوابع asof التي كان فيها
    السهم ضمن أعلى top_n_runners بالتغيّر اللحظي (مطابقة أعلى-15 الحي)؛ خارجها
    لا يُقيَّم (كالحي الذي لا يرى إلا أعلى-15 كل دورة). None = بلا بوّابة ترتيب.
    """
    # best-effort (القسم 3): فشل شبكي لسهم واحد يتخطّاه ولا يكسر الباكتيست.
    try:
        if full5 is None:
            full5 = base.bars_5min(ticker, day, day)
            full1 = base.bars_1min(ticker, day, day)
    except Exception as exc:  # noqa: BLE001
        logger.debug("باكتيست جلب شموع %s@%s فشل: %s", ticker, day, exc)
        return {"kind": "error"}
    if not full5:
        return {"kind": "no_5min"}
    # لحظات الرنر: إغلاق الشمعة ضمن [trigger, max_change_pct] — مطابقة detector
    # الحي الذي يشترط الحدّين معًا على التغيّر اللحظي (لا الحدّ الأدنى فقط).
    runner_idx = [i for i, b in enumerate(full5)
                  if pc > 0 and cfg.trigger_change_pct
                  <= (b.c - pc) / pc * 100.0 <= cfg.max_change_pct]
    if not runner_idx:
        return {"kind": "no_trigger"}
    step = max(1, cfg.backtest_scan_step_bars)
    evaluated = errored = False
    premarket_skipped = False   # تخطّى الحارس شمعةً واحدة على الأقل (رنر بريماركت)
    ranked_out = False          # كان رنرًا لكنه خارج أعلى-15 اللحظي في كل شمعه (BUG-02)
    last_reason = ""
    last_code = ""
    max_rvol = 0.0          # أقصى RVol بلغه السهم (لقياس الظل عند رفض RVol)
    last_asof = 0
    last_snap = None
    # كاش الأطر الثابتة (يومي/أسبوعي/شهري) لهذا السهم/اليوم — يُعاد استخدامه عبر
    # شموع المسح المتكرّر بدل إعادة الحساب الثقيل كل شمعة. بلا أثر على النتيجة.
    rcache: dict = {}
    for k in range(0, len(runner_idx), step):
        asof = full5[runner_idx[k]].t_ms
        asof_dt = datetime.fromtimestamp(
            asof / 1000, tz=timezone.utc).astimezone(ET)
        session = classify_session(cfg, asof_dt)
        # مطابقة الحي (run_cycle): تنبيهات البريماركت معطّلة → لا تقييم ولا
        # تنبيه في شموع البريماركت؛ يُعاد فحص السهم في الجلسات التالية كالحي.
        # بدون هذا الحارس يقيس الباكتيست بوتًا غير البوت المنشور.
        if session is Session.PREMARKET and not cfg.premarket_alerts_enabled:
            premarket_skipped = True
            continue
        up_to = [x for x in full5 if x.t_ms <= asof]
        snap = _build_snapshot(ticker, pc, up_to)
        if snap is None or not snap.is_valid:
            continue
        # asof = بداية شمعة الزناد 5د؛ القرار عند إغلاقها (سعر الدخول = إغلاقها).
        # نمدّ شموع 1د حتى نهاية نافذة الزناد (لا بدايتها) كي يُحسب VWAP الجلسة
        # ومشتقاته على لحظة القرار نفسها كالحي — ليس تسرّبًا (نفس نافذة السنابشوت).
        up_to_1 = [x for x in full1 if x.t_ms < asof + _BAR5_MS]
        client = AsOfClient(base, day, asof, up_to, up_to_1, static_cache,
                            news_fetch_limit=cfg.backtest_news_fetch_limit)
        try:
            cand = process_candidate(
                cfg, client, snap, halts=None,
                session=session, et_now=asof_dt,
                readiness_cache=rcache)
        except Exception as exc:  # noqa: BLE001 — سهم واحد لا يكسر اليوم
            logger.debug("باكتيست %s@%s فشل: %s", ticker, day, exc)
            errored = True
            continue
        evaluated = True
        if cand.momentum:
            max_rvol = max(max_rvol, cand.momentum.rvol)
        last_asof, last_snap = asof, snap
        if not cand.is_rejected:
            # BUG-02: نجح، لكن هل كان ضمن أعلى-15 اللحظي عند asof؟ إن لا، لم يكن
            # البوت الحي لينبّه عليه هذه الدورة (يرى أعلى-15 فقط) → لا تنبيه،
            # نواصل المسح (قد يدخل الأعلى في شمعة لاحقة). البوّابة على **التنبيه**
            # لا التقييم، كي يبقى قياس الظل للمرفوضين سليمًا.
            if allowed_asof is not None and asof not in allowed_asof:
                ranked_out = True
                continue
            # ✅ نجح ودخل أعلى-15 → تنبيه عند لحظتها (دخول = إغلاق الشمعة)
            post = [x for x in full5 if x.t_ms > asof]
            entry = snap.last_price
            result, gain, draw, tlevel = simulate_outcome(
                entry, cand.risk, post, asof, cfg.outcome_window_min)
            tgts = cand.risk.targets if cand.risk else []
            # ربح الهدف1% (إمكانية الربح) + الربح المحقّق الفعلي عند الخروج
            t1_pct = (tgts[0] - entry) / entry * 100.0 if tgts and entry else 0.0
            if result == "win":
                realized = t1_pct
            elif result == "loss" and cand.risk:
                realized = (cand.risk.stop_price - entry) / entry * 100.0
            else:
                realized = 0.0
            # قياس الخروج الجزئي (ظل — لا يغيّر القرار): مقارنة التوقّع لاحقًا
            realized_partial = partial_exit_realized(
                entry, cand.risk, post, asof, cfg.outcome_window_min,
                cfg.partial_exit_fraction)
            # قياس الوقف المتعقّب (ظل — لا يغيّر القرار): هل يلتقط فجوة القمة؟
            realized_trail = trailing_exit_realized(
                entry, cand.risk, post, asof, cfg.outcome_window_min,
                cfg.backtest_trail_pct)
            # قياس توسيع الهدف1 (ظل): لو رُفع الهدف القريب لعائد/مخاطرة أعلى
            realized_wide_t1 = wide_target1_realized(
                entry, cand.risk, post, asof, cfg.outcome_window_min,
                cfg.backtest_wide_t1_rr)
            # قياس ترقية الوقف مع كل هدف (ظل — طلب المستخدم): هل يرفع التوقّع؟
            realized_ratchet = ratchet_exit_realized(
                entry, cand.risk, post, asof, cfg.outcome_window_min)
            # MEAS-28: للـtimeout قياس أصدق = إغلاق نهاية النافذة (بدل تجميد 0)؛
            # لغير الـtimeout = المحقّق نفسه (خروج فعلي عند هدف/وقف). قياس فقط.
            realized_eow = (
                _eow_close_pct(entry, post, asof, cfg.outcome_window_min)
                if result == "timeout" else realized)
            # MEAS-29: للخاسرة قياس أصدق = تعبئة الفجوة (لو فتحت شمعة الكسر تحت
            # الوقف، الحيّ يتعبّأ عند فتحها لا عند الوقف)؛ لغير الخاسرة = المحقّق
            # نفسه. قياس فقط — لا يغيّر النتيجة ولا التوقّع المعتمد.
            realized_gap = (
                _loss_gap_fill_pct(entry, cand.risk.stop_price, post, asof,
                                   cfg.outcome_window_min)
                if result == "loss" and cand.risk else realized)
            # DIAG-26: ranked_out=True هنا يعني «تأخّر» — حُجب عن أعلى-15 في
            # شمعة أسبق ثم دخلها فنُبِّه (البوّابة اللحظية أثّرت على توقيته).
            return {"kind": "alert", "ranked_out": ranked_out, "trade": {
                "date": day, "ticker": ticker,
                "entry": round(entry, 4),
                "session": cand.session.value,
                # نسبة صعود اليوم عند الدخول (فرضية «الموجة الأخيرة الأضعف»:
                # هل الحركة المتقدّمة تخسر أكثر؟) + نافذة الافتتاح (فرضية «سهم
                # الماركت»: هل ضغط أول نص ساعة أقوى؟) — قياس لا تغيير فرز.
                "change_pct": round(snap.change_pct, 1),
                "late_wave_thr": cfg.late_wave_run_pct,   # عتبة «الحركة المتقدّمة»
                "opening_range": is_opening_range(cfg, asof_dt),
                "score": round(cand.final_score, 1),
                "readiness": round(cand.readiness.classic_score, 1)
                if cand.readiness else 0,
                "rvol": round(cand.momentum.rvol, 1) if cand.momentum else 0,
                # RVol اللحظي 5د: منخفض = زخم منطفئ (الحركة صارت، قياس فقط)
                "rvol_5min": round(cand.momentum.rvol_5min, 1)
                if cand.momentum else None,
                "news": _news_label(cand),
                # مؤشرات لكل صفقة — تكشف لاحقًا أيها يتنبّأ بالنجاح (نظام الفرز)
                "macd_bull": cand.readiness.macd_bull if cand.readiness else None,
                "golden_cross": cand.readiness.golden_cross if cand.readiness else None,
                "above_ma200": cand.readiness.above_ma200 if cand.readiness else None,
                "above_ma50": cand.readiness.above_ma50 if cand.readiness else None,
                "divergence": cand.readiness.divergence if cand.readiness else None,
                "trend": cand.readiness.trend if cand.readiness else None,
                "adx": round(cand.readiness.adx, 1) if cand.readiness else None,
                "above_vwap": cand.momentum.above_vwap if cand.momentum else None,
                "volume_rising": cand.momentum.volume_rising if cand.momentum else None,
                # الربحية والأهداف
                "target1_pct": round(t1_pct, 1),      # إمكانية الربح عند الهدف1
                # عائد/مخاطرة الهدف1 = ربح الهدف1 ÷ مسافة الوقف (قياس فقط)
                "t1_rr": round(t1_pct / cand.risk.stop_pct, 2)
                if cand.risk and cand.risk.stop_pct else None,
                "realized_pct": round(realized, 1),    # الربح/الخسارة المحقّق
                "realized_partial_pct": round(realized_partial, 1),  # لو خروج جزئي
                "realized_trail_pct": round(realized_trail, 1),  # لو وقف متعقّب
                "realized_wide_t1_pct": round(realized_wide_t1, 1),  # لو هدف1 أوسع
                "wide_t1_rr": cfg.backtest_wide_t1_rr,  # عتبة التوسيع المستخدمة
                "realized_ratchet_pct": round(realized_ratchet, 1),  # لو وقف مُرقّى
                # MEAS-28: محقّق نهاية النافذة (للـtimeout = إغلاق آخر شمعة؛ قياس)
                "realized_eow_pct": round(realized_eow, 1),
                # MEAS-29: محقّق بتعبئة صادقة للفجوة (للخاسرة = فتح شمعة الكسر لو
                # فتحت تحت الوقف؛ لغيرها = المحقّق نفسه؛ قياس فقط)
                "realized_gap_pct": round(realized_gap, 1),
                "target_hit": tlevel,                  # أعلى هدف لُمس (0..3)
                "result": result, "max_gain_pct": round(gain, 1),
                "max_draw_pct": round(draw, 1),
            }}
        last_reason = cand.rejected_reason or ""
        last_code = cand.reject_code or ""
        # بوّابات لا تتغيّر خلال اليوم (فلوت/نوع/بورصة) → لا فائدة من إعادة الفحص
        if _reject_bucket(last_reason, last_code) in ("فلوت", "نوع/بورصة"):
            break
    if not evaluated:
        if errored:
            return {"kind": "error"}
        # كل شموع رنره في البريماركت وتخطّاها الحارس (مطابقة الحي) — ليس سنابشوت
        # فاسدًا؛ السهم صالح لكنه خارج ساعات التنبيه. تصنيف منفصل كي لا يلوّث القمع.
        if premarket_skipped:
            return {"kind": "premarket_only"}
        return {"kind": "bad_snapshot"}
    # BUG-02: قُيّم لكنه لم يُنبَّه ولم يُرفض بأي بوّابة (last_reason فارغ) → نجح
    # لكنه بقي خارج أعلى-15 اللحظي في كل شمعة. تصنيف not_ranked منفصل كي لا يُعدّ
    # رفضًا فارغًا يلوّث «أخرى» في القمع. (evaluated=True دائمًا هنا، فالفحص بعده.)
    if ranked_out and not last_reason:
        return {"kind": "not_ranked"}
    # قياس الظل: لرفض RVol أو «سعر فوق الحد» نحسب نتيجة افتراضية (لو دخلنا) —
    # يكشف هل العتبة تحمي أم تفوّت فرصًا (قياس فقط). نفس مصادر الأهداف الحيّة.
    shadow = None
    _sh_bucket = _reject_bucket(last_reason, last_code)
    if (cfg.backtest_shadow_rvol and last_snap is not None
            and _sh_bucket in ("RVol", "سعر فوق الحد")):
      # الظل قياس best-effort (§3): فشل شبكة هنا لا يُسقط الرن كاملًا —
      # وقع فعلًا: 400 على مرفوض سقف السعر أسقط الباكتيست التلقائي كله.
      try:
        closed = [x for x in full5 if x.t_ms <= last_asof]
        closed5 = closed[:-1] if len(closed) > 1 else closed
        post = [x for x in full5 if x.t_ms > last_asof]
        # مقاومات يومية مطابقة للخط الفعلي (وإلا أهداف الظل تختلف فيَختلّ حكم
        # «العتبة مثبتة/تستحق الدراسة» المبنيّ عليها). نفس _closed_daily الحي.
        last_dt = datetime.fromtimestamp(
            last_asof / 1000, tz=timezone.utc).astimezone(ET)
        # بداية فعلية لليومي (نافذة الحي 400 يوم): مرفوض «سعر فوق الحد» يُرفض
        # قبل جلب الشموع فالكاش بارد، وبداية فارغة ترجع 400 من الـAPI.
        year_ago = (datetime.strptime(day, "%Y-%m-%d")
                    - timedelta(days=400)).strftime("%Y-%m-%d")
        daily = AsOfClient(base, day, last_asof, closed, [],
                           static_cache).bars_daily(ticker, year_ago, day)
        cdaily = _closed_daily(daily, last_dt)
        daily_res = daily_resistance_targets(cdaily, last_snap.last_price)
        # مطابقة الخط الحي: نفس مصادر الأهداف (متوسطات ٢٠/٥٠ + قمم تأرجح)
        ma_levels, daily_peaks = _daily_ma_and_peaks(cdaily)
        risk = build_risk_plan(cfg, last_snap.last_price, closed5,
                               daily_resistances=daily_res,
                               ma_levels=ma_levels, daily_peaks=daily_peaks)
        sres, _, _, _ = simulate_outcome(last_snap.last_price, risk, post,
                                         last_asof, cfg.outcome_window_min)
        kind = "price_cap" if _sh_bucket == "سعر فوق الحد" else "rvol"
        shadow = {"kind": kind, "max_rvol": round(max_rvol, 1), "result": sres}
      except MassiveError as exc:
        # تعذّر ≠ صفر: نفقد قياس ظل سهمٍ واحد ويكمل الباكتيست
        logger.debug("تعذّر قياس ظل %s: %s", ticker, exc)
        shadow = None
    # رُفض في كل الدورات → سببه من آخر محاولة (أكثر تمثيلًا لقيد نهاية اليوم)
    # DIAG-26: ranked_out=True هنا يعني «حجب» — نجح ضمنيًّا عند شمعة وخرج من
    # أعلى-15، ثم واصل صعوده فرُفض ببوّابة لاحقة (فامتصّه reject_reasons لا
    # not_ranked). العلم يكشف أثر البوّابة الذي كان مخفيًّا في القمع.
    return {"kind": "rejected", "reason": last_reason,
            "reason_code": last_code, "shadow": shadow, "ranked_out": ranked_out}


def _asof_rank_gate(cfg: Config, cands: list[tuple[str, float]],
                    bars5: dict[str, list[Bar] | None],
                    prev_close: dict[str, float]) -> dict[str, set]:
    """ترتيب المجمّع اللحظي (BUG-02) — يزيل تسرّب ترتيب قمة-اليوم.

    عند كل طابع شمعة 5د، يرتّب مرشّحي المجمّع بالتغيّر اللحظي (إغلاق آخر شمعة
    ≤ الطابع) ويسجّل أعلى top_n_runners — **مطابقة detect_runners الحي** (نفس
    شرط [trigger, max] ونطاق السعر، ثم أعلى-15). يرجّع {ticker: set(طوابع كان
    فيها ضمن الأعلى)}. الأساس (قمة-اليوم top_n) يبقى مجمّعًا أوّليًّا رخيصًا غير
    متسرّب؛ الترتيب/القطع اللحظي هنا هو ما يطابق الحي."""
    allowed: dict[str, set] = {t: set() for t, _ in cands}
    times = sorted({b.t_ms for t, _ in cands for b in (bars5.get(t) or [])})
    # مؤشّر تصاعدي لكل سهم على آخر إغلاق ≤ الطابع (الطوابع مرتّبة) — كفاءة.
    ptr = {t: 0 for t, _ in cands}
    last_close: dict[str, float | None] = {t: None for t, _ in cands}
    for T in times:
        ranks = []
        for t, _ in cands:
            b5 = bars5.get(t) or []
            i = ptr[t]
            while i < len(b5) and b5[i].t_ms <= T:
                last_close[t] = b5[i].c
                i += 1
            ptr[t] = i
            pc = prev_close.get(t)
            c = last_close[t]
            if not (pc and pc > 0 and c is not None):
                continue
            chg = (c - pc) / pc * 100.0
            # أهلية أعلى-15 = نفس فلتر detect_runners: رنر ضمن النطاق وسعر مقبول.
            if (cfg.trigger_change_pct <= chg <= cfg.max_change_pct
                    and cfg.price_min <= c <= cfg.price_max):
                ranks.append((t, chg))
        ranks.sort(key=lambda x: -x[1])
        # مطابقة الحي (main.py): top_n_runners ≤ 0 = بلا سقف (كل الرنرات).
        cut = ranks if cfg.top_n_runners <= 0 else ranks[:cfg.top_n_runners]
        for t, _ in cut:
            allowed[t].add(T)
    return allowed


def simulate_day(cfg: Config, base: MassiveClient, day: str,
                 static_cache: dict, funnel: dict | None = None) -> list[dict]:
    """يحاكي يوم تداول كاملًا → قائمة صفقات. يعالج المرشّحين **متوازيًا**
    (backtest_workers) لأن كل سهم نداءات شبكية مستقلّة — يسرّع الشهر كثيرًا.
    التجميع تسلسليّ بعد الانتهاء (لا تسابق على القمع/الصفقات)."""
    prev = _prev_trading_day(day)
    grouped = base.grouped_daily(day)
    if not grouped:
        return []
    prev_close = _prev_close_map(base.grouped_daily(prev))
    cands = _day_candidates(cfg, grouped, prev_close)
    if funnel is not None:
        funnel["considered"] += len(cands)
    if not cands:
        return []

    workers = max(1, cfg.backtest_workers)
    _parallel = workers > 1 and len(cands) > 1

    # ── BUG-02: نجلب شموع كل المجمّع مرّة (متوازيًا) لبناء ترتيب أعلى-15 اللحظي
    # (المجمّع الحي عند كل شمعة) بدل ترتيب قمة-اليوم المتسرّب. ──
    def _fetch(tc):
        t = tc[0]
        try:
            return t, base.bars_5min(t, day, day), base.bars_1min(t, day, day)
        except Exception as exc:  # noqa: BLE001 — سهم واحد لا يكسر اليوم
            logger.debug("باكتيست جلب شموع %s@%s فشل: %s", t, day, exc)
            return t, None, None

    if _parallel:
        with ThreadPoolExecutor(max_workers=min(workers, len(cands))) as ex:
            fetched = list(ex.map(_fetch, cands))
    else:
        fetched = [_fetch(tc) for tc in cands]
    bars5 = {t: b5 for t, b5, _ in fetched}
    bars1 = {t: b1 for t, _, b1 in fetched}
    allowed = _asof_rank_gate(cfg, cands, bars5, prev_close)

    def _run(tc):
        t = tc[0]
        b5 = bars5.get(t)
        if b5 is None:
            return {"kind": "error"}   # فشل الجلب المسبق (best-effort §3)
        return _eval_candidate(cfg, base, day, static_cache, prev_close[t], t,
                               full5=b5, full1=bars1.get(t) or [],
                               allowed_asof=allowed.get(t, set()))

    if _parallel:
        with ThreadPoolExecutor(max_workers=min(workers, len(cands))) as ex:
            results = list(ex.map(_run, cands))
    else:
        results = [_run(tc) for tc in cands]

    # ── تجميع تسلسليّ آمن (بلا تسابق) ──
    trades: list[dict] = []
    for r in results:
        kind = r["kind"]
        # DIAG-26: أثر بوّابة الترتيب مستقلّ عن التصنيف النهائي — نعدّه أوّلًا
        # (تنبيه متأخّر، أو رفض لاحق بعد حجب) كي لا يبقى مخفيًّا في القمع.
        if funnel is not None and r.get("ranked_out"):
            funnel["rank_gate_affected"] += 1
        if kind == "alert":
            trades.append(r["trade"])
            if funnel is not None:
                funnel["alerts"] += 1
        elif funnel is not None:
            if kind == "rejected":
                funnel["rejected"] += 1
                bucket = _reject_bucket(r.get("reason", ""), r.get("reason_code", ""))
                funnel["reject_reasons"][bucket] = \
                    funnel["reject_reasons"].get(bucket, 0) + 1
                if r.get("shadow"):
                    funnel["shadow"].append(r["shadow"])
            else:
                # no_5min · no_trigger · bad_snapshot · not_ranked · premarket_only · error
                funnel[kind] = funnel.get(kind, 0) + 1
    return trades


# ── النتيجة المجمّعة + التقرير ────────────────────────────────────
@dataclass
class BacktestResult:
    start: str
    end: str
    days: int = 0
    trades: list[dict] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)
    # بصمة الإعدادات الفعّالة وقت الرن (ت1) — None في الرنّات القديمة/الدمج
    # المختلط. تميّز «أي نشر/عتبات أنتجت هذي الأرقام» بدل الاستنتاج الجنائي.
    run_config: dict | None = None

    def stats(self) -> dict:
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t["result"] == "win")
        losses = sum(1 for t in self.trades if t["result"] == "loss")
        timeouts = sum(1 for t in self.trades if t["result"] == "timeout")
        decisive = wins + losses
        return {
            "alerts": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "win_rate": (wins / decisive * 100.0) if decisive else None,
            # نسبة متحفّظة: تعدّ ⏳ غير-فوز (الحدّ الأدنى الواقعي) — لمنع التفاؤل
            "win_rate_conservative": (wins / n * 100.0) if n else None,
            "avg_gain": (sum(t["max_gain_pct"] for t in self.trades) / n) if n else 0.0,
            "per_day": n / self.days if self.days else 0.0,
        }


# ── الحفظ الدائم للتشغيلات (مصدر الدمج عبر الأشهر) ────────────────
def _save_dir(cfg: Config) -> str:
    """مجلد حفظ نتائج الباكتيست: المُعدّ صراحةً أو <مجلد القاعدة>/backtests."""
    if cfg.backtest_save_dir:
        return cfg.backtest_save_dir
    return os.path.join(os.path.dirname(cfg.db_path) or ".", "backtests")


def _run_config(cfg: Config) -> dict:
    """بصمة الإعدادات المؤثّرة على نتائج الرن (ت1) — للتمييز بين الرنّات.
    نقرأ الموجود في Config فقط (لا حقول جديدة). أي عتبة تغيّر ماذا يُقبل
    أو كيف يُقاس تدخل هنا كي يكشف الدمج المختلط اختلافها."""
    return {
        "code_version": cfg.code_version or "—",
        "tech_readiness_min": cfg.tech_readiness_min,
        "entry_change_max_pct": cfg.entry_change_max_pct,
        "target1_min_rr": cfg.target1_min_rr,
        "late_wave_run_pct": cfg.late_wave_run_pct,
        "stop_fixed_pct": cfg.stop_fixed_pct,
        "price_min": cfg.price_min,
        "price_max": cfg.price_max,
        "backtest_wide_t1_rr": cfg.backtest_wide_t1_rr,
        # BUG-12: عتبات إضافية تغيّر النتائج — بدونها يُدمج رنّان بإعدادات
        # مختلفة (RVOL_MIN 5 مقابل 4) بلا تحذير «إعدادات مختلفة». عُدّدت صراحةً
        # (لا asdict) كي لا يولّد تغيير رمز تيليجرام تحذيرًا كاذبًا.
        "rvol_min": cfg.rvol_min,
        "float_max": cfg.float_max,
        "parabolic_day_change_pct": cfg.parabolic_day_change_pct,
        "alert_score_min": cfg.alert_score_min,
        "trigger_change_pct": cfg.trigger_change_pct,
        "max_change_pct": cfg.max_change_pct,
        "min_target_profit_pct": cfg.min_target_profit_pct,
        "catalyst_score_bonus": cfg.catalyst_score_bonus,
        "outcome_window_min": cfg.outcome_window_min,
        "backtest_top_n": cfg.backtest_top_n,
        "backtest_scan_step_bars": cfg.backtest_scan_step_bars,
        "premarket_alerts_enabled": cfg.premarket_alerts_enabled,
    }


def _fingerprint_line(run_config: dict | None) -> str:
    """سطر بصمة الإعدادات في رأس التقرير. غياب البصمة = رن قديم/دمج مختلط
    فنصرّح بذلك بدل ادّعاء إعدادات لا نعرفها (صادق مع الواقع)."""
    if not run_config:
        return "⚙️ إعدادات الرن: غير موقّعة (رن قديم)"
    ver = esc(str(run_config.get("code_version") or "—"))
    ec = _num2(run_config.get("entry_change_max_pct"))
    move = f"حركة≤{ec:g}%" if ec and ec > 0 else "حركة: بلا سقف"
    rd = _num2(run_config.get("tech_readiness_min"))
    t1 = _num2(run_config.get("target1_min_rr"))
    lw = _num2(run_config.get("late_wave_run_pct"))
    return (f"⚙️ نسخة {ver} · جاهزية≥{rd:g} · {move} · "
            f"تخطّي هدف1 R/R≥{t1:g} · موجة متأخرة {lw:g}%")


def _num2(v) -> float:
    """قيمة عددية آمنة للعرض (0.0 لو غابت/تلفت) — البصمة قد تنقص حقلًا."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def save_run(cfg: Config, res: "BacktestResult", report_text: str,
             now_utc: datetime | None = None) -> str | None:
    """يحفظ تشغيل باكتيست كامل على القرص (best-effort §3): JSON (مصدر الدمج) +
    نسخة نص التقرير. يرجّع مسار JSON للإرسال، أو None عند الفشل (لا يكسر شيئًا).
    JSON يحفظ الأنواع بدقّة (أعداد/منطقيات) فيُدمَج لاحقًا بلا فقد."""
    try:
        d = _save_dir(cfg)
        os.makedirs(d, exist_ok=True)
        stem = f"bt_{res.start}_{res.end}"
        created = (now_utc or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        payload = {"start": res.start, "end": res.end, "days": res.days,
                   "funnel": res.funnel, "trades": res.trades,
                   "created_utc": created, "run_config": res.run_config}
        json_path = os.path.join(d, stem + ".json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        with open(os.path.join(d, stem + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(report_text)
        return json_path
    except OSError as exc:
        logger.warning("تعذّر حفظ نتائج الباكتيست: %s", exc)
        return None


def _num(v) -> bool:
    """قيمة رقمية حقيقية (لا منطقية) — لتجاهل bool في جمع أعداد القمع."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _merge_funnel(dst: dict, src: dict) -> None:
    """يدمج قمع تشغيل (src) في المجمّع (dst): الأعداد بالجمع، reject_reasons
    بجمع كل بوّابة، shadow بالإلحاق. **دفاعي**: أي قيمة فرعية مشوّهة (نوع غير
    متوقّع من انجراف مخطّط §7) تُتجاهَل بأمان بلا رفع استثناء يُسقط الدمج كلّه."""
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if k == "reject_reasons":
            if isinstance(v, dict):
                for rk, rv in v.items():
                    if _num(rv):
                        dst["reject_reasons"][rk] = \
                            dst["reject_reasons"].get(rk, 0) + rv
        elif k == "shadow":
            if isinstance(v, list):
                dst["shadow"].extend(v)
        elif _num(v):
            dst[k] = dst.get(k, 0) + v


def merge_saved_runs(cfg: Config) -> tuple["BacktestResult | None", list[str]]:
    """يقرأ كل bt_*.json المحفوظة (م1) ويدمجها في نتيجة واحدة + ملاحظات عربية.
    best-effort: ملف تالف يُتخطّى مع تحذير. لتجنّب العدّ المزدوج، النطاقات
    المتداخلة زمنيًا تُتخطّى (نُبقي الأبكر ونتخطّى المتقاطع معه)."""
    notes: list[str] = []
    paths = sorted(glob.glob(os.path.join(_save_dir(cfg), "bt_*.json")))
    runs: list[tuple[dict, str]] = []
    for p in paths:
        name = os.path.basename(p)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            # تحقّق بنيوي كامل (لا الوجود فقط): يمنع ملفًا منجرف-المخطّط (§7) من
            # كسر الدمج كلّه لاحقًا خارج هذا try — يُتخطّى هنا كباقي التالفة.
            if not (data.get("start") and data.get("end")
                    and isinstance(data.get("trades"), list)
                    and isinstance(data.get("funnel", {}), dict)
                    and _num(data.get("days", 0))):
                raise ValueError("بنية مشوّهة")
            runs.append((data, name))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # §5: الاسم/الاستثناء نصّان خارجيان → هروب HTML قبل دمجهما في رسالة
            notes.append(f"⚠️ تُخطّي ملف تالف {esc(name)}: {esc(exc)}")
    if not runs:
        return None, notes
    runs.sort(key=lambda r: (r[0]["start"], r[0]["end"]))
    merged = BacktestResult(start=runs[0][0]["start"], end=runs[0][0]["end"])
    merged.funnel = new_funnel()
    last_end: str | None = None
    used = 0
    # بصمات الرنّات المدموجة فعلًا (ت1): لكشف خلط إعدادات مختلفة. الملف القديم
    # بلا بصمة = بصمة مستقلّة «غير موقّع» (فخلطه برن موقّع تحذير أيضًا).
    fingerprints: list = []
    for data, name in runs:
        if last_end is not None and data["start"] <= last_end:
            notes.append(f"⚠️ تخطّي المتداخل {esc(name)} (يتقاطع مع نطاق سابق)")
            continue
        used += 1
        merged.trades.extend(data.get("trades") or [])
        merged.days += int(data.get("days") or 0)   # عدد رقمي (تحقّق مسبقًا)
        _merge_funnel(merged.funnel, data.get("funnel") or {})
        if data["end"] > merged.end:
            merged.end = data["end"]
        last_end = data["end"]
        fingerprints.append(data.get("run_config"))
    # بصمة النتيجة المدموجة: تُملأ فقط لو كل الرنّات متطابقة الإعدادات، وإلا
    # None (فيظهر «غير موقّعة» — لا ندّعي إعدادات موحّدة ونحن خلطنا).
    uniq = {json.dumps(fp, sort_keys=True, ensure_ascii=False)
            for fp in fingerprints}
    if len(uniq) == 1 and fingerprints[0] is not None:
        merged.run_config = fingerprints[0]
    else:
        merged.run_config = None
        if len(uniq) > 1:
            notes.append(f"⚠️ الرنّات المدموجة بإعدادات مختلفة ({len(uniq)} بصمة) "
                         "— قارن الشرائح بحذر")
    notes.insert(0, f"🧩 دمج {used} تشغيلات باكتيست محفوظة "
                    f"({esc(merged.start)} → {esc(merged.end)})")
    return merged, notes


def format_merged_report(cfg: Config) -> str:
    """تقرير مجمّع من التشغيلات المحفوظة (لأمر «/backtest دمج»). خفيف، بلا شبكة."""
    merged, notes = merge_saved_runs(cfg)
    if merged is None:
        body = ("لا توجد تشغيلات باكتيست محفوظة بعد. شغّل «/backtest كامل» أو "
                "شهرًا محددًا («/backtest 4») أولًا — تُحفَظ تلقائيًا للدمج.")
        return "\n".join(notes + [body]) if notes else body
    return "\n".join(notes) + "\n\n" + format_report(merged)


def _bucket_stats(trades: list[dict], keyfn) -> list[tuple]:
    groups: dict = {}
    for t in trades:
        k = keyfn(t)
        if k is not None:
            groups.setdefault(k, []).append(t)
    out = []
    for k, g in groups.items():
        dec = sum(1 for t in g if t["result"] in ("win", "loss"))
        wr = sum(1 for t in g if t["result"] == "win") / dec * 100.0 if dec else None
        out.append((k, len(g), wr))
    return sorted(out, key=lambda x: -(x[2] or -1))


def format_report(res: BacktestResult) -> str:
    s = res.stats()
    wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"
    wrc = f"{s['win_rate_conservative']:.0f}%" \
        if s["win_rate_conservative"] is not None else "—"
    lines = [
        f"📈 باكتيست {res.start} → {res.end} ({res.days} يوم تداول)",
        f"تنبيهات مُحاكاة: {s['alerts']} (~{s['per_day']:.1f}/يوم)",
        _fingerprint_line(res.run_config),
        f"النجاح: {wr} ({s['wins']}✅/{s['losses']}🛑/{s['timeouts']}⏳) · "
        f"متوسط أقصى ربح {s['avg_gain']:+.0f}%",
        f"النجاح المتحفّظ (⏳=غير فوز): {wrc} ← الحدّ الأدنى الواقعي",
    ]
    # ── 💰 الربحية والأهداف (تفصيل مهم) ──
    if res.trades:
        n = len(res.trades)
        wins = [t for t in res.trades if t["result"] == "win"]
        losses = [t for t in res.trades if t["result"] == "loss"]
        tos = [t for t in res.trades if t["result"] == "timeout"]

        def _avg(g, key):
            return sum(t.get(key, 0) or 0 for t in g) / len(g) if g else 0.0

        def _pct(c):
            return c / n * 100.0 if n else 0.0
        avg_real = _avg(res.trades, "realized_pct")       # التوقّع المحقّق/صفقة
        avg_t1pot = _avg(res.trades, "target1_pct")       # متوسط إمكانية الربح
        avg_win = _avg(wins, "realized_pct")
        avg_loss = _avg(losses, "realized_pct")
        rr = (avg_win / abs(avg_loss)) if avg_loss else None
        r_t2 = _pct(sum(1 for t in res.trades if (t.get("target_hit") or 0) >= 2))
        r_t3 = _pct(sum(1 for t in res.trades if (t.get("target_hit") or 0) >= 3))
        low10 = _pct(sum(1 for t in res.trades if (t.get("target1_pct") or 0) < 10))
        avg_peak_win = _avg(wins, "max_gain_pct")
        lines.append("\n💰 الربحية والأهداف:")
        lines.append(f"  • التوقّع/صفقة (محقّق، ⏳=0): {avg_real:+.1f}% · "
                     f"متوسط إمكانية الربح (هدف1): +{avg_t1pot:.1f}%")
        lines.append(f"  • متوسط الفوز: {avg_win:+.1f}% · متوسط الخسارة: "
                     f"{avg_loss:+.1f}%" + (f" · عائد/مخاطرة {rr:.1f}:1" if rr else ""))
        lines.append(f"  • تحقيق الأهداف: هدف1 {_pct(len(wins)):.0f}% · "
                     f"هدف2 {r_t2:.0f}% · هدف3 {r_t3:.0f}% (من كل التنبيهات)")
        lines.append(f"  • كسر الوقف: {_pct(len(losses)):.0f}% · "
                     f"بلا حسم ⏳: {_pct(len(tos)):.0f}%")
        lines.append(f"  • متوسط قمة الفائز: +{avg_peak_win:.1f}% (مقابل خروج الهدف1)")
        lines.append(f"  • ℹ️ هدفها الأول (الأقرب) أقل من 10%: {low10:.0f}% "
                     "— للعلم فقط؛ الرفض يكون على سقف الأهداف لا الأقرب")
        # ── 🔀 قياس بدائل الخروج (ظل — لا يُطبَّق): هل يرفع التوقّع؟ ──
        # على **نفس المجتمع**: الصفقات التي تحمل كل قياسات الظل الثلاثة، ونحسب
        # الخط الأساسي (حالي) والبدائل عليها معًا — وإلا دمج تشغيلات محفوظة قديمة
        # تفتقد حقلًا يقارن الخط الأساسي بمجتمع أوسع من البدائل (مقارنة زائفة).
        alt = [t for t in res.trades
               if t.get("realized_partial_pct") is not None
               and t.get("realized_trail_pct") is not None
               and t.get("realized_ratchet_pct") is not None]
        if alt:
            base = _avg(alt, "realized_pct")

            def _tag(d):
                return ("🟢 أفضل" if d > 0.05 else
                        "🔴 أسوأ" if d < -0.05 else "≈ مماثل")

            def _seg(name, key):
                v = _avg(alt, key)
                d = v - base
                return f" ← {name} {v:+.1f}% ({_tag(d)} {d:+.1f}%)"
            lines.append("\n🔀 قياس بدائل الخروج (ظل — لا يُطبَّق على الحيّ):")
            lines.append(f"  • التوقّع/صفقة: حالي {base:+.1f}%"
                         + _seg("جزئي", "realized_partial_pct")
                         + _seg("متعقّب", "realized_trail_pct")
                         + _seg("مُرقّى", "realized_ratchet_pct"))
            lines.append("  <i>↳ الجزئي: نصف عند هدف1 + وقف تعادل. المتعقّب: وقف "
                         "يتبع القمة. المُرقّى: الوقف يرتفع مع كل هدف (تعادل بعد "
                         "هدف1 ثم الهدف السابق). كلها محاكى-المسار متحفّظ.</i>")
            # MEAS-28: صدق توقّع الـtimeout — تُحسب اليوم ⏳ على 0 (كأن خروج على
            # الدخول)، وهذا سطر يقيس ما لو خرجنا على إغلاق نهاية النافذة فعلًا.
            if all(t.get("realized_eow_pct") is not None for t in alt):
                eow = _avg(alt, "realized_eow_pct")
                d = eow - base
                lines.append(
                    f"  • لو خروج نهاية النافذة (بدل تجميد ⏳ على 0): "
                    f"{eow:+.1f}% ({_tag(d)} {d:+.1f}%)")
                lines.append("  <i>↳ يقيس ما تركته صفقات ⏳ على الطاولة: إغلاق "
                             "آخر شمعة داخل نافذة النتيجة بدل افتراض الخروج على "
                             "سعر الدخول. قياس فقط — لا يغيّر النتيجة الرئيسة.</i>")
            # MEAS-29: صدق فجوة الوقف — الباكتيست يحجز كل خاسرة على الوقف بالضبط
            # حتى لو فتحت شمعة الكسر تحته (فجوة)، والحيّ لا يتعبّأ عند سعر قفز فوقه
            # هبوطًا. هذا سطر يقيس التوقّع لو عُبِّئت الخسارة على فتح شمعة الكسر.
            if all(t.get("realized_gap_pct") is not None for t in alt):
                gap = _avg(alt, "realized_gap_pct")
                d = gap - base
                lines.append(
                    f"  • لو تعبئة صادقة لفجوات الوقف: "
                    f"{gap:+.1f}% ({_tag(d)} {d:+.1f}%)")
                lines.append("  <i>↳ يكشف تفاؤل ذيل الخسارة: الخاسرة التي تفتح "
                             "شمعة كسرها تحت الوقف تتعبّأ عند فتحها (أسوأ) لا عند "
                             "الوقف. قياس فقط — لا يغيّر النتيجة الرئيسة.</i>")
    if res.trades:
        def b(title, kf):
            rows = [r for r in _bucket_stats(res.trades, kf) if r[1] >= 3]
            if not rows:
                return
            lines.append(f"\n{title}:")
            for k, cnt, w in rows:
                lines.append(f"  • {k}: {w:.0f}% نجاح ({cnt})" if w is not None
                             else f"  • {k}: — ({cnt})")
        def _band(v):
            return None if v is None else (
                "60-70" if v < 70 else "70-80" if v < 80 else "80+")
        b("حسب الجلسة", lambda t: t.get("session"))
        b("حسب الخبر", lambda t: t.get("news"))
        b("حسب الجاهزية", lambda t: _band(t.get("readiness")))
        b("حسب الدرجة", lambda t: _band(t.get("score")))
        b("الدايفرجنس", lambda t: t.get("divergence"))
        b("الاتجاه اليومي", lambda t: t.get("trend"))
        b("ADX", lambda t: None if t.get("adx") is None else
          ("قوي ≥25" if t["adx"] >= 25 else "ضعيف تحت 25"))
        # فرضية PYXS (أ): الرنر المنطفئ — 5min RVol منخفض = الحركة صارت وتطارد
        b("حسب 5min RVol", lambda t: None if t.get("rvol_5min") is None else
          ("نشط ≥2x" if t["rvol_5min"] >= 2 else "منطفئ تحت 2x"))
        # فرضية PYXS (ب): تمييز R/R الهدف1 — هل منخفضو العائد/المخاطرة يخسرون أكثر؟
        b("حسب R/R الهدف1", lambda t: None if t.get("t1_rr") is None else
          ("دون 0.5" if t["t1_rr"] < 0.5 else "0.5–1" if t["t1_rr"] < 1 else "≥1"))
        # فرضية المستخدم «الموجة الأخيرة الأضعف»: هل الحركة المتقدّمة (صعدت
        # كثيرًا اليوم) تخسر أكثر؟ عتبة من الإعداد (مخزّنة لكل صفقة).
        b("حسب تقدّم الحركة", lambda t: None if t.get("change_pct") is None
          else (f"متقدّمة ≥{t.get('late_wave_thr', 60):g}%"
                if t["change_pct"] >= t.get("late_wave_thr", 60) else "عادية"))
        # فرضية المستخدم «سهم الماركت»: هل نافذة الافتتاح (أول نص ساعة) أقوى؟
        b("حسب نافذة الافتتاح", lambda t: None if t.get("opening_range") is None
          else ("نافذة الافتتاح" if t["opening_range"] else "بعد الافتتاح"))

        # ── التوقّع المحقّق لكل شريحة (نسبة النجاح تخدع: هدف أقرب يُلمس أسهل
        # فترفع النسبة؛ التوقّع = متوسط realized_pct يوزن الربح بالخسارة فيكشف
        # الصافي الحقيقي — أساس أي قرار على شرائح R/R أو موقع VWAP) ──
        def exp_line(title, kf, order):
            groups: dict = {}
            for t in res.trades:
                k = kf(t)
                if k is not None:
                    groups.setdefault(k, []).append(t)
            rows = [(k, groups[k]) for k in order if len(groups.get(k, [])) >= 3]
            if not rows:
                return
            lines.append(f"\n{title}:")
            for k, g in rows:
                lines.append(f"  • {k}: توقّع {_avg(g, 'realized_pct'):+.1f}%"
                             f"/صفقة ({len(g)})")
        exp_line("توقّع محقّق حسب R/R الهدف1",
                 lambda t: None if t.get("t1_rr") is None else
                 ("دون 0.5" if t["t1_rr"] < 0.5 else
                  "0.5–1" if t["t1_rr"] < 1 else "≥1"),
                 ["دون 0.5", "0.5–1", "≥1"])
        exp_line("توقّع محقّق حسب موقع VWAP",
                 lambda t: None if t.get("above_vwap") is None else
                 ("فوق VWAP" if t["above_vwap"] else "تحت VWAP"),
                 ["فوق VWAP", "تحت VWAP"])
        # فرضية «الموجة الأخيرة الأضعف» بالتوقّع المحقّق (النسبة تخدع)
        _lw = (res.trades[0].get("late_wave_thr", 60) if res.trades else 60)
        exp_line("توقّع محقّق حسب تقدّم الحركة",
                 lambda t: None if t.get("change_pct") is None else
                 (f"متقدّمة ≥{t.get('late_wave_thr', 60):g}%"
                  if t["change_pct"] >= t.get("late_wave_thr", 60) else "عادية"),
                 [f"متقدّمة ≥{_lw:g}%", "عادية"])
        # فرضية «سهم الماركت»: توقّع نافذة الافتتاح مقابل بعدها
        exp_line("توقّع محقّق حسب نافذة الافتتاح",
                 lambda t: None if t.get("opening_range") is None else
                 ("نافذة الافتتاح" if t["opening_range"] else "بعد الافتتاح"),
                 ["نافذة الافتتاح", "بعد الافتتاح"])
        # ── تحقّق أرقام «الحركة النموذجية حسب الجلسة» (خبرة المستخدم) مقابل
        # الواقع: متوسط قمة الفائز الفعلية لكل جلسة (تُعاير الأرقام لاحقًا) ──
        sess_groups: dict = {}
        for t in res.trades:
            if t.get("result") == "win":
                sess_groups.setdefault(t.get("session"), []).append(t)
        sess_rows = [(k, g) for k, g in sess_groups.items() if len(g) >= 3]
        if sess_rows:
            lines.append("\n📊 قمة الفائز الفعلية حسب الجلسة "
                         "(تحقّق أرقام «توقّع الجلسة»):")
            for k, g in sess_rows:
                lines.append(f"  • {esc(str(k))}: +{_avg(g, 'max_gain_pct'):.0f}% "
                             f"متوسط قمة ({len(g)} فائز)")
        # ── قياس ظلّ: توسيع الهدف1 لشريحة «دون العتبة» (الهدف القريب أكبر عددًا
        # وأضعف توقّعًا؛ نقيس هل توسيعه يرفع التوقّع أم يحوّل إصابات سهلة لانتهاء
        # وقت). حدّ الشريحة = عتبة التوسيع المخزّنة (backtest_wide_t1_rr) كي يتّسق
        # مع الحساب لو غيّرها المستخدم. قياس فقط. §5: بلا < أو > حرفية. ──
        near = [t for t in res.trades
                if t.get("t1_rr") is not None
                and t.get("realized_wide_t1_pct") is not None
                and t["t1_rr"] < t.get("wide_t1_rr", 0.5)]
        if len(near) >= 3:
            thr = near[0].get("wide_t1_rr", 0.5)
            cur = _avg(near, "realized_pct")
            wide = _avg(near, "realized_wide_t1_pct")
            d = wide - cur
            tag = ("🟢 أفضل" if d > 0.05 else
                   "🔴 أسوأ" if d < -0.05 else "≈ مماثل")
            lines.append(f"\n🎯 قياس ظلّ: تخطّي هدف1 لمقاومة حقيقية أبعد "
                         f"لشريحة «دون {thr:g}» ({len(near)}):")
            lines.append(f"  • التوقّع/صفقة: حالي {cur:+.1f}% ← هدف متخطٍّ "
                         f"{wide:+.1f}% ({tag} {d:+.1f}%)")
            lines.append("  <i>↳ لو تُخطّي الهدف الأقرب لأقرب مقاومة حقيقية "
                         "بعائد/مخاطرة ≥ العتبة (لا مستوى اصطناعي). قياس فقط، "
                         "لا يُطبَّق على الحيّ.</i>")
        # ── المؤشرات الثنائية: نجاح «نعم» مقابل «لا» جنبًا لجنب (يكشف
        # أيها يتنبّأ بالنجاح فعلًا → أساس ضبط أوزان نظام الفرز بالبيانات) ──
        def _wr(g):
            d = [t for t in g if t["result"] in ("win", "loss")]
            return (sum(1 for t in d if t["result"] == "win") / len(d) * 100.0,
                    len(d)) if d else (None, 0)
        ind_specs = [
            ("MACD صاعد", "macd_bull"), ("تقاطع ذهبي", "golden_cross"),
            ("فوق MA200", "above_ma200"), ("فوق MA50", "above_ma50"),
            ("فوق VWAP", "above_vwap"), ("حجم متصاعد", "volume_rising"),
        ]
        ind_lines = []
        for name, key in ind_specs:
            yes = [t for t in res.trades if t.get(key) is True]
            no = [t for t in res.trades if t.get(key) is False]
            wy, ny = _wr(yes)
            wn, nn = _wr(no)
            if wy is not None and wn is not None and ny >= 5 and nn >= 5:
                ind_lines.append(
                    f"  • {name}: نعم {wy:.0f}% ({ny}) · لا {wn:.0f}% ({nn})")
        if ind_lines:
            lines.append("\n📐 المؤشرات الثنائية (نجاح نعم/لا):")
            lines.extend(ind_lines)
    # ── قمع الترشيح: يشرح «ليش العدد قليل؟» (أين مات المرشّحون) ──
    f = res.funnel
    if f and f.get("considered"):
        lines.append(
            f"\n🔎 قمع الترشيح (من {f['considered']} مرشّحًا اعتُبروا):")
        lines.append(f"  • فُقدت شموع 5د تاريخية: {f['no_5min']}")
        lines.append(f"  • ما عبرت الحدّ بإغلاق 5د: {f['no_trigger']}")
        if f.get("bad_snapshot"):
            lines.append(f"  • سنابشوت غير صالح: {f['bad_snapshot']}")
        if f.get("premarket_only"):
            lines.append(
                f"  • تخطّاها حارس البريماركت (رنر بريماركت فقط): "
                f"{f['premarket_only']}")
        if f.get("not_ranked"):
            lines.append(
                f"  • خارج أعلى-15 اللحظي (لم يدخل مجمّع الحي): "
                f"{f['not_ranked']}")
        if f.get("error"):
            lines.append(f"  • خطأ معالجة: {f['error']}")
        # DIAG-26: أثر بوّابة الترتيب اللحظي (BUG-02) — تنبيه تأخّر أو رفض بعد
        # حجب. القمع كان يخفيه (not_ranked=0 بينما البوّابة تحرّك عشرات الأسهم).
        if f.get("rank_gate_affected"):
            lines.append(
                f"  • مرّ عليه ترتيب أعلى-15 (تأخّر تنبيه أو حجب): "
                f"{f['rank_gate_affected']}")
        lines.append(f"  • رُفضت بالبوّابات: {f['rejected']}")
        for reason, cnt in sorted(f.get("reject_reasons", {}).items(),
                                  key=lambda x: -x[1]):
            lines.append(f"      ↳ {reason}: {cnt}")
        lines.append(f"  ✅ نجت كتنبيه: {f['alerts']}")
    # ── قياس الظل: أداء افتراضي لمرفوضي RVol (هل العتبة 5x تفوّت فرصًا؟) ──
    sh_all = (res.funnel or {}).get("shadow") or []
    sh = [x for x in sh_all if x.get("kind", "rvol") == "rvol"]   # قديم بلا kind = rvol
    sh_pc = [x for x in sh_all if x.get("kind") == "price_cap"]
    if sh:
        lines.append(f"\n🌑 قياس الظل — مرفوضو RVol ({len(sh)}) لو دخلناهم:")

        def _rv_bucket(mr):
            return "أقل من 2x" if mr < 2 else "2–3x" if mr < 3 else "3–5x"
        groups: dict = {}
        for srec in sh:
            groups.setdefault(_rv_bucket(srec["max_rvol"]), []).append(srec)
        for key in ("3–5x", "2–3x", "أقل من 2x"):
            g = groups.get(key)
            if not g:
                continue
            dec = [x for x in g if x["result"] in ("win", "loss")]
            w = (sum(1 for x in dec if x["result"] == "win") / len(dec) * 100.0
                 if dec else None)
            tail = f" · نجاح افتراضي {w:.0f}% ({len(dec)} محسومة)" \
                if w is not None else " · بلا محسومة"
            lines.append(f"  • أقصى RVol {key}: {len(g)} سهم{tail}")
        # حكم حيّ من الأرقام (لا اقتراح أزلي): شريحة 3–5x مقابل الناجين الفعليين
        dec35 = [x for x in (groups.get("3–5x") or [])
                 if x["result"] in ("win", "loss")]
        w35 = (sum(1 for x in dec35 if x["result"] == "win")
               / len(dec35) * 100.0) if dec35 else None
        if w35 is None or len(dec35) < 8 or s["win_rate"] is None:
            verdict = "عيّنة الظل غير كافية للحكم بعد."
        elif w35 >= s["win_rate"] - 15:
            verdict = (f"شريحة 3–5x ({w35:.0f}%) تقارب الناجين "
                       f"({s['win_rate']:.0f}%) — خفض RVol يستحق الدراسة "
                       "(قرارك بالبيانات).")
        else:
            verdict = (f"شريحة 3–5x ({w35:.0f}%) أدنى بكثير من الناجين "
                       f"({s['win_rate']:.0f}%) — عتبة RVol مثبتة؛ لا تُخفَّض.")
        lines.append(f"  <i>↳ {verdict}</i>")
    # ── قياس الظل: مرفوضو «سقف السعر» ($30) — هل السقف يحمي أم يفوّت؟ ──
    if sh_pc:
        decp = [x for x in sh_pc if x["result"] in ("win", "loss")]
        wp = (sum(1 for x in decp if x["result"] == "win") / len(decp) * 100.0
              if decp else None)
        tail = (f"نجاح افتراضي {wp:.0f}% ({len(decp)} محسومة)"
                if wp is not None else "بلا محسومة")
        lines.append(f"\n🌑 قياس الظل — مرفوضو سقف السعر ({len(sh_pc)}) "
                     f"لو دخلناهم: {tail}")
        if wp is None or len(decp) < 8 or s["win_rate"] is None:
            vp = "عيّنة الظل غير كافية للحكم بعد."
        elif wp >= s["win_rate"] - 15:
            vp = (f"يقارب الناجين ({s['win_rate']:.0f}%) — سقف السعر قد يفوّت "
                  "فرصًا (قرارك بالبيانات).")
        else:
            vp = (f"أدنى من الناجين ({s['win_rate']:.0f}%) — سقف السعر مثبَّت.")
        lines.append(f"  <i>↳ {vp}</i>")
    # ── إفصاح: حدود المحاكاة (طبقات تُقيَّم حيًّا فقط + حبيبية الزناد) ──
    # الباكتيست يمسح نفس الاستراتيجية الفنية للحي، لكنه يتخطّى طبقات خارجية
    # لا-حتمية/شبكية (محلّل Claude · الشورت · رادار SEC) وتوقّفات LULD/T12 (لا
    # تاريخ لها عبر REST)، ولا يحاكي توريث الأبطال (أثره على التنبيهات ضئيل:
    # البطل يحتاج عبور الحدّ ليُنبَّه، والمجمّع أوسع من 15 أصلًا). هذه الغيابات
    # تجعل التقدير **متفائلًا قليلًا** (بلا خصم هبوطي/تخفيف). §5: لا محارف
    # < أو > حرفية هنا (تُسقط رسالة تيليجرام كاملةً).
    lines.append(
        "\n⚠️ تقدير تاريخي (لا يضمن المستقبل؛ بلا انزلاق/دفتر أوامر؛ بلا محلّل "
        "Claude/شورت/رادار SEC؛ بلا محاكاة توريث الأبطال ولا توقّفات LULD/T12؛ "
        "الزناد والدخول على إغلاقات شموع 5د لا اللحظي).")
    lines.append(
        "ℹ️ المجمّع يُرتَّب لحظيًّا (أعلى-15 بالتغيّر عند كل شمعة، مطابقة الحي)؛ "
        "قيد متبقٍّ: المجمّع الأوّلي محدود بأعلى المرشّحين بقمة اليوم.")
    return "\n".join(lines)


def run_backtest(cfg: Config, base: MassiveClient, start: str, end: str,
                 progress=None) -> BacktestResult:
    days = trading_days(start, end)
    res = BacktestResult(start=start, end=end, days=len(days),
                         funnel=new_funnel(), run_config=_run_config(cfg))
    for i, day in enumerate(days, 1):
        # كاش جديد لكل يوم ثم يُحرَّر — يمنع تكديس بيانات الشهر كلها في الذاكرة
        # (سوق كامل × 31 يوم + تاريخ كل سهم) الذي يتجاوز حدّ ذاكرة Render.
        res.trades.extend(simulate_day(cfg, base, day, {}, res.funnel))
        if progress:
            progress(i, len(days), day, len(res.trades))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="باكتيست الماسح الشامل")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--csv", help="مسار لحفظ الصفقات CSV (اختياري)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.from_env()
    if not cfg.massive_api_key:
        print("MASSIVE_API_KEY مطلوب للباكتيست.")
        return 2
    base = MassiveClient(cfg)

    def _prog(i, total, day, n):
        print(f"  [{i}/{total}] {day} … إجمالي صفقات: {n}", flush=True)

    res = run_backtest(cfg, base, args.start, args.end, progress=_prog)
    print("\n" + format_report(res))

    if args.csv and res.trades:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(res.trades[0].keys()))
            w.writeheader()
            w.writerows(res.trades)
        print(f"\n💾 حُفظت الصفقات: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
