"""تخزين توقعات Kronos التجريبية مستقلًا عن سجل القرار."""

from datetime import datetime, timezone
import sqlite3

import pytest

from runner_scanner.state import Store


_EARLY_KRONOS_SCHEMA = """
CREATE TABLE {table} (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, asof_at TEXT NOT NULL,
    status TEXT, model TEXT, model_revision TEXT NOT NULL DEFAULT '',
    tokenizer_revision TEXT, lookback INTEGER, pred_len INTEGER,
    returns_json TEXT NOT NULL DEFAULT '{{}}', latency_ms REAL,
    error TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (ticker, asof_at, model_revision)
)
"""


def test_legacy_kronos_primary_key_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE kronos_forecasts (
            ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
            asof_at TEXT NOT NULL, status TEXT NOT NULL, model TEXT,
            model_revision TEXT NOT NULL DEFAULT '', tokenizer_revision TEXT,
            lookback INTEGER, pred_len INTEGER,
            returns_json TEXT NOT NULL DEFAULT '{}', latency_ms REAL,
            error TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY (ticker, asof_at, model_revision)
        )
        """
    )
    conn.execute(
        "INSERT INTO kronos_forecasts VALUES "
        "('ABCD','2026-07-29','2026-07-29T15:30:00+00:00','ok','model',"
        "'rev-1','tok-1',256,18,'{\"6\":1.0}',10.0,'','2026-07-29T15:30:01+00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(str(path))

    rows = store.fetch_kronos_forecasts()
    assert len(rows) == 1
    assert rows[0]["returns"] == {6: 1.0}
    assert rows[0]["experiment_id"]
    pk = store._conn.execute(  # noqa: SLF001 — نتحقق من ترحيل مخطط SQLite
        "PRAGMA table_info(kronos_forecasts)"
    ).fetchall()
    assert [row["name"] for row in sorted(pk, key=lambda row: row["pk"])
            if row["pk"]] == ["ticker", "asof_at", "experiment_id"]
    store.close()


def test_failed_primary_key_migration_rolls_back_without_orphaning_rows(tmp_path):
    path = tmp_path / "failed-migration.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(_EARLY_KRONOS_SCHEMA.format(table="kronos_forecasts"))
    conn.execute(
        "INSERT INTO kronos_forecasts VALUES "
        "('ABCD','2026-07-29','2026-07-29T15:30:00+00:00',NULL,'model',"
        "'rev-1','tok-1',256,18,'{\"6\":1.0}',10.0,'',"
        "'2026-07-29T15:30:01+00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(str(path))
    assert store.kronos_available is False
    assert "NOT NULL" in store.kronos_error
    store.close()

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM kronos_forecasts").fetchone()[0] == 1
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='kronos_forecasts_legacy'"
    ).fetchone() is None
    conn.close()


def test_interrupted_empty_rebuild_recovers_legacy_table(tmp_path):
    path = tmp_path / "interrupted-migration.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(_EARLY_KRONOS_SCHEMA.format(table="kronos_forecasts_legacy"))
    conn.execute(
        "INSERT INTO kronos_forecasts_legacy VALUES "
        "('ABCD','2026-07-29','2026-07-29T15:30:00+00:00','ok','model',"
        "'rev-1','tok-1',256,18,'{\"6\":1.0}',10.0,'',"
        "'2026-07-29T15:30:01+00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(str(path))

    rows = store.fetch_kronos_forecasts()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCD"
    assert not store._conn.execute(  # noqa: SLF001 — تحقق من اكتمال التعافي
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='kronos_forecasts_legacy'"
    ).fetchone()
    store.close()


def test_ambiguous_kronos_tables_disable_shadow_without_blocking_core(tmp_path):
    path = tmp_path / "ambiguous-kronos.sqlite3"
    conn = sqlite3.connect(path)
    for table, ticker in (
        ("kronos_forecasts", "CURRENT"),
        ("kronos_forecasts_legacy", "LEGACY"),
    ):
        conn.execute(_EARLY_KRONOS_SCHEMA.format(table=table))
        conn.execute(
            f"INSERT INTO {table} VALUES "
            "(?, '2026-07-29', '2026-07-29T15:30:00+00:00', 'ok', "
            "'model', 'rev-1', 'tok-1', 256, 18, '{\"6\":1.0}', 10.0, '', "
            "'2026-07-29T15:30:01+00:00')",
            (ticker,),
        )
    conn.commit()
    conn.close()

    store = Store(str(path))

    assert store.kronos_available is False
    assert store.kronos_error
    store.mark_alerted(
        "CORE", 80.0,
        datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc),
    )
    assert store.already_alerted(
        "CORE", datetime(2026, 7, 29, 15, 31, tzinfo=timezone.utc)
    )
    assert store.fetch_kronos_forecasts() == []
    store.close()


def test_kronos_name_collision_with_view_disables_shadow_but_keeps_core(tmp_path):
    path = tmp_path / "kronos-view.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE VIEW kronos_forecasts AS SELECT 1 AS x")
    conn.commit()
    conn.close()

    store = Store(str(path))

    assert store.kronos_available is False
    assert "ليس جدول" in store.kronos_error
    store.mark_alerted(
        "CORE", 75.0,
        datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc),
    )
    assert store.already_alerted(
        "CORE", datetime(2026, 7, 29, 15, 31, tzinfo=timezone.utc)
    )
    store.close()


def test_kronos_index_name_collision_disables_only_shadow(tmp_path):
    path = tmp_path / "kronos-index-collision.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE idx_kronos_forecasts_recent(x INTEGER)")
    conn.commit()
    conn.close()

    store = Store(str(path))

    assert store.kronos_available is False
    assert "اسم فهرس" in store.kronos_error
    store.mark_alerted(
        "CORE", 75.0,
        datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc),
    )
    assert store.already_alerted(
        "CORE", datetime(2026, 7, 29, 15, 31, tzinfo=timezone.utc)
    )
    store.close()


def test_incomplete_modern_kronos_table_disables_shadow_before_live_writes(tmp_path):
    path = tmp_path / "incomplete-modern.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE kronos_forecasts (
            ticker TEXT NOT NULL, trade_date TEXT NOT NULL, asof_at TEXT NOT NULL,
            experiment_id TEXT NOT NULL, session TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, model_revision TEXT NOT NULL DEFAULT '',
            returns_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            PRIMARY KEY (ticker, asof_at, experiment_id)
        )
        """
    )
    conn.commit()
    conn.close()

    store = Store(str(path))

    assert store.kronos_available is False
    assert "ناقص بعد الترحيل" in store.kronos_error
    assert store.fetch_kronos_forecasts() == []
    store.mark_alerted(
        "CORE", 75.0,
        datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc),
    )
    assert store.already_alerted(
        "CORE", datetime(2026, 7, 29, 15, 31, tzinfo=timezone.utc)
    )
    store.close()


