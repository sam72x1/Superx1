"""مُختبِر تاريخي (Backtester) — يعيد تشغيل البوت على الماضي لقياس الحافة.

الفكرة: بدل انتظار أسابيع من البيانات الحيّة، نعيد تمثيل أيام تداول ماضية عبر
**نفس** خط المعالجة (process_candidate) ونقيس النتائج (نجاح/خسارة/بلا حسم).

⚠️ **مبدأ حاسم — لا تسرّب مستقبل (no-lookahead):** عند تقييم مرشّح في لحظة T من
يوم ماضٍ، لا يرى الكود إلا بيانات **حتى T** (يومي قبل اليوم · شموع حتى T · أخبار
حتى T). النتيجة تُقاس من شموع **بعد T** فقط. أي خرق لهذا يعطي نتائج متفائلة كاذبة.

النطاق (v1): يختبر **الاستراتيجية الفنية الأساسية** (كشف + بوّابات + ركيزتان +
وقف/أهداف). يتخطّى طبقات Claude/SEC/الشورت (تُقيَّم حيًّا). تقريبات موثّقة:
- «أعلى N» يُقرَّب بأعلى N صعودًا في قمة اليوم (proxy لـ top-gainers اللحظي).
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
from .massive_client import MassiveClient
from .models import Bar, Session, SnapshotEntry
from .pipeline import _closed_daily, daily_resistance_targets, process_candidate
from .risk import build_risk_plan
from .sessions import ET, classify_session
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
                 static_cache: dict):
        self._base = base
        self._date = date_str
        self._asof = asof_ms
        self._5 = bars_5min        # مقصوصة مسبقًا حتى asof
        self._1 = bars_1min
        self._static = static_cache

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
        # **الجزئية** المعاد بناؤها «حتى T» من شموع 5د المقصوصة — لأن الحي يمرّر
        # اليومي شاملًا شمعة اليوم الجزئية إلى compute_readiness (الاستبعاد بعُمر
        # الطابع في _closed_daily يطال المتوسطات/المقاومات فقط). بلا هذه الشمعة
        # كانت جاهزية الباكتيست أشدّ من الحي فترفض «جاهزية» أكثر.
        # بلا تسرّب: كل قيمها من شموع ≤ asof. عُمرها <20h (طابعها = أول 5د اليوم)
        # فيستبعدها _closed_daily في الـ pipeline من المتوسطات/المقاومات كما مع
        # الحي — يبقى السلوكان متطابقين في الموضعين.
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
        lte = _iso_utc(datetime.fromtimestamp(self._asof / 1000, tz=timezone.utc))
        return self._base.latest_news(ticker, published_gte_utc, limit=limit,
                                      published_lte_utc=lte)


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
        low = min(low, b.l)             # القاع يتحدّث دائمًا (لقياس أقصى سحب)
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
    """ربح افتراضي لو رُفع الهدف1 إلى عائد/مخاطرة ≥ min_rr (قياس فقط، لا حيّ):
    نستبدل الهدف1 بالأبعد بين الأصلي و entry×(1+min_rr×stop_pct/100)، ثم نقيس:
    بلوغ الهدف الموسّع = ربح · كسر الوقف = خسارة كاملة · انتهاء النافذة = 0.
    للصفقات التي هدفها أصلًا ≥ min_rr لا يتغيّر شيء (الهدف الموسّع = الأصلي).
    يكشف: هل الهدف القريب («دون min_rr») يترك ربحًا، أم توسيعه يحوّل إصابات
    سهلة إلى انتهاء وقت؟ تحفّظ: الوقف أولًا داخل الشمعة. لا-تسرّب-المستقبل."""
    if not risk or not risk.targets or entry <= 0 or not risk.stop_pct:
        return 0.0
    stop = risk.stop_price
    wide = max(risk.targets[0], entry * (1.0 + min_rr * risk.stop_pct / 100.0))
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
            "bad_snapshot": 0, "premarket_only": 0, "error": 0,
            "rejected": 0, "alerts": 0,
            "reject_reasons": {}, "shadow": []}


def _news_label(cand) -> str:
    """تصنيف الخبر للباكتيست: «إيجابي» (مُكافأ) · «سلبي» (طرح/تخفيف) · «بلا».
    مهمّ: المكافأة +8 تُمنح للإيجابي فقط، فالفصل يقيس أثرها الحقيقي لا «أي خبر»."""
    cat = getattr(cand, "catalyst", None)
    if not (cat and cat.has_news):
        return "بلا"
    return "سلبي" if cat.category == NEGATIVE_NEWS else "إيجابي"


def _reject_bucket(reason: str) -> str:
    """يصنّف سبب الرفض لفئة موجزة (لتجميع «أكثر بوّابة ترفض»)."""
    r = reason or ""
    # الترتيب مهمّ: المفاتيح الأدقّ أولًا (كلّ سبب سعر يحوي «سعر»، فنميّز «سنتات»
    # و«فوق نطاق» قبل «سعر» العام)؛ و«جاهزية» و«درجة» مفصولتان (كانتا مدموجتين).
    pairs = [("فلوت", "فلوت"), ("RVol", "RVol"), ("بارابولِك", "بارابولِك"),
             ("تحت VWAP", "تحت VWAP"),
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
                    static_cache: dict, pc: float, ticker: str) -> dict:
    """يقيّم مرشّحًا واحدًا (آمن للتشغيل المتوازي) → نتيجة موسومة.

    يحاكي المسح المتكرّر للبوت الحي: يفحص عند كل شمعة 5د يكون فيها رنرًا حتى
    **أول نجاح** (تنبيه واحد/سهم/يوم) — المرفوض يُعاد فحصه مع تراكم الحجم، لا
    يُسقَط للأبد عند أول عبور. لا يلمس حالة مشتركة (التجميع لاحقًا تسلسليًّا).
    """
    # best-effort (القسم 3): فشل شبكي لسهم واحد يتخطّاه ولا يكسر الباكتيست.
    try:
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
    last_reason = ""
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
        client = AsOfClient(base, day, asof, up_to, up_to_1, static_cache)
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
            # ✅ نجح في هذه الدورة → تنبيه عند لحظتها (دخول = إغلاق الشمعة)
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
            return {"kind": "alert", "trade": {
                "date": day, "ticker": ticker,
                "entry": round(entry, 4),
                "session": cand.session.value,
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
                "target_hit": tlevel,                  # أعلى هدف لُمس (0..3)
                "result": result, "max_gain_pct": round(gain, 1),
                "max_draw_pct": round(draw, 1),
            }}
        last_reason = cand.rejected_reason or ""
        # بوّابات لا تتغيّر خلال اليوم (فلوت/نوع/بورصة) → لا فائدة من إعادة الفحص
        if _reject_bucket(last_reason) in ("فلوت", "نوع/بورصة"):
            break
    if not evaluated:
        if errored:
            return {"kind": "error"}
        # كل شموع رنره في البريماركت وتخطّاها الحارس (مطابقة الحي) — ليس سنابشوت
        # فاسدًا؛ السهم صالح لكنه خارج ساعات التنبيه. تصنيف منفصل كي لا يلوّث القمع.
        if premarket_skipped:
            return {"kind": "premarket_only"}
        return {"kind": "bad_snapshot"}
    # قياس الظل: لو الرفض النهائي بسبب RVol، نحسب نتيجة افتراضية (لو دخلنا) +
    # أقصى RVol بلغه — يكشف لاحقًا إن كانت عتبة RVol=5x تفوّت فرصًا (قياس فقط).
    shadow = None
    if (cfg.backtest_shadow_rvol and last_snap is not None
            and _reject_bucket(last_reason) == "RVol"):
        closed = [x for x in full5 if x.t_ms <= last_asof]
        closed5 = closed[:-1] if len(closed) > 1 else closed
        post = [x for x in full5 if x.t_ms > last_asof]
        # مقاومات يومية مطابقة للخط الفعلي (وإلا أهداف الظل تختلف فيَختلّ حكم
        # «العتبة مثبتة/تستحق الدراسة» المبنيّ عليها). نفس _closed_daily الحي.
        last_dt = datetime.fromtimestamp(
            last_asof / 1000, tz=timezone.utc).astimezone(ET)
        daily = AsOfClient(base, day, last_asof, closed, [],
                           static_cache).bars_daily(ticker, "", "")
        daily_res = daily_resistance_targets(
            _closed_daily(daily, last_dt), last_snap.last_price)
        risk = build_risk_plan(cfg, last_snap.last_price, closed5,
                               daily_resistances=daily_res)
        sres, _, _, _ = simulate_outcome(last_snap.last_price, risk, post,
                                         last_asof, cfg.outcome_window_min)
        shadow = {"max_rvol": round(max_rvol, 1), "result": sres}
    # رُفض في كل الدورات → سببه من آخر محاولة (أكثر تمثيلًا لقيد نهاية اليوم)
    return {"kind": "rejected", "reason": last_reason, "shadow": shadow}


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

    def _run(tc):
        ticker = tc[0]
        return _eval_candidate(cfg, base, day, static_cache,
                               prev_close[ticker], ticker)

    workers = max(1, cfg.backtest_workers)
    if workers > 1 and len(cands) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(cands))) as ex:
            results = list(ex.map(_run, cands))
    else:
        results = [_run(tc) for tc in cands]

    # ── تجميع تسلسليّ آمن (بلا تسابق) ──
    trades: list[dict] = []
    for r in results:
        kind = r["kind"]
        if kind == "alert":
            trades.append(r["trade"])
            if funnel is not None:
                funnel["alerts"] += 1
        elif funnel is not None:
            if kind == "rejected":
                funnel["rejected"] += 1
                bucket = _reject_bucket(r.get("reason", ""))
                funnel["reject_reasons"][bucket] = \
                    funnel["reject_reasons"].get(bucket, 0) + 1
                if r.get("shadow"):
                    funnel["shadow"].append(r["shadow"])
            else:
                funnel[kind] += 1   # no_5min · no_trigger · bad_snapshot · error
    return trades


# ── النتيجة المجمّعة + التقرير ────────────────────────────────────
@dataclass
class BacktestResult:
    start: str
    end: str
    days: int = 0
    trades: list[dict] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)

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
                   "created_utc": created}
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
            lines.append(f"\n🎯 قياس ظلّ: توسيع هدف1 لشريحة «دون {thr:g}» "
                         f"({len(near)}):")
            lines.append(f"  • التوقّع/صفقة: حالي {cur:+.1f}% ← هدف أوسع "
                         f"{wide:+.1f}% ({tag} {d:+.1f}%)")
            lines.append("  <i>↳ لو رُفع هدف1 لعائد/مخاطرة ≥ العتبة. قياس فقط، "
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
        if f.get("error"):
            lines.append(f"  • خطأ معالجة: {f['error']}")
        lines.append(f"  • رُفضت بالبوّابات: {f['rejected']}")
        for reason, cnt in sorted(f.get("reject_reasons", {}).items(),
                                  key=lambda x: -x[1]):
            lines.append(f"      ↳ {reason}: {cnt}")
        lines.append(f"  ✅ نجت كتنبيه: {f['alerts']}")
    # ── قياس الظل: أداء افتراضي لمرفوضي RVol (هل العتبة 5x تفوّت فرصًا؟) ──
    sh = (res.funnel or {}).get("shadow") or []
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
    return "\n".join(lines)


def run_backtest(cfg: Config, base: MassiveClient, start: str, end: str,
                 progress=None) -> BacktestResult:
    days = trading_days(start, end)
    res = BacktestResult(start=start, end=end, days=len(days),
                         funnel=new_funnel())
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
