"""اختبارات مزوّد الشورت (بلا شبكة — تُموَّه المصادر)."""

from __future__ import annotations

from runner_scanner.short_interest import (
    ShortInfo, ShortInterestProvider, _parse_finra,
)

_FINRA_SAMPLE = (
    "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
    "20260626|AAA|30000|0|100000|Q\n"
    "20260626|BBB|5000|0|200000|N\n"
)


def test_parse_finra():
    table = _parse_finra(_FINRA_SAMPLE)
    assert table["AAA"] == (30000.0, 100000.0)
    assert "Symbol" not in table


def test_finra_vol_pct_from_table():
    p = ShortInterestProvider()
    p._finra_cache["x"] = {"AAA": (30000.0, 100000.0)}
    # نتجاوز التحميل الشبكي بحقن الكاش وتثبيت _finra_table
    p._finra_table = lambda: {"AAA": (30000.0, 100000.0)}
    assert abs(p._finra_vol_pct("AAA") - 30.0) < 1e-6
    assert p._finra_vol_pct("ZZZ") is None


def test_finra_cache_keeps_only_latest_day():
    """تنظيف: كاش RegSHO يُبقي أحدث جدول فقط (لا ينمو كل يوم — كاش ذاكرة قتل
    الخدمة بحدّ Render مرّة). مفتاح يوم قديم يُستبدَل عند تحميل جديد."""
    from datetime import date
    p = ShortInterestProvider()
    p._finra_cache = {"20200101": {"OLD": (1.0, 2.0)}}   # يوم قديم متراكم

    class _Resp:
        status_code = 200
        text = _FINRA_SAMPLE

    p._http.get = lambda url, timeout=0: _Resp()
    table = p._finra_table()
    assert table["AAA"] == (30000.0, 100000.0)
    assert list(p._finra_cache) == [date.today().strftime("%Y%m%d")]
    assert "20200101" not in p._finra_cache            # القديم أُخلي


def test_merge_prefers_fintel_then_fallbacks():
    p = ShortInterestProvider()
    # Fintel يعطي حجم فقط، Yahoo يعطي فلوت، FINRA لا يُستدعى للحجم لأن Fintel غطّاه
    p._fintel = lambda t: ShortInfo(short_vol_pct=40.0, source="Fintel")
    p._finra_vol_pct = lambda t: 99.0   # يجب ألا يُستخدم (Fintel غطّى الحجم)
    p._yahoo_float_pct = lambda t: 12.0
    info = p.get("AAA", today="2026-06-26")
    assert info.short_vol_pct == 40.0       # من Fintel
    assert info.short_float_pct == 12.0     # من Yahoo
    assert "Fintel" in info.source and "Yahoo" in info.source


def test_finra_used_when_fintel_fails():
    p = ShortInterestProvider()
    p._fintel = lambda t: None
    p._finra_vol_pct = lambda t: 25.0
    p._yahoo_float_pct = lambda t: None
    info = p.get("BBB", today="2026-06-26")
    assert info.short_vol_pct == 25.0 and info.short_float_pct is None
    assert info.source == "FINRA"


def test_all_fail_returns_none():
    p = ShortInterestProvider()
    p._fintel = lambda t: None
    p._finra_vol_pct = lambda t: None
    p._yahoo_float_pct = lambda t: None
    assert p.get("CCC", today="2026-06-26") is None   # «—» لا صفر


def test_daily_cache_avoids_refetch():
    p = ShortInterestProvider()
    calls = {"n": 0}

    def _fintel(t):
        calls["n"] += 1
        return ShortInfo(short_float_pct=10.0, source="Fintel")

    p._fintel = _fintel
    p._finra_vol_pct = lambda t: None
    p._yahoo_float_pct = lambda t: None
    p.get("DDD", today="2026-06-26")
    p.get("DDD", today="2026-06-26")
    assert calls["n"] == 1   # نفس اليوم → جلب واحد
