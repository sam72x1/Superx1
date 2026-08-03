"""اختبارات قياس Kronos Shadow وتقريره."""

from datetime import datetime, timezone

from runner_scanner.kronos_metrics import (
    flatten_kronos_rows,
    format_kronos_report,
    summarize_kronos,
)
from runner_scanner.state import Store


def test_summary_measures_direction_and_error_by_horizon():
    rows = [
        {"status": "ok", "completed_at": "x", "model_revision": "rev-1",
         "returns": {6: 2.0, 12: -1.0}, "actuals": {6: 1.0, 12: -3.0}},
        {"status": "ok", "completed_at": "x", "model_revision": "rev-1",
         "returns": {6: 1.0, 12: 2.0}, "actuals": {6: -1.0, 12: 4.0}},
        {"status": "error", "completed_at": None, "model_revision": "",
         "returns": {}, "actuals": {}},
    ]

    summary = summarize_kronos(rows)

    assert summary["total_forecasts"] == 3
    assert summary["forecasts"] == 2
    assert summary["ok"] == 2
    assert summary["errors"] == 0
    assert summary["total_errors"] == 1
    assert summary["total_skipped"] == 0
    assert summary["active_model_revision"] == "rev-1"
    assert summary["horizons"][6]["directional_accuracy_pct"] == 50.0
    assert summary["horizons"][6]["baseline_accuracy_pct"] == 50.0
    assert summary["horizons"][6]["directional_lift_pp"] == 0.0
    assert summary["horizons"][6]["mae_pct_points"] == 1.5
    assert summary["horizons"][12]["directional_accuracy_pct"] == 100.0
    assert summary["horizons"][12]["directional_lift_pp"] == 50.0
    assert summary["horizons"][12]["mae_pct_points"] == 2.0


def test_summary_ignores_missing_and_non_finite_actuals():
    rows = [{
        "status": "ok", "completed_at": "x", "model_revision": "rev-1",
        "returns": {6: 1.0, 12: 2.0, 18: float("nan")},
        "actuals": {6: None, 12: float("inf"), 18: 1.0},
    }]

    assert summarize_kronos(rows)["horizons"] == {}


def test_summary_uses_newest_successful_revision_without_mixing_old_model():
    rows = [
        # fetch يعيد الأحدث أولًا؛ هذي هي النسخة النشطة.
        {"status": "ok", "completed_at": "x", "model_revision": "rev-new",
         "returns": {6: 2.0}, "actuals": {6: 1.0}},
        {"status": "ok", "completed_at": "x", "model_revision": "rev-old",
         "returns": {6: 2.0}, "actuals": {6: -1.0}},
        {"status": "ok", "completed_at": "x", "model_revision": "rev-old",
         "returns": {6: 2.0}, "actuals": {6: -1.0}},
        {"status": "error", "completed_at": None, "model_revision": "",
         "returns": {}, "actuals": {}},
    ]

    summary = summarize_kronos(rows)

    assert summary["active_model_revision"] == "rev-new"
    assert summary["model_revisions"] == ["rev-new", "rev-old"]
    assert summary["forecasts"] == 1
    assert summary["horizons"][6]["samples"] == 1
    assert summary["horizons"][6]["directional_accuracy_pct"] == 100.0


def test_summary_does_not_mix_same_model_revision_across_experiments():
    rows = [
        {"status": "ok", "completed_at": "x", "experiment_id": "new-exp",
         "model_revision": "same-rev", "tokenizer_revision": "tok-new",
         "returns": {6: 1.0}, "actuals": {6: 2.0}},
        {"status": "ok", "completed_at": "x", "experiment_id": "old-exp",
         "model_revision": "same-rev", "tokenizer_revision": "tok-old",
         "returns": {6: 1.0}, "actuals": {6: -2.0}},
    ]

    summary = summarize_kronos(rows)

    assert summary["active_experiment_id"] == "new-exp"
    assert summary["forecasts"] == 1
    assert summary["horizons"][6]["samples"] == 1
    assert summary["horizons"][6]["directional_accuracy_pct"] == 100.0


