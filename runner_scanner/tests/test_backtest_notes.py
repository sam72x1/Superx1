"""اختبارات ملاحظات الباكتيست (backtest_notes) — قاعدية بلا شبكة/Claude."""

from __future__ import annotations

from runner_scanner import backtest, backtest_notes
from runner_scanner.config import Config
from runner_scanner.tests.test_backtest import MockBase

CFG = Config(massive_api_key="x", anthropic_api_key="",   # بلا Claude → قاعدي
             backtest_grid_min_decisive=8)


def _res_with_funnel(**funnel):
    res = backtest.BacktestResult(start="2026-05-01", end="2026-06-01", days=20)
    res.funnel = backtest.new_funnel()
    res.funnel.update(funnel)
    return res


def test_notes_warns_small_sample():
    res = _res_with_funnel(considered=100, alerts=3)
    res.trades = [{"result": "win", "max_gain_pct": 10},
                  {"result": "win", "max_gain_pct": 12},
                  {"result": "loss", "max_gain_pct": 2}]
    note = backtest_notes.build_notes(CFG, res)
    assert "ملاحظات الباكتيست" in note
    assert "العيّنة صغيرة" in note            # 2 محسومة < 8


def test_notes_flags_rvol_as_legitimate():
    res = _res_with_funnel(considered=200, no_5min=10, no_trigger=40,
                           rejected=145, alerts=5,
                           reject_reasons={"RVol": 120, "فلوت": 25})
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 5
    note = backtest_notes.build_notes(CFG, res)
    assert "أكثر بوّابة ترفض" in note and "RVol" in note
    # بعد إصلاح المسح المتكرّر: الرفض مشروع لا artifact منهجي
    assert "مشروع" in note and "قياس الظل" in note
    assert "قيد منهجي" not in note           # النص القديم الخاطئ أُزيل


def test_notes_reports_biggest_leak():
    res = _res_with_funnel(considered=200, no_5min=5, no_trigger=150,
                           rejected=40, alerts=5)
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 5
    note = backtest_notes.build_notes(CFG, res)
    assert "أكبر تسرّب" in note and "إغلاق 5د" in note


def test_notes_includes_grid_best():
    res = _res_with_funnel(considered=50, alerts=10)
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 8 + \
                 [{"result": "loss", "max_gain_pct": 1}] * 2
    grid = {"best": {"env": "TECH_READINESS_MIN", "value": 65.0,
                     "win_rate": 70.0, "baseline_win_rate": 62.0}}
    note = backtest_notes.build_notes(CFG, res, grid=grid)
    assert "TECH_READINESS_MIN=65" in note


def test_notes_end_to_end_from_real_backtest():
    res = backtest.run_backtest(
        Config(massive_api_key="x", anthropic_api_key="", trigger_change_pct=10.0),
        MockBase(), "2026-06-26", "2026-06-26")
    note = backtest_notes.build_notes(CFG, res)
    assert "ملاحظات الباكتيست" in note