def test_kronos_forecast_round_trip():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)

    requested = datetime(2026, 7, 29, 15, 30, 20, tzinfo=timezone.utc)
    received = datetime(2026, 7, 29, 15, 30, 22, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "abcd",
        asof,
        status="ok",
        experiment_id="exp-1",
        session="رسمي",
        model="NeoQuasar/Kronos-small",
        model_revision="rev-1",
        tokenizer="NeoQuasar/Kronos-Tokenizer-base",
        tokenizer_revision="tok-1",
        kronos_revision="source-1",
        service_revision="service-1",
        client_revision="client-1",
        market_timezone="America/New_York",
        device="cpu",
        max_context=512,
        observation_grace_min=3.0,
        lookback=512,
        pred_len=18,
        returns={6: 1.25, 12: -0.5, 18: 3.75},
        base_close=4.0,
        latency_ms=123.4,
        service_latency_ms=100.1,
        requested_at=requested,
        received_at=received,
    )

    rows = store.fetch_kronos_forecasts()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCD"
    assert rows[0]["status"] == "ok"
    assert rows[0]["model_revision"] == "rev-1"
    assert rows[0]["experiment_id"] == "exp-1"
    assert rows[0]["session"] == "رسمي"
    assert rows[0]["kronos_revision"] == "source-1"
    assert rows[0]["tokenizer"] == "NeoQuasar/Kronos-Tokenizer-base"
    assert rows[0]["service_revision"] == "service-1"
    assert rows[0]["client_revision"] == "client-1"
    assert rows[0]["market_timezone"] == "America/New_York"
    assert rows[0]["device"] == "cpu"
    assert rows[0]["max_context"] == 512
    assert rows[0]["observation_grace_min"] == 3.0
    assert rows[0]["requested_at"] == requested.isoformat()
    assert rows[0]["received_at"] == received.isoformat()
    assert rows[0]["returns"] == {6: 1.25, 12: -0.5, 18: 3.75}
    assert rows[0]["base_close"] == 4.0
    assert rows[0]["service_latency_ms"] == 100.1
    assert rows[0]["actuals"] == {}
    assert rows[0]["actual_observed_at"] == {}
    indexes = {
        row["name"] for row in store._conn.execute(  # noqa: SLF001
            "PRAGMA index_list(kronos_forecasts)"
        ).fetchall()
    }
    assert "idx_kronos_forecasts_pending" in indexes
    assert "idx_kronos_forecasts_recent" in indexes
    pending_plan = " ".join(
        row["detail"] for row in store._conn.execute(  # noqa: SLF001
            "EXPLAIN QUERY PLAN SELECT * FROM kronos_forecasts "
            "WHERE status='ok' AND completed_at IS NULL "
            "AND base_close IS NOT NULL"
        ).fetchall()
    )
    assert "idx_kronos_forecasts_pending" in pending_plan
    store.close()