def test_summary_separates_sessions_and_keeps_legacy_rows_compatible():
    rows = [
        {"status": "ok", "completed_at": "x", "model_revision": "rev-2",
         "session": "رسمي", "returns": {6: 1.0}, "actuals": {6: 2.0}},
        {"status": "ok", "completed_at": "x", "model_revision": "rev-2",
         "session": "بريماركت", "returns": {6: 1.0}, "actuals": {6: -2.0}},
        {"status": "ok", "completed_at": "x", "model_revision": "rev-2",
         "returns": {6: -1.0}, "actuals": {6: -2.0}},
    ]

    summary = summarize_kronos(rows)

    assert summary["horizons"] == {}  # لا رقم إجمالي يخلط ثلاث جلسات
    assert set(summary["sessions"]) == {"رسمي", "بريماركت", "غير محددة"}
    assert summary["sessions"]["رسمي"]["horizons"][6][
        "directional_accuracy_pct"] == 100.0
    assert summary["sessions"]["بريماركت"]["horizons"][6][
        "directional_accuracy_pct"] == 0.0
    assert summary["sessions"]["غير محددة"]["horizons"][6]["samples"] == 1


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def fetch_kronos_forecasts(self, limit):
        assert limit == 10_000
        return self.rows


class _WindowStore(_Store):
    def fetch_kronos_evaluation_window(self, trading_days):
        assert trading_days == 30
        return self.rows

    def fetch_kronos_forecasts(self, limit):
        raise AssertionError("التقرير يجب أن يستخدم نافذة الأيام الكاملة")


def test_report_uses_complete_recent_trading_day_window_when_store_supports_it():
    text = format_kronos_report(_WindowStore([{
        "status": "ok", "completed_at": "x", "model_revision": "rev-1",
        "returns": {6: 1.0}, "actuals": {6: 2.0},
    }]))

    assert "آخر 30 يوم تداول مسجّل" in text


def test_report_streams_real_store_window_and_resolves_active_experiment():
    store = Store(":memory:")
    store.save_kronos_forecast(
        "AAA",
        datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc),
        status="ok",
        experiment_id="exp-real",
        model_revision="rev-real",
        returns={6: 1.0},
        base_close=10.0,
    )

    text = format_kronos_report(store)

    assert "exp-real" in text
    assert "rev-real" in text
    assert "آخر 30 يوم تداول مسجّل" in text
    store.close()


def test_report_resolves_active_and_latest_from_the_same_stream_snapshot():
    class _InsertWhenReadStore(Store):
        def fetch_kronos_evaluation_window(self, trading_days):
            def stream():
                # تحاكي نجاحًا يصل بعد إنشاء generator وقبل أول قراءة منه.
                self.save_kronos_forecast(
                    "NEW",
                    datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
                    status="ok",
                    experiment_id="exp-new",
                    model_revision="rev-new",
                    returns={6: 2.0},
                    base_close=20.0,
                )
                yield from Store.fetch_kronos_evaluation_window(
                    self, trading_days=trading_days
                )

            return stream()

        def fetch_kronos_active_record(self, trading_days=30):
            raise AssertionError("لا يجوز استعمال snapshot منفصلة للتجربة النشطة")

        def fetch_kronos_latest_record(self):
            raise AssertionError("لا يجوز استعمال snapshot منفصلة لأحدث حالة")

    store = _InsertWhenReadStore(":memory:")
    store.save_kronos_forecast(
        "OLD",
        datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc),
        status="ok",
        experiment_id="exp-old",
        model_revision="rev-old",
        returns={6: 1.0},
        base_close=10.0,
    )

    text = format_kronos_report(store)

    assert "exp-new" in text
    assert "rev-new" in text
    assert "rev-old" not in text
    assert "سجلات القياس (نافذة آخر 30 يوم تداول مسجّل): 2" in text
    store.close()


def test_report_explains_small_sample_and_shadow_safety():
    text = format_kronos_report(_Store([{
        "status": "ok", "completed_at": "x", "model_revision": "rev-1",
        "returns": {6: 1.0}, "actuals": {6: 2.0},
    }]), min_sample=20)

    assert "30د" in text
    assert "100.0%" in text
    assert "لا حكم بعد" in text
    assert "لا ترقية بعد" in text
    assert "لا ينفّذ ولا يفلتر" in text


def test_report_exposes_queue_pressure_and_sampling_warning():
    text = format_kronos_report(
        _Store([]),
        runtime_stats={
            "enqueued": 8,
            "queue_full": 2,
            "audit_duplicate": 3,
            "audit_dropped": 1,
            "stale_before_fetch": 1,
            "save_failed": 4,
            "queue_depth": 3,
            "queue_capacity": 16,
            "audit_queue_depth": 1,
            "audit_queue_capacity": 16,
        },
    )

    assert "أُدرج 8" in text
    assert "امتلاء الطابور 2" in text
    assert "تدقيق مكرر 3" in text
    assert "تدقيق فائت 1" in text
    assert "قديم قبل الجلب 1" in text
    assert "فشل حفظ 4" in text
    assert "3/16" in text
    assert "تدقيق 1/16" in text


