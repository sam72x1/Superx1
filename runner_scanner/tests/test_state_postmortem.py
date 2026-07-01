"""يتحقّق أن بيانات تشريح الفشل تُخزَّن وتُسترجَع (closed-loop كامل)."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from runner_scanner.models import (
    AnalystResult, Candidate, Catalyst, DilutionResult, RiskPlan, Session)
from runner_scanner.state import Store, trade_date_str
from runner_scanner.tests.fixtures import make_snapshot


def _store():
    db = os.path.join(tempfile.mkdtemp(), "pm.sqlite3")
    return Store(db)


def _cand():
    c = Candidate(snapshot=make_snapshot(ticker="DILX", last=3.0, change_pct=25.0),
                  session=Session.REGULAR)
    c.final_score = 72
    c.short_pct = 30.0
    c.dilution = DilutionResult(risk="مرتفع", latest_form="424B5")
    c.catalyst = Catalyst(has_news=True, headline="Pricing of public offering")
    c.analyst = AnalystResult(direction="هبوطي", warning="طرح")
    c.risk = RiskPlan(stop_price=2.7, stop_pct=10, entry_ref=3.0,
                      targets=[3.6, 3.9, 4.2], stop_basis="دعم 5د")
    return c


def test_postmortem_fields_persisted_and_queryable():
    st = _store()
    t0 = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
    day = trade_date_str(t0)
    c = _cand()
    st.log_candidate(c, t0)
    st.mark_alerted("DILX", 72, t0)
    # سعر تحت الوقف → خسارة
    events = st.update_outcomes({"DILX": 2.6}, t0)
    assert any(e["type"] == "stop" for e in events)

    row = st.fetch_row("DILX", day)
    assert row is not None
    assert row["dilution_risk"] == "مرتفع"
    assert row["short_pct"] == 30.0
    assert row["analyst_dir"] == "هبوطي"
    assert "offering" in (row["catalyst_head"] or "").lower()
    assert row["result"] == "loss"

    failures = st.fetch_failures(day)
    assert any(r["ticker"] == "DILX" for r in failures)
    st.close()


def test_fetch_row_latest_when_no_day():
    st = _store()
    t0 = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
    st.log_candidate(_cand(), t0)
    row = st.fetch_row("DILX")          # بلا يوم → الأحدث
    assert row is not None and row["ticker"] == "DILX"
    assert st.fetch_row("NOPE") is None
    st.close()


def test_first_session_immutable_across_relogs():
    """first_session تُثبَّت عند أول رصد ولا تُمسّ بإعادة التقييم — بينما session
    تتحدّث (يعتمد عليها منطق إعادة-التأسيس). كان 33% من الصفوف الحيّة بجلسة غلط."""
    st = _store()
    t0 = datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)    # 5ص ET = بريماركت
    day = trade_date_str(t0)
    c = _cand()
    c.session = Session.PREMARKET
    st.log_candidate(c, t0)
    # إعادة تقييم في الرسمي ثم الأفترهاوس (نفس السهم/اليوم)
    c.session = Session.REGULAR
    st.log_candidate(c, datetime(2026, 6, 26, 15, 0, tzinfo=timezone.utc))
    c.session = Session.AFTERHOURS
    st.log_candidate(c, datetime(2026, 6, 26, 21, 0, tzinfo=timezone.utc))
    row = st.fetch_row("DILX", day)
    assert row["first_session"] == Session.PREMARKET.value   # ثابتة
    assert row["session"] == Session.AFTERHOURS.value        # الأخيرة (مقصود)
    st.close()


def test_relog_rebaseline_still_works_with_first_session():
    """إعادة تأسيس السعر المرجعي عند دخول الرسمي (لغير المُنبَّه) لم تنكسر."""
    st = _store()
    t0 = datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)    # بريماركت
    day = trade_date_str(t0)
    c = _cand()
    c.session = Session.PREMARKET
    c.snapshot.last_price = 3.0
    st.log_candidate(c, t0)
    c.session = Session.REGULAR                               # دخل الرسمي
    c.snapshot.last_price = 3.5
    st.log_candidate(c, datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc))
    row = st.fetch_row("DILX", day)
    assert row["first_price"] == 3.5      # أُعيد التأسيس على سعر الرسمي
    assert row["first_session"] == Session.PREMARKET.value   # التحليل يبقى صادقًا
    st.close()