def test_same_model_and_asof_is_idempotent():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)

    store.save_kronos_forecast(
        "ABCD", asof, status="error", model_revision="rev-1",
        experiment_id="exp-1", error="تعذّر مؤقتًا")
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        experiment_id="exp-1", returns={6: 2.0})

    rows = store.fetch_kronos_forecasts()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["returns"] == {6: 2.0}
    store.close()


def test_successful_forecast_is_immutable_against_later_downgrade():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", experiment_id="exp-1",
        model_revision="rev-1", returns={6: 2.0}, base_close=10.0)
    store.update_kronos_actuals(
        {"ABCD": 11.0},
        datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc))

    store.save_kronos_forecast(
        "ABCD", asof, status="skipped", experiment_id="exp-1",
        model_revision="rev-1", error="late")

    row = store.fetch_kronos_forecasts()[0]
    assert row["status"] == "ok"
    assert row["returns"] == {6: 2.0}
    assert row["base_close"] == 10.0
    assert row["actuals"] == {6: 10.0}
    assert row["completed_at"] is not None
    store.close()


def test_different_model_revisions_are_preserved():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)

    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1", returns={6: 1.0})
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-2", returns={6: 2.0})

    assert {row["model_revision"] for row in store.fetch_kronos_forecasts()} == {
        "rev-1", "rev-2"
    }
    store.close()


def test_evaluation_window_returns_every_row_from_latest_trading_days():
    store = Store(":memory:")
    for day in (1, 2, 3):
        asof = datetime(2026, 7, day, 15, 30, tzinfo=timezone.utc)
        for ticker in ("AAA", "BBB"):
            store.save_kronos_forecast(
                ticker,
                asof,
                status="ok",
                experiment_id=f"exp-{day}-{ticker}",
                model_revision="rev-1",
                returns={6: 1.0},
            )

    rows = list(store.fetch_kronos_evaluation_window(trading_days=2))

    assert len(rows) == 4
    assert {row["trade_date"] for row in rows} == {"2026-07-02", "2026-07-03"}
    store.close()


def test_same_model_revision_with_different_experiments_is_preserved():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)

    store.save_kronos_forecast(
        "ABCD", asof, status="ok", experiment_id="exp-a",
        model_revision="rev-1", tokenizer_revision="tok-a",
        lookback=256, pred_len=18, returns={6: 1.0})
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", experiment_id="exp-b",
        model_revision="rev-1", tokenizer_revision="tok-b",
        lookback=512, pred_len=18, returns={6: 2.0})

    rows = store.fetch_kronos_forecasts()
    assert {row["experiment_id"] for row in rows} == {"exp-a", "exp-b"}
    assert {row["tokenizer_revision"] for row in rows} == {"tok-a", "tok-b"}
    store.close()


