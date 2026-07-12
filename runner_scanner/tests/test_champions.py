"""اختبارات توريث أبطال الفترة بين الجلسات."""

from __future__ import annotations

import os
import tempfile

from runner_scanner.models import Session
from runner_scanner.state import Store


def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "c.sqlite3"))


def test_save_and_get_champions():
    st = _store()
    rows = [("AAA", 60.0, 3.2), ("BBB", 40.0, 2.1), ("CCC", 25.0, 8.0)]
    st.save_champions(Session.AFTERHOURS.value, "2026-06-25", rows)
    got = st.get_session_champions(Session.AFTERHOURS.value, "2026-06-25")
    assert [g["symbol"] for g in got] == ["AAA", "BBB", "CCC"]   # حسب rank


def test_save_replaces_same_session_day():
    st = _store()
    st.save_champions(Session.REGULAR.value, "2026-06-26", [("X", 30.0, 1.0)])
    st.save_champions(Session.REGULAR.value, "2026-06-26", [("Y", 50.0, 2.0)])
    got = st.get_session_champions(Session.REGULAR.value, "2026-06-26")
    assert [g["symbol"] for g in got] == ["Y"]                   # استبدال


def test_premarket_inherits_afterhours_yesterday():
    st = _store()
    st.save_champions(Session.AFTERHOURS.value, "2026-06-25",
                      [("AAA", 60.0, 3.2), ("CCC", 25.0, 8.0)])
    # بري اليوم (26) يرث أبطال افتر أمس (25)
    inh = st.inherited_champions(Session.PREMARKET.value, "2026-06-26")
    assert inh == ["AAA", "CCC"]


def test_regular_inherits_premarket_today():
    st = _store()
    st.save_champions(Session.PREMARKET.value, "2026-06-26",
                      [("DDD", 45.0, 2.0)])
    inh = st.inherited_champions(Session.REGULAR.value, "2026-06-26")
    assert inh == ["DDD"]


def test_afterhours_inherits_regular_today():
    st = _store()
    st.save_champions(Session.REGULAR.value, "2026-06-26", [("EEE", 33.0, 4.0)])
    inh = st.inherited_champions(Session.AFTERHOURS.value, "2026-06-26")
    assert inh == ["EEE"]


def test_no_champions_returns_empty():
    st = _store()
    assert st.inherited_champions(Session.PREMARKET.value, "2026-06-26") == []


def test_regular_inherits_premarket_exact_day_only():
    """BUG-06: توريث الرسمي من بريماركت اليوم (إزاحة 0) — الغياب يعطي فارغًا
    لا يبعث بريماركت يوم قديم. (قبل الإصلاح كان <= يرتدّ ليوم قديم اعتباطيًا.)"""
    st = _store()
    # بريماركت يوم قديم فقط (26) — لا يوجد بريماركت اليوم (29)
    st.save_champions(Session.PREMARKET.value, "2026-06-26",
                      [("OLD", 40.0, 2.0)])
    inh = st.inherited_champions(Session.REGULAR.value, "2026-06-29")
    assert inh == []                       # لا يبعث OLD القديم
    # وحين يوجد بريماركت اليوم نفسه → يُورَّث
    st.save_champions(Session.PREMARKET.value, "2026-06-29",
                      [("TODAY", 55.0, 3.0)])
    assert st.inherited_champions(Session.REGULAR.value, "2026-06-29") == ["TODAY"]
