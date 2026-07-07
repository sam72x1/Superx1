"""اختبارات معايرة العتبات A/B (backtest_grid) — بعميل أساس وهمي (بلا شبكة).

يتحقّق من: المذكّر يشارك الجلب (نداء شبكي واحد عبر تمريرات متعدّدة) ·
تشغيل الشبكة end-to-end · التقرير يحوي أسماء متغيّرات البيئة + تنويه «لا تطبيق».
"""

from __future__ import annotations

from runner_scanner import backtest_grid
from runner_scanner.config import Config
from runner_scanner.tests.test_backtest import MockBase


def test_memo_client_shares_fetch():
    """نداءان متطابقان عبر المذكّر = جلب أساس واحد فقط."""
    calls = {"n": 0}

    class Counting(MockBase):
        def grouped_daily(self, date):
            calls["n"] += 1
            return super().grouped_daily(date)

    memo = backtest_grid.memoized(Counting())
    memo.grouped_daily("2026-06-26")
    memo.grouped_daily("2026-06-26")
    assert calls["n"] == 1                    # الثاني من الكاش


def test_memo_client_idempotent_wrap():
    memo = backtest_grid.memoized(MockBase())
    assert backtest_grid.memoized(memo) is memo   # لا يلفّ مرتين


def test_run_grid_end_to_end():
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_grid_readiness=(60.0, 65.0),
                 backtest_grid_float_max=(40_000_000, 60_000_000),
                 backtest_grid_parabolic=(120.0,))
    grid = backtest_grid.run_grid(cfg, MockBase(), "2026-06-26", "2026-06-26")
    assert grid["days"] == 1
    assert "baseline" in grid
    # ثلاثة محاور (جاهزية/فلوت/بارابولِك)
    assert [a["env"] for a in grid["axes"]] == [
        "TECH_READINESS_MIN", "FLOAT_MAX", "PARABOLIC_DAY_CHANGE_PCT"]
    # محور الجاهزية له قيمتان مجرَّبتان
    rd_axis = grid["axes"][0]
    assert len(rd_axis["variants"]) == 2
    # قيمة الأساس (65) مُعلَّمة
    assert any(v["is_baseline"] for v in rd_axis["variants"])


def test_run_grid_shares_network_across_variants():
    """شبكة كاملة (عدة تركيبات) = جلب grouped_daily واحد لليوم (مُذكّر)."""
    calls = {"n": 0}

    class Counting(MockBase):
        def grouped_daily(self, date):
            calls["n"] += 1
            return super().grouped_daily(date)

    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_grid_readiness=(55.0, 60.0, 65.0),
                 backtest_grid_float_max=(40_000_000, 60_000_000),
                 backtest_grid_parabolic=(120.0, 150.0))
    backtest_grid.run_grid(cfg, Counting(), "2026-06-26", "2026-06-26")
    # يوم واحد له تاريخان (اليوم + السابق) رغم ~6 تمريرات تركيبات
    assert calls["n"] == 2


def test_grid_report_has_env_vars_and_no_autoapply_notice():
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_grid_readiness=(55.0, 60.0),
                 backtest_grid_float_max=(40_000_000,),
                 backtest_grid_parabolic=(120.0,))
    grid = backtest_grid.run_grid(cfg, MockBase(), "2026-06-26", "2026-06-26")
    report = backtest_grid.format_grid_report(grid)
    assert "معايرة العتبات" in report
    assert "TECH_READINESS_MIN" in report
    assert "FLOAT_MAX" in report
    # تنويه الهوية: اقتراح لا تطبيق تلقائي
    assert "ما يغيّر شيئًا تلقائيًا" in report


def test_pick_best_requires_sufficient_sample():
    """تركيبة أفضل لكن عيّنتها أقل من الحدّ → لا تُقترح."""
    cfg = Config(massive_api_key="x", backtest_grid_min_decisive=99,
                 backtest_grid_min_edge=0.0)
    baseline = {"win_rate": 50.0}
    axes = [{"env": "TECH_READINESS_MIN", "title": "x", "variants": [
        {"value": 65.0, "is_baseline": False,
         "stats": {"win_rate": 90.0, "decisive": 3}}]}]
    assert backtest_grid._pick_best(axes, baseline, cfg) is None


def test_pick_best_requires_edge_over_baseline():
    """تحسّن أقل من الهامش → لا يُقترح (ضوضاء صغيرة)."""
    cfg = Config(massive_api_key="x", backtest_grid_min_decisive=1,
                 backtest_grid_min_edge=5.0)
    baseline = {"win_rate": 60.0}
    axes = [{"env": "FLOAT_MAX", "title": "x", "variants": [
        {"value": 60_000_000, "is_baseline": False,
         "stats": {"win_rate": 62.0, "decisive": 20}}]}]   # +2 < 5
    assert backtest_grid._pick_best(axes, baseline, cfg) is None


def test_pick_best_picks_highest_qualifying():
    cfg = Config(massive_api_key="x", backtest_grid_min_decisive=5,
                 backtest_grid_min_edge=3.0)
    baseline = {"win_rate": 60.0}
    axes = [{"env": "TECH_READINESS_MIN", "title": "الجاهزية", "variants": [
        {"value": 65.0, "is_baseline": False,
         "stats": {"win_rate": 68.0, "decisive": 10}},
        {"value": 70.0, "is_baseline": False,
         "stats": {"win_rate": 72.0, "decisive": 8}}]}]
    best = backtest_grid._pick_best(axes, baseline, cfg)
    assert best is not None and best["value"] == 70.0   # الأعلى المؤهَّل
