"""اختبارات تحليل ردود Massive (بلا شبكة)."""

from __future__ import annotations

from runner_scanner.massive_client import MassiveClient


def test_snapshot_uses_api_change_field():
    entry = MassiveClient._parse_snapshot_entry({
        "ticker": "AAA",
        "todaysChangePerc": 22.5,
        "day": {"o": 2.0, "h": 2.6, "l": 1.9, "c": 2.4, "v": 800000, "vw": 2.2},
        "prevDay": {"c": 2.0},
        "lastTrade": {"p": 2.45},
    })
    assert entry.ticker == "AAA"
    assert entry.change_pct == 22.5
    assert entry.last_price == 2.45
    assert entry.is_valid is True


def test_snapshot_computes_change_when_field_missing():
    # لا حقل todaysChangePerc → يُحسب من السعر/إغلاق أمس (2.5 من 2.0 = +25%)
    entry = MassiveClient._parse_snapshot_entry({
        "ticker": "BBB",
        "day": {"c": 2.5, "v": 500000},
        "prevDay": {"c": 2.0},
        "lastTrade": {"p": 2.5},
    })
    assert abs(entry.change_pct - 25.0) < 1e-6


def test_snapshot_invalid_without_prev_close():
    entry = MassiveClient._parse_snapshot_entry({
        "ticker": "CCC",
        "day": {"c": 2.5},
        "lastTrade": {"p": 2.5},
    })
    assert entry.is_valid is False


def test_bar_parse_defaults():
    bar = MassiveClient._parse_bar({"t": 100, "c": 1.5})
    assert bar.t_ms == 100 and bar.c == 1.5 and bar.v == 0.0