def test_report_shows_active_revision_sessions_and_escapes_dynamic_html():
    text = format_kronos_report(_Store([
        {"status": "ok", "completed_at": "x",
         "model_revision": "rev<&new", "session": "رسمي<script>",
         "returns": {6: 1.0}, "actuals": {6: 2.0}},
        {"status": "ok", "completed_at": "x",
         "model_revision": "rev-old", "session": "بريماركت",
         "returns": {6: -1.0}, "actuals": {6: 2.0}},
    ]))

    assert "rev&lt;&amp;new" in text
    assert "رسمي&lt;script&gt;" in text
    assert "rev-old" not in text
    assert "أقل من 200" in text              # حد العينة الافتراضي الجديد
    assert "أقل من 20" in text


def test_report_keeps_newest_failure_visible_beside_older_success():
    text = format_kronos_report(_Store([
        {"status": "error", "error": "proxy <down>", "created_at": "new",
         "model_revision": "", "returns": {}, "actuals": {}},
        {"status": "ok", "completed_at": "x", "created_at": "old",
         "model_revision": "rev-1", "returns": {6: 1.0},
         "actuals": {6: 2.0}, "trade_date": "2026-07-01"},
    ]))

    assert "سجلات القياس" in text
    assert "أخطاء: 1" in text
    assert "أحدث سجل تشغيلي: خطأ" in text
    assert "proxy &lt;down&gt;" in text
    assert "rev-1" in text


def test_latest_operational_status_uses_created_at_not_forecast_asof_order():
    rows = [
        {"status": "ok", "asof_at": "2026-07-01T10:05:00+00:00",
         "created_at": "2026-07-01T10:05:01+00:00", "model_revision": "rev",
         "returns": {6: 1.0}, "actuals": {}},
        {"status": "skipped", "asof_at": "2026-07-01T09:55:00+00:00",
         "created_at": "2026-07-01T10:06:00+00:00", "error": "queue lag",
         "model_revision": "", "returns": {}, "actuals": {}},
    ]

    summary = summarize_kronos(rows)

    assert summary["active_model_revision"] == "rev"
    assert summary["latest_status"] == "skipped"
    assert summary["latest_error"] == "queue lag"


def test_trading_days_are_counted_independently_per_horizon():
    rows = [
        {"status": "ok", "model_revision": "rev-1", "trade_date": "2026-07-01",
         "returns": {6: 1.0, 12: 1.0}, "actuals": {6: 1.0, 12: 1.0}},
        {"status": "ok", "model_revision": "rev-1", "trade_date": "2026-07-02",
         "returns": {6: 1.0, 12: 1.0}, "actuals": {6: 1.0}},
    ]

    horizons = summarize_kronos(rows)["horizons"]

    assert horizons[6]["trading_days"] == 2
    assert horizons[12]["trading_days"] == 1


def test_flattened_export_has_one_analysis_row_per_horizon():
    rows = flatten_kronos_rows([{
        "ticker": "AAA", "status": "ok", "experiment_id": "exp-1",
        "model_revision": "rev-1", "kronos_revision": "source-1",
        "client_revision": "client-1", "device": "cpu", "max_context": 512,
        "returns": {6: 2.0, 12: -1.0}, "actuals": {6: 1.0, 12: 3.0},
        "actual_observed_at": {6: "2026-07-01T14:30:10+00:00"},
        "latency_ms": 120.0, "service_latency_ms": 100.0,
    }])

    assert [row["horizon_minutes"] for row in rows] == [30, 60]
    assert rows[0]["direction_hit"] == 1
    assert rows[0]["always_up_baseline_hit"] == 1
    assert rows[0]["absolute_error_pct_points"] == 1.0
    assert rows[0]["experiment_id"] == "exp-1"
    assert rows[0]["kronos_revision"] == "source-1"
    assert rows[0]["client_revision"] == "client-1"
    assert rows[0]["device"] == "cpu"
    assert rows[0]["max_context"] == 512
    assert rows[0]["actual_observed_at"] == "2026-07-01T14:30:10+00:00"
    assert rows[1]["direction_hit"] == 0
    assert rows[1]["always_up_baseline_hit"] == 1
