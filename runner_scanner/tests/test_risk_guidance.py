"""إرشاد المخاطر على البطاقة + مرساة نافذة النتيجة.

يثبّت أربعة أشياء وجدها تقييم مستقلّ ناقصةً أو معطوبة:
(١) البطاقة تعلن أن الرقم المقيس يخصّ البيع الكامل لا قاعدة الترقية.
(٢) سطر تحجيم المركز — الرقم الوحيد الذي يقرّر البقاء، وكان غائبًا تمامًا.
(٣) قيمة التداول من حجم **الجلسة** لا snap.day_volume (فخّ §4).
(٤) نافذة النتيجة تُرسى على لحظة التنبيه لا أوّل رصد (BUG-39).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from runner_scanner.alerts import _dollar_volume, build_card
from runner_scanner.config import Config
from runner_scanner.models import (Candidate, MomentumResult, Session,
                                   SnapshotEntry)
from runner_scanner.risk import build_risk_plan


def _cand(cfg, *, price=10.0, sess=Session.REGULAR, sess_vol=0.0, day_vol=0.0):
    snap = SnapshotEntry("TST", price, 8.0, 8.0, price, 7.8, day_vol,
                         price * 0.95, 25.0)
    c = Candidate(snapshot=snap, session=sess)
    c.momentum = MomentumResult(
        score=35, rvol=9.0, rvol_5min=12.0, change_5min_pct=3.0,
        vwap_distance_pct=4.0, above_vwap=True, volume_rising=True,
        session_volume=sess_vol)
    c.risk = build_risk_plan(cfg, price, [])
    c.final_score = 80.0
    return c


def test_card_declares_which_rule_the_number_belongs_to():
    """البطاقة كانت ترشد لقاعدة الترقية (+0.7%) بينما الرقم المعلن للبيع
    الكامل (+2.4%) — أي أن الرقم الذي يثق به المستخدم ليس رقم القاعدة التي
    يتبعها. الآن تُصرّح بالفرق."""
    cfg = Config(massive_api_key="x")
    card = build_card(cfg, _cand(cfg))
    assert "رقِّ الوقف" in card                 # الإرشاد باقٍ
    assert "البيع الكامل عند" in card           # ومعه الحقيقة
    assert "+0.7%" in card


def test_card_shows_position_size_only_when_account_known():
    """لا يوجد في المشروع كلّه سطر عن حجم المركز. يُعرض فقط عند معرفة الحساب —
    لا نخمّن رقمًا يقرّر بقاء المستخدم."""
    off = Config(massive_api_key="x")            # account_size_usd=0
    assert "⚖️ مخاطرة 2% =" not in build_card(off, _cand(off))

    on = Config(massive_api_key="x", account_size_usd=10_000.0,
                risk_per_trade_pct=2.0, stop_fixed_pct=7.0)
    card = build_card(on, _cand(on, price=10.0))
    # مخاطرة 2% من 10,000 = 200$ ÷ (10$ × 7%) = 285 سهمًا
    assert "285 سهم" in card
    assert "$200.00" in card


def test_dollar_volume_uses_session_volume_not_day_volume():
    """§4: day_volume صفري/جزئي في الجلسات الممتدة. استخدامه يعطي «سيولة صفر»
    على سهم نشط — وهو الفخّ الأكثر تكرارًا في هذا المشروع."""
    cfg = Config(massive_api_key="x")
    # أفترهاوس: day_volume صفر (artifact) لكن حجم الجلسة حقيقي
    c = _cand(cfg, price=10.0, sess=Session.AFTERHOURS,
              sess_vol=50_000, day_vol=0.0)
    assert _dollar_volume(c) == 500_000.0
    # الرسمي: يرتدّ لليومي حين لا حجم جلسة
    c2 = _cand(cfg, price=10.0, sess=Session.REGULAR,
               sess_vol=0.0, day_vol=30_000)
    assert _dollar_volume(c2) == 300_000.0


def test_card_warns_on_thin_liquidity():
    """8 من 20 تنبيهًا حيًّا كانت تحت 400 ألف دولار، أرقّها 61 ألفًا فقط."""
    cfg = Config(massive_api_key="x")
    thin = build_card(cfg, _cand(cfg, price=10.0, sess_vol=6_000))  # $60K
    assert "رقيق" in thin and "مركز آمن" in thin
    thick = build_card(cfg, _cand(cfg, price=10.0, sess_vol=200_000))  # $2M
    assert "مركز آمن" in thick and "رقيق" not in thick


def test_outcome_window_anchors_on_alert_time_not_first_seen():
    """BUG-39: سهم يُرصد صباحًا مرفوضًا ثم يُنبَّه بعد ساعتين كانت نافذته
    منتهية سلفًا (مرساة first_seen_at) ⇒ يُغلق timeout في أوّل تحديث ⇒ لا تصلك
    «بلغ الهدف» ولا «كسر الوقف» وأنت ممسك بالصفقة. الآن تُرسى على لحظة التنبيه."""
    import tempfile, os
    from runner_scanner.state import Store

    cfg = Config(massive_api_key="x", outcome_window_min=90.0,
                 stop_fixed_pct=7.0)
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        st = Store(path)
        t0 = datetime(2026, 7, 29, 13, 35, tzinfo=timezone.utc)   # أوّل رصد
        c = _cand(cfg, price=10.0)
        c.reject("RVol منخفض")                    # رُصد مرفوضًا أوّلًا
        st.log_candidate(c, t0)
        # بعد ساعتين: يُقبل ويُنبَّه (النافذة القديمة كانت ستنتهي سلفًا)
        t_alert = t0 + timedelta(hours=2)
        st.mark_alerted("TST", 80, t_alert, entry_price=10.0,
                        stop_price=9.3, targets=[11.0])
        # بعد 10 دقائق من التنبيه فقط ⇒ يجب أن يبقى مفتوحًا
        st.update_outcomes({"TST": 10.2}, t_alert + timedelta(minutes=10))
        row = st.fetch_row("TST", t0.date().isoformat())
        assert row["outcome"] == "open", "أُغلق رغم أن نافذته لم تبدأ إلا للتوّ"
        # وبعد 95 دقيقة من التنبيه ⇒ يُغلق طبيعيًّا
        st.update_outcomes({"TST": 10.2}, t_alert + timedelta(minutes=95))
        row = st.fetch_row("TST", t0.date().isoformat())
        assert row["outcome"] == "closed"
    finally:
        os.unlink(path)