def test_non_finite_or_invalid_returns_are_dropped():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)

    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: float("nan"), 12: float("inf"), -1: 2.0, 18: 3.0})

    assert store.fetch_kronos_forecasts()[0]["returns"] == {18: 3.0}
    store.close()


def test_actual_returns_are_captured_at_each_horizon():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0, 12: 2.0}, base_close=10.0)

    assert store.update_kronos_actuals(
        {"ABCD": 11.0},
        datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
    ) == 1
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {6: 10.0}
    assert store.fetch_kronos_forecasts()[0]["actual_observed_at"] == {
        6: "2026-07-29T16:00:00+00:00"
    }
    assert store.update_kronos_actuals(
        {"ABCD": 9.0},
        datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc),
    ) == 1
    row = store.fetch_kronos_forecasts()[0]
    assert row["actuals"] == {6: 10.0, 12: -10.0}
    assert row["actual_observed_at"] == {
        6: "2026-07-29T16:00:00+00:00",
        12: "2026-07-29T16:30:00+00:00",
    }
    assert row["completed_at"] is not None
    store.close()


def test_missed_observation_is_marked_unknown_not_backfilled_late():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0}, base_close=10.0)

    assert store.update_kronos_actuals(
        {"ABCD": 15.0},
        datetime(2026, 7, 29, 16, 10, tzinfo=timezone.utc),
        grace_min=3.0,
    ) == 1
    row = store.fetch_kronos_forecasts()[0]
    assert row["actuals"] == {6: None}
    assert row["actual_observed_at"] == {6: None}
    assert row["completed_at"] is not None
    store.close()


def test_missing_price_waits_until_observation_grace_expires():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0}, base_close=10.0)

    assert store.update_kronos_actuals(
        {}, datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
        grace_min=3.0,
    ) == 0
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {}
    assert store.update_kronos_actuals(
        {"ABCD": 10.5},
        datetime(2026, 7, 29, 16, 2, tzinfo=timezone.utc),
        grace_min=3.0,
    ) == 1
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {6: 5.0}
    store.close()


def test_open_forecast_keeps_its_original_observation_grace_after_restart():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0}, base_close=10.0, observation_grace_min=3.0,
    )
    observed = datetime(2026, 7, 29, 16, 2, tzinfo=timezone.utc)

    assert store.update_kronos_actuals(
        {"ABCD": 11.0}, observed,
        grace_min=0.5,  # إعداد العملية الجديدة لا يغيّر سياسة الصف القديم.
        observed_at_map={"ABCD": observed},
    ) == 1

    row = store.fetch_kronos_forecasts()[0]
    assert row["actuals"] == {6: 10.0}
    assert row["actual_observed_at"] == {6: observed.isoformat()}
    store.close()


def test_stale_snapshot_timestamp_is_not_recorded_as_horizon_truth():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0}, base_close=10.0)
    stale_at = datetime(2026, 7, 29, 15, 55, tzinfo=timezone.utc)

    assert store.update_kronos_actuals(
        {"ABCD": 15.0},
        datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
        observed_at_map={"ABCD": stale_at},
    ) == 0
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {}
    assert store.update_kronos_actuals(
        {"ABCD": 15.0},
        datetime(2026, 7, 29, 16, 4, tzinfo=timezone.utc),
        observed_at_map={"ABCD": stale_at},
    ) == 1
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {6: None}
    store.close()


def test_corrupt_success_row_cannot_crash_actual_capture():
    store = Store(":memory:")
    asof = datetime(2026, 7, 29, 15, 30, tzinfo=timezone.utc)
    store.save_kronos_forecast(
        "ABCD", asof, status="ok", model_revision="rev-1",
        returns={6: 1.0}, base_close=10.0,
    )
    store._conn.execute(  # noqa: SLF001 — محاكاة فساد قرص/ترحيل قديم
        "UPDATE kronos_forecasts SET base_close='bad'"
    )
    store._conn.commit()  # noqa: SLF001

    assert store.update_kronos_actuals(
        {"ABCD": 11.0},
        datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
    ) == 0
    assert store.fetch_kronos_forecasts()[0]["actuals"] == {}
    store.close()
