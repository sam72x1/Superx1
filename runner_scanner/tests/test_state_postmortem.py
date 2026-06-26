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
