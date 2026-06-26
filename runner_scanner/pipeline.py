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
from .models import Candidate, HaltState, Session, SnapshotEntry
from .sessions import classify_session, now_et, session_elapsed_fraction

logger = logging.getLogger(__name__)


def _et_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def process_candidate(
    cfg: Config,
    client: MassiveClient,
    snap: SnapshotEntry,
    halts: HaltTracker | None = None,
    session: Session | None = None,
    et_now: datetime | None = None,
    short_provider=None,
) -> Candidate:
    """يعالج مرشّحًا واحدًا عبر خط المعالجة الكامل."""
    et_now = et_now or now_et()
    session = session or classify_session(cfg, et_now)
    c = Candidate(snapshot=snap, session=session)

    # ── 1) التوقّف ───────────────────────────────────────────────
    if halts is not None:
        st = halts.state_of(snap.ticker)
        c.halt_state = st
        if st is HaltState.T12:
            return c.reject("T12 — استبعاد نهائي")
        if st in (HaltState.HALTED, HaltState.RESUMED):
            return c.reject(f"توقّف ({st.value}) — لا بطاقة، انتظر استئنافًا نظيفًا")

    # ── 2) الفلوت + الماركت كاب ──────────────────────────────────
    try:
        c.float_shares, c.float_source = client.free_float(snap.ticker)
        shares = client.shares_outstanding(snap.ticker)
        if shares:
            c.market_cap = shares * snap.last_price
    except MassiveError as exc:
        logger.debug("فلوت/أسهم فشل لـ %s: %s", snap.ticker, exc)

    # ── 3) بوابات ما-قبل-التحليل (رخيصة، قبل جلب الشموع) ─────────
    pre = gates.apply_gates(cfg, c, gates.PRE_TA_GATES)
    if not pre.passed:
        return c.reject(pre.reason)

    # ── 4) جلب الشموع ────────────────────────────────────────────
    today = _et_date(et_now)
    year_ago = _et_date(et_now - timedelta(days=400))
    try:
        bars_5min = client.bars_5min(snap.ticker, today, today)
        bars_1min = client.bars_1min(snap.ticker, today, today)
        daily = client.bars_daily(snap.ticker, year_ago, today)
    except MassiveError as exc:
        return c.reject(f"تعذّر جلب الشموع: {exc}")

    # ── 5) التحليل: الركيزتان ────────────────────────────────────
    avg_daily_vol = (
        sum(b.v for b in daily[-20:]) / min(20, len(daily))
    ) if daily else 0.0
    elapsed = session_elapsed_fraction(cfg, et_now) \
        if session is Session.REGULAR else None
    c.momentum = intraday_ta.compute_momentum(
        cfg, snap, session, bars_5min, bars_1min,
        avg_daily_volume=avg_daily_vol, elapsed_fraction=elapsed)
    c.readiness = classic_ta.compute_readiness(cfg, daily)

    # ── 6) بوابات ما-بعد-التحليل (RVol + بارابولِك بعد VWAP) ─────
    post = gates.apply_gates(cfg, c, gates.POST_TA_GATES)
    if not post.passed:
        return c.reject(post.reason)

    # ── 7) الخبر/المحفّز (إشارة تقوية) ───────────────────────────
    gte = catalyst_mod.lookback_iso(cfg, et_now.astimezone(timezone.utc))
    raw_news = client.latest_news(snap.ticker, gte)
    c.catalyst = catalyst_mod.evaluate_catalyst(
        cfg, raw_news, et_now.astimezone(timezone.utc))

    # ── 8) الدرجة (جاهزية ≥70 + زخم فوق الحد) ───────────────────
    result = scoring.score_candidate(cfg, c)
    if not result.passed:
        return c.reject(result.reason)

    # ── 9) الوقف (دعم 5د) والأهداف (مقاومات حقيقية) ─────────────
    from . import risk
    closed_5min = bars_5min[:-1] if len(bars_5min) > 1 else bars_5min
    # مقاومات يومية كأهداف محتملة: قمة أمس + قمة آخر 10 أيام
    daily_res: list[float] = []
    if daily:
        if len(daily) >= 2:
            daily_res.append(daily[-2].h)                 # قمة أمس
        daily_res.append(max(b.h for b in daily[-10:]))   # قمة 10 أيام
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
