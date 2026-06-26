"""خط المعالجة الكامل لمرشّح واحد (القسم 10) — منفصل عن الحلقة لقابلية الاختبار.

يأخذ مرشّحًا خامًا (من السنابشوت) + عميل البيانات + الحالة، ويمرّره عبر:
الجلسة → التوقّف → بوابات ما-قبل-التحليل → التحليل (ركيزتان) → بوابات
ما-بعد-التحليل → الخبر → الدرجة → الوقف/الأهداف.

يرجّع Candidate (مقبول أو مرفوض مع السبب). لا يرسل ولا يخزّن — ذلك على main.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import catalyst as catalyst_mod
from . import classic_ta, gates, intraday_ta, scoring
from .config import Config
from .halts import HaltTracker
from .massive_client import MassiveClient, MassiveError
from .models import Candidate, FloatSource, HaltState, Session, SnapshotEntry
from .sessions import (
    ET, classify_session, now_et, session_elapsed_fraction,
    session_volume_baselines,
)

logger = logging.getLogger(__name__)


def _et_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _bar_et_date(b) -> str:
    return datetime.fromtimestamp(b.t_ms / 1000, tz=timezone.utc) \
        .astimezone(ET).strftime("%Y-%m-%d")


def _closed_daily(daily: list, today: str) -> list:
    """يستبعد شمعة اليوم الجزئية (نفس تاريخ اليوم) من السلسلة اليومية —
    شمعة اليوم في البريماركت حجمها/قمتها جزئية مضلّلة (متوسط الحجم/المقاومات)."""
    return [b for b in daily if b.t_ms > 0 and _bar_et_date(b) != today]


def process_candidate(
    cfg: Config,
    client: MassiveClient,
    snap: SnapshotEntry,
    halts: HaltTracker | None = None,
    session: Session | None = None,
    et_now: datetime | None = None,
    short_provider=None,
    cache=None,
    analyst=None,
    sec_radar=None,
) -> Candidate:
    """يعالج مرشّحًا واحدًا عبر خط المعالجة الكامل."""
    et_now = et_now or now_et()
    session = session or classify_session(cfg, et_now)
    c = Candidate(snapshot=snap, session=session)
    today = _et_date(et_now)
    tkr = snap.ticker

    def _cached(key: str, fetch):
        """يكاش البيانات البطيئة لكل (سهم/يوم) إن وُجد كاش."""
        return cache.get(today, key, fetch) if cache is not None else fetch()

    # ── 1) التوقّف ───────────────────────────────────────────────
    if halts is not None:
        st = halts.state_of(snap.ticker)
        c.halt_state = st
        if st is HaltState.T12:
            return c.reject("T12 — استبعاد نهائي")
        if st in (HaltState.HALTED, HaltState.RESUMED):
            return c.reject(f"توقّف ({st.value}) — لا بطاقة، انتظر استئنافًا نظيفًا")

    # ── 2) تفاصيل الورقة (نوع/بورصة/أسهم) + الفلوت + الماركت كاب ──
    # بطيئة لا تتغيّر خلال اليوم → تُكاش لكل (سهم/يوم).
    overview = _cached(f"ov:{tkr}", lambda: client.ticker_overview(tkr))
    c.ticker_type = (overview.get("type") or "").upper()
    c.primary_exchange = (overview.get("primary_exchange") or "").upper()
    shares = overview.get("weighted_shares_outstanding") or \
        overview.get("share_class_shares_outstanding")
    # حقل مرجعي خام قد يكون نصًّا/كائنًا مشوّهًا → تطبيع دفاعي لا يُسقط الدورة
    try:
        shares = float(shares) if shares else None
    except (TypeError, ValueError):
        shares = None
    if shares:
        c.market_cap = shares * snap.last_price
    # الفلوت: endpoint vX، وإلا الأسهم القائمة (ليس فلوت حقيقي)، وإلا مجهول
    fv = _cached(f"fl:{tkr}", lambda: client.float_endpoint(tkr))
    if fv:
        c.float_shares, c.float_source = fv, FloatSource.FLOAT_ENDPOINT
    elif shares:
        c.float_shares, c.float_source = shares, FloatSource.SHARES_OUTSTANDING
    else:
        c.float_shares, c.float_source = None, FloatSource.UNKNOWN

    # ── 3) بوابات ما-قبل-التحليل (رخيصة، قبل جلب الشموع) ─────────
    pre = gates.apply_gates(cfg, c, gates.PRE_TA_GATES)
    if not pre.passed:
        return c.reject(pre.reason)

    # ── 4) جلب الشموع ────────────────────────────────────────────
    year_ago = _et_date(et_now - timedelta(days=400))
    two_months = _et_date(et_now - timedelta(days=60))
    try:
        # الشموع اللحظية طازجة دائمًا (الزخم لحظي)
        bars_5min = client.bars_5min(tkr, today, today)
        bars_1min = client.bars_1min(tkr, today, today)
        # اليومي/الساعة بطيئة → تُكاش لكل (سهم/يوم)
        daily = _cached(f"d:{tkr}", lambda: client.bars_daily(tkr, year_ago, today))
        hourly = _cached(
            f"h:{tkr}", lambda: client.aggregates(tkr, 1, "hour", two_months, today))
    except MassiveError as exc:
        return c.reject(f"تعذّر جلب الشموع: {exc}")

    # ── 5) التحليل: الركيزتان ────────────────────────────────────
    # متوسط الحجم اليومي من الأيام **المغلقة** فقط (استبعاد شمعة اليوم الجزئية
    # التي تلوّث المتوسط فتنفخ RVol كذبًا في البريماركت).
    daily_closed = _closed_daily(daily, today)
    avg_daily_vol = (
        sum(b.v for b in daily_closed[-20:]) / min(20, len(daily_closed))
    ) if daily_closed else 0.0
    elapsed = session_elapsed_fraction(cfg, et_now) \
        if session is Session.REGULAR else None
    # RVol حقيقي للجلسات الممتدة: متوسط حجم البريماركت/الأفترهاوس من
    # شموع الساعة (بدل تقدير 3%/5%). تُحسب فقط عند الحاجة.
    avg_pre = avg_aft = None
    if session in (Session.PREMARKET, Session.AFTERHOURS):
        avg_pre, avg_aft = session_volume_baselines(cfg, hourly, today)
    c.momentum = intraday_ta.compute_momentum(
        cfg, snap, session, bars_5min, bars_1min,
        avg_daily_volume=avg_daily_vol, avg_premarket_volume=avg_pre,
        avg_afterhours_volume=avg_aft, elapsed_fraction=elapsed)
    c.readiness = classic_ta.compute_readiness(cfg, daily, hourly=hourly)

    # ── 6) بوابات ما-بعد-التحليل (RVol + بارابولِك بعد VWAP) ─────
    post = gates.apply_gates(cfg, c, gates.POST_TA_GATES)
    if not post.passed:
        return c.reject(post.reason)

    # ── 7) الخبر/المحفّز (إشارة تقوية) ───────────────────────────
    gte = catalyst_mod.lookback_iso(cfg, et_now.astimezone(timezone.utc))
    raw_news = client.latest_news(snap.ticker, gte)
    c.catalyst = catalyst_mod.evaluate_catalyst(
        cfg, raw_news, et_now.astimezone(timezone.utc))

    # ── 8) الدرجة (جاهزية ≥60 + زخم فوق الحد) ───────────────────
    result = scoring.score_candidate(cfg, c)
    if not result.passed:
        return c.reject(result.reason)

    # ── 8.5) المحلّل الذكي (Claude) — للمقبولين فقط ──────────────
    # يقيّم المحفّز؛ خبر هبوطي (طرح/تخفيف) يخصم الدرجة وقد يُسقط التنبيه.
    if analyst is not None:
        try:
            c.analyst = analyst.analyze(c)
        except Exception as exc:  # noqa: BLE001 — محلّل best-effort
            logger.debug("المحلّل الذكي فشل لـ %s: %s", tkr, exc)
            c.analyst = None
        if c.analyst is not None and c.analyst.is_bearish:
            c.final_score = max(0.0, c.final_score - cfg.analyst_bearish_penalty)
            if c.final_score < cfg.alert_score_min:
                return c.reject(
                    f"محفّز هبوطي ({c.analyst.warning or c.analyst.direction})"
                    f" → درجة {c.final_score:.0f} تحت العتبة")

    # ── 8.6) رادار التخفيف (SEC) — للمقبولين فقط ─────────────────
    # طرح/تخفيف فعّال (S-1/424B/EFFECT) يضرّ السهم الصاعد كالشورت تمامًا
    # → خصم درجة وقد يُسقط التنبيه. الرفّ المُسجّل (متوسط) خصمه أخفّ.
    if sec_radar is not None:
        try:
            c.dilution = _cached(
                f"sec:{tkr}", lambda: sec_radar.check(tkr, today=et_now.date()))
        except Exception as exc:  # noqa: BLE001 — مصدر خارجي best-effort
            logger.debug("رادار SEC فشل لـ %s: %s", tkr, exc)
        if c.dilution is not None and c.dilution.is_active:
            pen = cfg.dilution_penalty * (0.5 if c.dilution.risk == "متوسط" else 1.0)
            c.final_score = max(0.0, c.final_score - pen)
            if c.final_score < cfg.alert_score_min:
                return c.reject(
                    f"تخفيف {c.dilution.risk} ({c.dilution.latest_form})"
                    f" → درجة {c.final_score:.0f} تحت العتبة")

    # ── 9) الوقف (دعم 5د) والأهداف (مقاومات حقيقية) ─────────────
    from . import risk
    closed_5min = bars_5min[:-1] if len(bars_5min) > 1 else bars_5min
    # مقاومات يومية كأهداف محتملة — من الأيام **المغلقة** فقط (لا شمعة اليوم
    # الجزئية)، وبسقف اتجاهي قريب: قمة بعيدة جدًا فوق السعر (سهم منهار) ليست
    # هدفًا واقعيًا داخل-الجلسة (تجنّب فئة بق +474%).
    daily_res: list[float] = []
    if daily_closed:
        daily_res.append(daily_closed[-1].h)                      # قمة أمس
        daily_res.append(max(b.h for b in daily_closed[-10:]))    # قمة 10 أيام
    daily_cap = snap.last_price * 1.30   # المقاومة اليومية كهدف ضمن +30% فقط
    daily_res = [r for r in daily_res if r and r <= daily_cap]
    c.risk = risk.build_risk_plan(cfg, snap.last_price, closed_5min,
                                  daily_resistances=daily_res)

    # ── 10) الشورت (يضرّ السهم) — للمقبولين فقط (تجنّب تعليق الحلقة) ─
    # عرض فقط لا يؤثّر على الفرز؛ best-effort، تعذّر ≠ صفر. كاش يومي.
    if short_provider is not None:
        try:
            info = short_provider.get(snap.ticker)
            if info is not None:
                c.short_pct = info.short_float_pct
                c.short_vol_pct = info.short_vol_pct
                c.short_source = info.source
        except Exception as exc:  # noqa: BLE001 — مصادر خارجية best-effort
            logger.debug("شورت فشل لـ %s: %s", snap.ticker, exc)

    return c
