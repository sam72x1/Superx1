"""التخزين: منع تكرار + closed-loop + تتبّع نتائج وأحداث المتابعة.

SQLite على قرص دائم:
1. منع تكرار التنبيه (تنبيه/سهم/يوم) — يُعاد تحميله عند الإقلاع.
2. صفّ واحد لكل سهم/يوم يحمل تحليله + نتيجته (للمعايرة وأداة التطوير).
3. تتبّع نتائج وأحداث: نتابع سعر كل مرشّح من السنابشوت (بلا API إضافي)،
   ونصدر **أحداث متابعة** للمُنبَّه عنها: 🎯 تحقيق هدف · ⛔ كسر الوقف ·
   🚀 قفزة قوية جديدة. مزيلة التكرار (تُحفظ حالة الإشعار في DB).

«النتيجة» (result) لأداة التطوير: win (بلغ هدفًا) · loss (ضرب الوقف) ·
timeout (انتهت النافذة بلا حسم). «الحالة» (outcome): open/closed (دورة حياة
التتبّع — تبقى مفتوحة لإصدار أحداث أهداف لاحقة حتى الوقف/النافذة).

⚠️ يعتمد على القرص الدائم (درس CCXI، القسم 14).
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Candidate, Session

logger = logging.getLogger(__name__)


class KronosMigrationError(RuntimeError):
    """فساد/التباس محصور في مخطط Shadow ولا يبرر إسقاط مخزن core."""

# يوم التداول يُحسب بتوقيت نيويورك (السوق ET) لتجنّب اختلاف التاريخ قرب
# منتصف الليل UTC بين تسجيل المرشّح وتحديث نتيجته.
_ET = ZoneInfo("America/New_York")

# توريث أبطال الفترة: أي فترة ترث أبطال (الفترة السابقة، إزاحة الأيام).
#   بري ← افتر أمس · رسمي ← بري اليوم · افتر ← رسمي اليوم
_CHAMP_INHERIT = {
    Session.PREMARKET.value: (Session.AFTERHOURS.value, -1),
    Session.REGULAR.value: (Session.PREMARKET.value, 0),
    Session.AFTERHOURS.value: (Session.REGULAR.value, 0),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    alerted_at  TEXT NOT NULL,
    score       REAL,
    PRIMARY KEY (ticker, trade_date)
);
CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS session_champions (
    session     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    change_pct  REAL,
    price       REAL,
    rank        INTEGER,
    PRIMARY KEY (session, trade_date, symbol)
);
CREATE TABLE IF NOT EXISTS tracking (
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    first_seen_at   TEXT,
    logged_at       TEXT,
    session         TEXT,
    -- جلسة أول رصد (لا تُحدَّث أبدًا). session تتغيّر مع كل إعادة تقييم لأن
    -- منطق إعادة-التأسيس يعتمد عليها — فتحليل «حسب الجلسة» يعتمد هذا العمود.
    first_session   TEXT,
    change_pct      REAL,
    score           REAL,
    momentum        REAL,
    readiness       REAL,
    rvol            REAL,
    rvol_5min       REAL,
    float_shares    REAL,
    float_source    TEXT,
    halt_state      TEXT,
    had_news        INTEGER,
    rejected        INTEGER,
    reject_reason   TEXT,
    reason_code     TEXT,                  -- كود رفض ثابت (DEBT-13): تصنيف آلي لا نصّ
    -- بيانات إضافية لتشريح الفشل (لماذا فشل السهم)
    short_pct       REAL,
    dilution_risk   TEXT,
    analyst_dir     TEXT,
    catalyst_head   TEXT,
    -- تتبّع النتيجة + الأحداث
    is_alert        INTEGER DEFAULT 0,
    first_price     REAL,
    -- سعر دخول البطاقة عند أوّل تنبيه (BUG-32): تُقاس النتيجة منه لا من
    -- first_price (سعر أول رصد قد يسبق التنبيه بساعات وبطبعة رقيقة، فوقفٌ فوقه
    -- يُطلق hit_stop زائفًا). NULL للمرفوضين وصفوف ما قبل الإصلاح.
    entry_price     REAL,
    -- لحظة أوّل تنبيه (BUG-39): نافذة النتيجة تُقاس منها لا من first_seen_at.
    -- سهم يُرصد 9:35 مرفوضًا ثم يُنبَّه 11:15 كانت نافذته منتهية سلفًا فيُغلق
    -- timeout فورًا ⇒ لا تصلك «بلغ الهدف» ولا «كسر الوقف» وأنت ممسك بالصفقة.
    alerted_at      TEXT,
    first_volume    REAL,                 -- حجم وقت أول رصد (لقياس المشاركة)
    stop_price      REAL,
    target1         REAL,
    target2         REAL,
    target3         REAL,
    high_after      REAL,
    low_after       REAL,
    max_gain_pct    REAL DEFAULT 0,
    max_draw_pct    REAL DEFAULT 0,
    hit_target      INTEGER DEFAULT 0,
    hit_stop        INTEGER DEFAULT 0,
    notified_targets INTEGER DEFAULT 0,
    notified_stop   INTEGER DEFAULT 0,
    notified_high   REAL,
    notified_missed INTEGER DEFAULT 0,    -- نبّهنا عن فرصة فائتة (مرفوض صعد)
    result          TEXT DEFAULT '',       -- win / loss / timeout (للتطوير)
    outcome         TEXT DEFAULT 'open',    -- open / closed (دورة حياة التتبّع)
    closed_at       TEXT,
    -- ترتيب القمة/القاع (لحسم سؤال الفرص الفائتة: هل سُتوقَف قبل القمة؟)
    peak_at         TEXT,                  -- آخر طابع تحسّنت فيه القمة
    stop_dist_at    TEXT,                  -- أول طابع لُمست فيه مسافة الوقف من الدخول
    PRIMARY KEY (ticker, trade_date)
);
-- صفقاتك **الفعلية** (الحلقة المغلقة): البوت كان يعرف ما اقترحه ولا يعرف ما
-- فعلتَه أنت — فكل تحليلاته عن أداء الاقتراحات لا عن أدائك. هذا الجدول يغلق
-- الفجوة: كم دخلت، بأي سعر، ومتى خرجت. يُملأ يدويًّا بأمر تيليجرام؛ فارغ =
-- لا شيء يتغيّر (كل التحليلات القائمة تبقى كما هي).
CREATE TABLE IF NOT EXISTS my_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    shares      REAL NOT NULL,
    entry       REAL NOT NULL,
    exit_price  REAL,                  -- NULL = الصفقة ما زالت مفتوحة
    closed_at   TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_my_trades_open
    ON my_trades(ticker, exit_price);
"""

_KRONOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kronos_forecasts (
    ticker                 TEXT NOT NULL,
    trade_date              TEXT NOT NULL,
    asof_at                 TEXT NOT NULL,
    experiment_id           TEXT NOT NULL,
    session                 TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL,
    model                   TEXT,
    model_revision          TEXT NOT NULL DEFAULT '',
    tokenizer               TEXT,
    tokenizer_revision      TEXT,
    kronos_revision         TEXT,
    service_revision        TEXT,
    client_revision         TEXT,
    market_timezone         TEXT,
    device                  TEXT,
    max_context             INTEGER,
    observation_grace_min   REAL,
    lookback                INTEGER,
    pred_len                INTEGER,
    returns_json            TEXT NOT NULL DEFAULT '{}',
    base_close              REAL,
    actuals_json            TEXT NOT NULL DEFAULT '{}',
    actual_observed_at_json TEXT NOT NULL DEFAULT '{}',
    latency_ms              REAL,
    service_latency_ms      REAL,
    error                   TEXT,
    requested_at            TEXT,
    received_at             TEXT,
    created_at              TEXT NOT NULL,
    completed_at            TEXT,
    PRIMARY KEY (ticker, asof_at, experiment_id)
);
"""


# أعمدة tracking عند أول شحن للجدول (dd7dd34) — مجمّدة كمرجع الاتّساق الذاتي.
# لا تعدّلها: كل عمود في _SCHEMA يجب أن يكون هنا أو في _MIGRATIONS، وإلا فاته
# الترحيل على قاعدة قديمة (اختبار الاتّساق يمسك المنسيّ القادم للأبد).
_ORIGINAL_TRACKING_COLS = frozenset({
    "ticker", "trade_date", "first_seen_at", "logged_at", "session",
    "change_pct", "score", "momentum", "readiness", "rvol", "rvol_5min",
    "float_shares", "float_source", "halt_state", "had_news", "rejected",
    "reject_reason", "is_alert", "first_price", "stop_price", "target1",
    "high_after", "low_after", "max_gain_pct", "max_draw_pct", "hit_target",
    "hit_stop", "outcome", "closed_at",
})

# ترحيل الأعمدة المضافة بعد شحن الجدول — كل عمود جديد يُضاف هنا **و** لـ_SCHEMA.
# القرص دائم فـ CREATE TABLE IF NOT EXISTS لا يفعل شيئًا على قاعدة قائمة؛ بلا
# هذا السطر يرمي INSERT «no such column» كل دورة ويعمى البوت صامتًا (§7 · BUG-01).
_MIGRATIONS = (
    ("short_pct", "REAL"), ("dilution_risk", "TEXT"),
    ("analyst_dir", "TEXT"), ("catalyst_head", "TEXT"),
    ("notified_missed", "INTEGER DEFAULT 0"),
    ("first_volume", "REAL"), ("first_session", "TEXT"),
    ("peak_at", "TEXT"), ("stop_dist_at", "TEXT"),
    # ── الستة التي فاتها الترحيل (دخلت _SCHEMA في 5a9ab43 بعد شحن الجدول) ──
    ("target2", "REAL"), ("target3", "REAL"),
    ("notified_targets", "INTEGER DEFAULT 0"),
    ("notified_stop", "INTEGER DEFAULT 0"),
    ("alerted_at", "TEXT"),   # BUG-39: مرساة نافذة النتيجة
    ("notified_high", "REAL"), ("result", "TEXT DEFAULT ''"),
    ("reason_code", "TEXT"),   # DEBT-13: كود الرفض الثابت
    ("entry_price", "REAL"),   # BUG-32: سعر دخول البطاقة (أساس قياس النتيجة الصادق)
)

# حقول قياس Kronos أُضيفت بعد إنشاء جدول Shadow الأولي أثناء التطوير. إبقاؤها
# كترحيل صريح يجعل قواعد المطوّرين/الأقراص الدائمة آمنة عند الترقية الجزئية.
_KRONOS_MIGRATIONS = (
    ("experiment_id", "TEXT NOT NULL DEFAULT ''"),
    ("session", "TEXT NOT NULL DEFAULT ''"),
    ("tokenizer", "TEXT"),
    ("kronos_revision", "TEXT"),
    ("service_revision", "TEXT"),
    ("client_revision", "TEXT"),
    ("market_timezone", "TEXT"),
    ("device", "TEXT"),
    ("max_context", "INTEGER"),
    ("observation_grace_min", "REAL"),
    ("base_close", "REAL"),
    ("actuals_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("actual_observed_at_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("completed_at", "TEXT"),
    ("service_latency_ms", "REAL"),
    ("requested_at", "TEXT"),
    ("received_at", "TEXT"),
)


def _tracking_schema_columns() -> set[str]:
    """أسماء أعمدة جدول tracking كما هي في _SCHEMA (لاختبار الاتّساق الذاتي)."""
    body = _SCHEMA.split("CREATE TABLE IF NOT EXISTS tracking", 1)[1]
    body = body.split("(", 1)[1].split("PRIMARY KEY", 1)[0]
    cols = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        first = line.split()[0]
        if first.isidentifier():
            cols.add(first)
    return cols


def trade_date_str(now: datetime | None = None) -> str:
    """تاريخ يوم التداول (بتوقيت ET) كمفتاح موحّد."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_ET).strftime("%Y-%m-%d")


def _iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _fallback_kronos_experiment_id(
    *, model: str, model_revision: str, tokenizer: str,
    tokenizer_revision: str, kronos_revision: str, service_revision: str,
    client_revision: str, market_timezone: str, device: str,
    max_context: int | None, observation_grace_min: float | None,
    lookback: int | None, pred_len: int | None,
    horizons: list[int],
) -> str:
    """هوية توافقية للكتابات المباشرة؛ العامل يمرّر هوية كاملة صراحةً."""
    body = json.dumps({
        "model": model,
        "model_revision": model_revision,
        "tokenizer": tokenizer,
        "tokenizer_revision": tokenizer_revision,
        "kronos_revision": kronos_revision,
        "service_revision": service_revision,
        "client_revision": client_revision,
        "market_timezone": market_timezone,
        "device": device,
        "max_context": max_context,
        "observation_grace_min": observation_grace_min,
        "lookback": lookback,
        "pred_len": pred_len,
        "horizons": sorted(horizons),
    }, ensure_ascii=True, allow_nan=False, sort_keys=True,
       separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


_KRONOS_MINIMUM_COLUMNS = frozenset({
    "ticker", "trade_date", "asof_at", "status", "model_revision",
    "returns_json", "created_at",
})
_KRONOS_CONTRACT_COLUMNS = frozenset({
    "ticker", "trade_date", "asof_at", "experiment_id", "session", "status",
    "model", "model_revision", "tokenizer", "tokenizer_revision",
    "kronos_revision", "service_revision", "client_revision",
    "market_timezone", "device", "max_context", "observation_grace_min",
    "lookback", "pred_len", "returns_json", "base_close", "actuals_json",
    "actual_observed_at_json", "latency_ms", "service_latency_ms", "error",
    "requested_at", "received_at", "created_at", "completed_at",
})
_KRONOS_INDEX_NAMES = (
    "idx_kronos_forecasts_trade_date",
    "idx_kronos_forecasts_pending",
    "idx_kronos_forecasts_recent",
    "idx_kronos_forecasts_created",
)


def _validate_kronos_table_shape(conn: sqlite3.Connection, name: str) -> None:
    """يرفض تضارب الاسم/الشكل كعطل Shadow صريح قبل أي ALTER."""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        return
    if row["type"] != "table":
        raise KronosMigrationError(f"{name} موجود لكنه ليس جدول SQLite")
    columns = {
        item["name"] for item in conn.execute(f"PRAGMA table_info({name})").fetchall()
    }
    if not _KRONOS_MINIMUM_COLUMNS.issubset(columns):
        missing = sorted(_KRONOS_MINIMUM_COLUMNS - columns)
        raise KronosMigrationError(
            f"مخطط {name} غير مدعوم؛ أعمدة أساسية مفقودة: {', '.join(missing)}"
        )


def _validate_kronos_contract(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(kronos_forecasts)").fetchall()
    }
    missing = sorted(_KRONOS_CONTRACT_COLUMNS - columns)
    if missing:
        raise KronosMigrationError(
            "مخطط kronos_forecasts ناقص بعد الترحيل: " + ", ".join(missing)
        )


def _validate_kronos_index_names(conn: sqlite3.Connection) -> None:
    """أسماء فهارس Shadow لا يجوز أن تحجبها table/view أو فهرس جدول آخر."""
    for name in _KRONOS_INDEX_NAMES:
        row = conn.execute(
            "SELECT type, tbl_name FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            continue
        if row["type"] != "index" or row["tbl_name"] not in {
            "kronos_forecasts", "kronos_forecasts_legacy",
        }:
            raise KronosMigrationError(
                f"اسم فهرس Kronos محجوز بكائن SQLite غير متوافق: {name}"
            )


def _is_kronos_schema_error(exc: sqlite3.OperationalError) -> bool:
    """يفصل SQLITE_ERROR/SCHEMA الاختياري عن أعطال القرص والقفل القاتلة."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    primary = code & 0xFF
    return primary in {sqlite3.SQLITE_ERROR, sqlite3.SQLITE_SCHEMA}


def _recover_interrupted_kronos_migration(conn: sqlite3.Connection) -> None:
    """يعيد الجدول القديم إذا ترك إصدار سابق rename مكتملًا ونسخة فارغة."""
    if not _table_exists(conn, "kronos_forecasts_legacy"):
        return
    conn.execute("SAVEPOINT recover_kronos_migration")
    try:
        if not _table_exists(conn, "kronos_forecasts"):
            conn.execute(
                "ALTER TABLE kronos_forecasts_legacy RENAME TO kronos_forecasts"
            )
        else:
            current_count = conn.execute(
                "SELECT COUNT(*) FROM kronos_forecasts"
            ).fetchone()[0]
            legacy_count = conn.execute(
                "SELECT COUNT(*) FROM kronos_forecasts_legacy"
            ).fetchone()[0]
            if current_count == 0:
                conn.execute("DROP INDEX IF EXISTS idx_kronos_forecasts_trade_date")
                conn.execute("DROP TABLE kronos_forecasts")
                conn.execute(
                    "ALTER TABLE kronos_forecasts_legacy RENAME TO kronos_forecasts"
                )
            elif legacy_count == 0:
                conn.execute("DROP TABLE kronos_forecasts_legacy")
            else:
                raise KronosMigrationError(
                    "ترحيل Kronos سابق غير مكتمل ويحتوي جدولين غير فارغين"
                )
    except Exception:
        conn.execute("ROLLBACK TO recover_kronos_migration")
        conn.execute("RELEASE recover_kronos_migration")
        raise
    else:
        conn.execute("RELEASE recover_kronos_migration")


def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    migrations: tuple[tuple[str, str], ...],
) -> None:
    """يضيف الغائب فقط؛ أخطاء القرص/القفل لا تُخفى كأن العمود موجود."""
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column, declaration in migrations:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        existing.add(column)


def _ensure_kronos_indexes(conn: sqlite3.Connection) -> None:
    """فهارس المسار الحي والتقرير؛ يلزم استعادتها بعد أي rebuild للجدول."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kronos_forecasts_trade_date "
        "ON kronos_forecasts(trade_date, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kronos_forecasts_pending "
        "ON kronos_forecasts(asof_at) WHERE status='ok' "
        "AND completed_at IS NULL AND base_close IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kronos_forecasts_recent "
        "ON kronos_forecasts(asof_at DESC, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kronos_forecasts_created "
        "ON kronos_forecasts(created_at DESC)"
    )


def _migrate_kronos_primary_key(conn: sqlite3.Connection) -> None:
    """يرقّي مفتاح Shadow ذريًا؛ الفشل يعيد الجدول الأصلي كاملًا."""
    conn.execute("SAVEPOINT migrate_kronos_primary_key")
    try:
        info = conn.execute("PRAGMA table_info(kronos_forecasts)").fetchall()
        pk_columns = [
            row["name"] for row in sorted(info, key=lambda value: value["pk"])
            if row["pk"]
        ]
        if pk_columns != ["ticker", "asof_at", "experiment_id"]:
            rows = conn.execute("SELECT * FROM kronos_forecasts").fetchall()
            conn.execute("DROP INDEX IF EXISTS idx_kronos_forecasts_trade_date")
            conn.execute(
                "ALTER TABLE kronos_forecasts RENAME TO kronos_forecasts_legacy"
            )
            conn.execute(
                """
                CREATE TABLE kronos_forecasts (
                    ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
                    asof_at TEXT NOT NULL, experiment_id TEXT NOT NULL,
                    session TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    model TEXT, model_revision TEXT NOT NULL DEFAULT '',
                    tokenizer TEXT, tokenizer_revision TEXT,
                    kronos_revision TEXT, service_revision TEXT,
                    client_revision TEXT, market_timezone TEXT, device TEXT,
                    max_context INTEGER, observation_grace_min REAL,
                    lookback INTEGER, pred_len INTEGER,
                    returns_json TEXT NOT NULL DEFAULT '{}', base_close REAL,
                    actuals_json TEXT NOT NULL DEFAULT '{}',
                    actual_observed_at_json TEXT NOT NULL DEFAULT '{}',
                    latency_ms REAL,
                    service_latency_ms REAL, error TEXT, requested_at TEXT,
                    received_at TEXT, created_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY (ticker, asof_at, experiment_id)
                )
                """
            )
            for row in rows:
                try:
                    returns = json.loads(row["returns_json"] or "{}")
                    horizons = [int(value) for value in returns]
                except (TypeError, ValueError, json.JSONDecodeError):
                    horizons = []
                experiment_id = str(row["experiment_id"] or "").strip()
                if not experiment_id:
                    experiment_id = _fallback_kronos_experiment_id(
                        model=row["model"] or "",
                        model_revision=row["model_revision"] or "",
                        tokenizer=row["tokenizer"] or "",
                        tokenizer_revision=row["tokenizer_revision"] or "",
                        kronos_revision=row["kronos_revision"] or "",
                        service_revision=row["service_revision"] or "",
                        client_revision=row["client_revision"] or "",
                        market_timezone=row["market_timezone"] or "",
                        device=row["device"] or "",
                        max_context=row["max_context"],
                        observation_grace_min=row["observation_grace_min"],
                        lookback=row["lookback"],
                        pred_len=row["pred_len"], horizons=horizons,
                    )
                conn.execute(
                    """
                    INSERT INTO kronos_forecasts (
                        ticker, trade_date, asof_at, experiment_id, session,
                        status, model, model_revision, tokenizer,
                        tokenizer_revision, kronos_revision, service_revision,
                        client_revision, market_timezone, device, max_context,
                        observation_grace_min, lookback, pred_len, returns_json,
                        base_close, actuals_json, actual_observed_at_json,
                        latency_ms, service_latency_ms, error,
                        requested_at, received_at, created_at, completed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        row["ticker"], row["trade_date"], row["asof_at"],
                        experiment_id, row["session"], row["status"], row["model"],
                        row["model_revision"], row["tokenizer"],
                        row["tokenizer_revision"], row["kronos_revision"],
                        row["service_revision"], row["client_revision"],
                        row["market_timezone"], row["device"], row["max_context"],
                        row["observation_grace_min"], row["lookback"],
                        row["pred_len"], row["returns_json"], row["base_close"],
                        row["actuals_json"], row["actual_observed_at_json"],
                        row["latency_ms"],
                        row["service_latency_ms"], row["error"], row["requested_at"],
                        row["received_at"], row["created_at"], row["completed_at"],
                    ),
                )
            conn.execute("DROP TABLE kronos_forecasts_legacy")
            conn.execute(
                "CREATE INDEX idx_kronos_forecasts_trade_date "
                "ON kronos_forecasts(trade_date, status)"
            )
    except Exception:
        conn.execute("ROLLBACK TO migrate_kronos_primary_key")
        conn.execute("RELEASE migrate_kronos_primary_key")
        raise
    else:
        conn.execute("RELEASE migrate_kronos_primary_key")


class Store:
    """طبقة SQLite. آمِنة للثريدات (قفل + check_same_thread=False)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._kronos_available = False
        self._kronos_error = ""
        try:
            with self._lock:
                self._conn.executescript(_SCHEMA)
                # نقرأ المخطط أولًا بدل ابتلاع كل OperationalError؛ lock/disk
                # full/read-only يجب أن يفشل الإقلاع بوضوح، لا يترك DB ناقصة.
                _add_missing_columns(self._conn, "tracking", _MIGRATIONS)
                self._conn.commit()
                try:
                    _validate_kronos_table_shape(
                        self._conn, "kronos_forecasts"
                    )
                    _validate_kronos_table_shape(
                        self._conn, "kronos_forecasts_legacy"
                    )
                    _validate_kronos_index_names(self._conn)
                    self._conn.executescript(_KRONOS_SCHEMA)
                    _recover_interrupted_kronos_migration(self._conn)
                    _validate_kronos_index_names(self._conn)
                    _add_missing_columns(
                        self._conn, "kronos_forecasts", _KRONOS_MIGRATIONS
                    )
                    _validate_kronos_contract(self._conn)
                    _migrate_kronos_primary_key(self._conn)
                    _validate_kronos_contract(self._conn)
                    _ensure_kronos_indexes(self._conn)
                except sqlite3.OperationalError as exc:
                    if not _is_kronos_schema_error(exc):
                        raise
                    self._conn.rollback()
                    self._kronos_error = str(exc)[:500]
                    logger.warning(
                        "تعارض مخطط Kronos Shadow؛ مخزن core مستمر: %s", exc
                    )
                except (
                    KronosMigrationError,
                    sqlite3.IntegrityError,
                    IndexError,
                    KeyError,
                ) as exc:
                    self._conn.rollback()
                    self._kronos_error = str(exc)[:500]
                    logger.warning(
                        "تعذّر تهيئة مخزن Kronos Shadow؛ مخزن core مستمر: %s",
                        exc,
                    )
                else:
                    self._conn.commit()
                    self._kronos_available = True
        except Exception:
            self._conn.close()
            raise

    @property
    def kronos_available(self) -> bool:
        """هل مخطط Shadow جاهز؟ لا يعبّر عن تفعيل العامل أو الخدمة."""
        return self._kronos_available

    @property
    def kronos_error(self) -> str:
        return self._kronos_error

    # ── منع التكرار ───────────────────────────────────────────────
    def already_alerted(self, ticker: str, now: datetime | None = None) -> bool:
        day = trade_date_str(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE ticker=? AND trade_date=?",
                (ticker, day)).fetchone()
            return row is not None

    def mark_alerted(self, ticker: str, score: float,
                     now: datetime | None = None,
                     entry_price: float | None = None,
                     stop_price: float | None = None,
                     targets: list[float] | None = None) -> None:
        """يُعلِّم السهم مُنبَّهًا عنه. عند تمرير خطة البطاقة (entry_price/stop/
        targets) نثبّت **لقطة البطاقة كما رآها المستخدم** عند أوّل انتقال إلى
        تنبيه (BUG-32): سعر الدخول والوقف والأهداف، ونُرسي القمة/القاع على سعر
        الدخول — كي تُقاس النتيجة من لحظة التنبيه لا من سعر أوّل رصد (قد يسبقه
        بساعات وبطبعة رقيقة فيُطلق hit_stop زائفًا حين الوقف فوق سعر أوّل رصد).
        بلا الخطة (اختبارات/توافق) نكتفي بـ is_alert=1 كالسابق."""
        day = trade_date_str(now)
        tg = list(targets or [])
        t1 = tg[0] if len(tg) > 0 else None
        t2 = tg[1] if len(tg) > 1 else None
        t3 = tg[2] if len(tg) > 2 else None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts(ticker, trade_date, alerted_at, score)"
                " VALUES(?,?,?,?)", (ticker, day, _iso(now), score))
            if entry_price and entry_price > 0:
                # شروط CASE تقرأ entry_price **قبل** التحديث (SQLite يقيّم طرف
                # SET من الصفّ الأصلي) → تُطبَّق مرّة واحدة فقط عند أوّل تنبيه.
                self._conn.execute(
                    """
                    UPDATE tracking SET is_alert=1,
                        alerted_at=COALESCE(alerted_at, ?),
                        entry_price=COALESCE(entry_price, ?),
                        stop_price=CASE WHEN entry_price IS NULL
                            THEN ? ELSE stop_price END,
                        target1=CASE WHEN entry_price IS NULL
                            THEN ? ELSE target1 END,
                        target2=CASE WHEN entry_price IS NULL
                            THEN ? ELSE target2 END,
                        target3=CASE WHEN entry_price IS NULL
                            THEN ? ELSE target3 END,
                        high_after=CASE WHEN entry_price IS NULL
                            THEN ? ELSE high_after END,
                        low_after=CASE WHEN entry_price IS NULL
                            THEN ? ELSE low_after END,
                        notified_high=CASE WHEN entry_price IS NULL
                            THEN ? ELSE notified_high END,
                        peak_at=CASE WHEN entry_price IS NULL
                            THEN NULL ELSE peak_at END,
                        stop_dist_at=CASE WHEN entry_price IS NULL
                            THEN NULL ELSE stop_dist_at END,
                        -- BUG-42 (النصف الثاني من BUG-39): الصفّ المرفوض قد
                        -- تكون نافذته انتهت فأُغلق `timeout` **قبل** التنبيه،
                        -- وupdate_outcomes تقرأ `outcome='open'` فقط ⇒ لا تصل
                        -- ولا رسالة متابعة. بما أننا نعيد إرساء الصفّ على لقطة
                        -- البطاقة هنا، تُعاد معه دورة حياته وقياساتُ ما قبل
                        -- التنبيه (أهداف/وقف من لقطة أوّل رصد لا من البطاقة).
                        outcome=CASE WHEN entry_price IS NULL
                            THEN 'open' ELSE outcome END,
                        result=CASE WHEN entry_price IS NULL
                            THEN NULL ELSE result END,
                        closed_at=CASE WHEN entry_price IS NULL
                            THEN NULL ELSE closed_at END,
                        notified_targets=CASE WHEN entry_price IS NULL
                            THEN 0 ELSE notified_targets END,
                        hit_target=CASE WHEN entry_price IS NULL
                            THEN 0 ELSE hit_target END,
                        hit_stop=CASE WHEN entry_price IS NULL
                            THEN 0 ELSE hit_stop END,
                        notified_stop=CASE WHEN entry_price IS NULL
                            THEN 0 ELSE notified_stop END
                    WHERE ticker=? AND trade_date=?
                    """,
                    (_iso(now), entry_price, stop_price, t1, t2, t3,
                     entry_price, entry_price, entry_price, ticker, day))
            else:
                self._conn.execute(
                    "UPDATE tracking SET is_alert=1,"
                    " alerted_at=COALESCE(alerted_at, ?)"
                    " WHERE ticker=? AND trade_date=?",
                    (_iso(now), ticker, day))
            self._conn.commit()

    # ── توقعات Kronos التجريبية (Shadow) ──────────────────────────
    def save_kronos_forecast(
        self,
        ticker: str,
        asof_at: datetime,
        *,
        status: str,
        experiment_id: str = "",
        session: str = "",
        model: str = "",
        model_revision: str = "",
        tokenizer: str = "",
        tokenizer_revision: str = "",
        kronos_revision: str = "",
        service_revision: str = "",
        client_revision: str = "",
        market_timezone: str = "",
        device: str = "",
        max_context: int | None = None,
        observation_grace_min: float | None = None,
        lookback: int | None = None,
        pred_len: int | None = None,
        returns: dict[int, float] | None = None,
        base_close: float | None = None,
        latency_ms: float | None = None,
        service_latency_ms: float | None = None,
        error: str = "",
        requested_at: datetime | None = None,
        received_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """يحفظ ناتج Shadow مستقلًا عن قرار التنبيه ودرجته.

        المفتاح يشمل وقت التنبؤ وهوية التجربة الكاملة، لذلك لا تكتب نسخة
        tokenizer/lookback/كود مختلفة فوق قياس سابق للسهم نفسه.
        """
        if not self._kronos_available:
            raise RuntimeError("مخزن Kronos Shadow غير متاح")
        if asof_at.tzinfo is None:
            asof_at = asof_at.replace(tzinfo=timezone.utc)
        clean_returns: dict[str, float] = {}
        for horizon, value in (returns or {}).items():
            try:
                horizon_i = int(horizon)
                value_f = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if horizon_i > 0 and math.isfinite(value_f):
                clean_returns[str(horizon_i)] = value_f
        try:
            base = float(base_close) if base_close is not None else None
        except (TypeError, ValueError, OverflowError):
            base = None
        if base is not None and (base <= 0 or not math.isfinite(base)):
            base = None
        try:
            stored_grace = (
                float(observation_grace_min)
                if observation_grace_min is not None else None
            )
        except (TypeError, ValueError, OverflowError):
            stored_grace = None
        if stored_grace is not None and (
            not math.isfinite(stored_grace) or not 0 <= stored_grace < 5
        ):
            stored_grace = None
        clean_experiment_id = str(experiment_id or "").strip()
        if not clean_experiment_id:
            clean_experiment_id = _fallback_kronos_experiment_id(
                model=model,
                model_revision=model_revision,
                tokenizer=tokenizer,
                tokenizer_revision=tokenizer_revision,
                kronos_revision=kronos_revision,
                service_revision=service_revision,
                client_revision=client_revision,
                market_timezone=market_timezone,
                device=device,
                max_context=max_context,
                observation_grace_min=stored_grace,
                lookback=lookback,
                pred_len=pred_len,
                horizons=[int(value) for value in clean_returns],
            )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO kronos_forecasts (
                    ticker, trade_date, asof_at, experiment_id, session,
                    status, model, model_revision, tokenizer,
                    tokenizer_revision, kronos_revision, service_revision,
                    client_revision, market_timezone, device, max_context,
                    observation_grace_min, lookback, pred_len,
                    returns_json, base_close, latency_ms, service_latency_ms,
                    error, requested_at, received_at, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(ticker, asof_at, experiment_id) DO UPDATE SET
                    session=excluded.session,
                    status=excluded.status,
                    model=excluded.model,
                    model_revision=excluded.model_revision,
                    tokenizer=excluded.tokenizer,
                    tokenizer_revision=excluded.tokenizer_revision,
                    kronos_revision=excluded.kronos_revision,
                    service_revision=excluded.service_revision,
                    client_revision=excluded.client_revision,
                    market_timezone=excluded.market_timezone,
                    device=excluded.device,
                    max_context=excluded.max_context,
                    observation_grace_min=excluded.observation_grace_min,
                    lookback=excluded.lookback,
                    pred_len=excluded.pred_len,
                    returns_json=excluded.returns_json,
                    base_close=COALESCE(excluded.base_close,
                                        kronos_forecasts.base_close),
                    latency_ms=excluded.latency_ms,
                    service_latency_ms=excluded.service_latency_ms,
                    error=excluded.error,
                    requested_at=excluded.requested_at,
                    received_at=excluded.received_at,
                    created_at=excluded.created_at
                WHERE kronos_forecasts.status != 'ok'
                """,
                (
                    ticker.upper(), trade_date_str(asof_at), asof_at.isoformat(),
                    clean_experiment_id[:200], str(session or "")[:64],
                    status, model, model_revision, tokenizer,
                    tokenizer_revision, kronos_revision, service_revision,
                    client_revision, market_timezone, device, max_context,
                    stored_grace, lookback, pred_len,
                    json.dumps(clean_returns, ensure_ascii=False, sort_keys=True),
                    base, latency_ms, service_latency_ms, error[:1000],
                    requested_at.isoformat() if requested_at else None,
                    received_at.isoformat() if received_at else None,
                    _iso(created_at),
                ),
            )
            self._conn.commit()

    def update_kronos_actuals(
        self,
        price_map: dict[str, float],
        now: datetime | None = None,
        *,
        bar_minutes: float = 5.0,
        grace_min: float = 3.0,
        observed_at_map: dict[str, datetime] | None = None,
    ) -> int:
        """يثبّت العائد الفعلي عند كل أفق من أقرب snapshot لاحق.

        إذا فات الموعد بأكثر من ``grace_min`` نسجّل ``null`` بدل اختراع نتيجة
        من سعر متأخر (انقطاع عامل/سوق مغلق). وعند تمرير ``observed_at_map`` لا
        يُقبل سعر snapshot قديم زمنيًا ولو وصل في دورة حديثة. يرجع عدد الآفاق.
        """
        if not self._kronos_available:
            return 0
        try:
            bar_minutes_f = float(bar_minutes)
            grace_min_f = float(grace_min)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("سياسة توقيت Kronos غير صالحة") from exc
        if (
            not math.isfinite(bar_minutes_f)
            or bar_minutes_f <= 0
            or not math.isfinite(grace_min_f)
            or not 0 <= grace_min_f < bar_minutes_f
        ):
            raise ValueError("سياسة توقيت Kronos غير صالحة")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        resolved = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM kronos_forecasts "
                "WHERE status='ok' AND completed_at IS NULL "
                "AND base_close IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    asof = datetime.fromisoformat(row["asof_at"])
                    if asof.tzinfo is None:
                        asof = asof.replace(tzinfo=timezone.utc)
                    elapsed_min = (now - asof).total_seconds() / 60.0
                    predicted = json.loads(row["returns_json"] or "{}")
                    actuals = json.loads(row["actuals_json"] or "{}")
                    actual_observed_at = json.loads(
                        row["actual_observed_at_json"] or "{}"
                    )
                    base_close = float(row["base_close"])
                    row_grace = float(
                        row["observation_grace_min"]
                        if row["observation_grace_min"] is not None
                        else grace_min_f
                    )
                    if (
                        not isinstance(predicted, dict)
                        or not isinstance(actuals, dict)
                        or not isinstance(actual_observed_at, dict)
                        or not math.isfinite(base_close)
                        or base_close <= 0
                        or not math.isfinite(row_grace)
                        or not 0 <= row_grace < bar_minutes_f
                    ):
                        continue
                except (
                    TypeError, ValueError, OverflowError, json.JSONDecodeError,
                ):
                    continue
                price = price_map.get(row["ticker"])
                try:
                    price_f = float(price) if price is not None else None
                except (TypeError, ValueError, OverflowError):
                    price_f = None
                if price_f is not None and (price_f <= 0 or not math.isfinite(price_f)):
                    price_f = None
                observed_at = now
                if observed_at_map is not None:
                    observed_at = observed_at_map.get(row["ticker"])
                if observed_at is not None and not isinstance(observed_at, datetime):
                    observed_at = None
                if observed_at is not None and observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                observed_elapsed_min = (
                    (observed_at - asof).total_seconds() / 60.0
                    if observed_at is not None else None
                )
                changed = False
                for horizon_s in predicted:
                    if horizon_s in actuals:
                        continue
                    try:
                        target_min = int(horizon_s) * bar_minutes_f
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if target_min <= 0 or not math.isfinite(target_min):
                        continue
                    if elapsed_min < target_min:
                        continue
                    fresh_observation = (
                        price_f is not None
                        and observed_elapsed_min is not None
                        and target_min <= observed_elapsed_min <= target_min + row_grace
                    )
                    if fresh_observation:
                        actuals[horizon_s] = round(
                            (price_f - base_close) / base_close * 100.0,
                            6,
                        )
                        actual_observed_at[horizon_s] = observed_at.isoformat()
                    elif elapsed_min <= target_min + row_grace:
                        # السعر غائب/قديم؛ انتظر لقطة أحدث داخل نافذة السماح.
                        continue
                    else:
                        actuals[horizon_s] = None
                        actual_observed_at[horizon_s] = None
                    changed = True
                    resolved += 1
                if not changed:
                    continue
                done = all(str(h) in actuals for h in predicted)
                self._conn.execute(
                    "UPDATE kronos_forecasts SET actuals_json=?, "
                    "actual_observed_at_json=?, completed_at=? "
                    "WHERE ticker=? AND asof_at=? AND experiment_id=?",
                    (
                        json.dumps(actuals, ensure_ascii=False, sort_keys=True),
                        json.dumps(
                            actual_observed_at,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        _iso(now) if done else None,
                        row["ticker"], row["asof_at"], row["experiment_id"],
                    ),
                )
            self._conn.commit()
        return resolved

    def fetch_kronos_forecasts(self, limit: int = 500) -> list[dict]:
        """يرجع أحدث توقعات Shadow بعد فك عوائد الآفاق من JSON."""
        if not self._kronos_available:
            return []
        safe_limit = max(1, min(int(limit), 10_000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT rowid AS _rowid, * FROM kronos_forecasts "
                "ORDER BY asof_at DESC, created_at DESC, rowid DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._decode_kronos_row(row) for row in rows]

    def fetch_kronos_evaluation_window(
        self, trading_days: int = 30,
    ) -> Iterator[dict]:
        """يبث كل سجلات أحدث N أيام بدفعات، بلا قطع عددي أو list ضخمة."""
        if not self._kronos_available:
            return
        safe_days = max(1, min(int(trading_days), 366))
        cursor: sqlite3.Cursor | None = None
        with self._lock:
            date_rows = self._conn.execute(
                "SELECT DISTINCT trade_date FROM kronos_forecasts "
                "WHERE trade_date <> '' ORDER BY trade_date DESC LIMIT ?",
                (safe_days,),
            ).fetchall()
            dates = [row["trade_date"] for row in date_rows]
            if not dates:
                return
            placeholders = ",".join("?" for _ in dates)
            cursor = self._conn.execute(
                "SELECT rowid AS _rowid, * FROM kronos_forecasts "
                f"WHERE trade_date IN ({placeholders}) "
                "ORDER BY asof_at DESC, created_at DESC, rowid DESC",
                dates,
            )
        try:
            while True:
                with self._lock:
                    batch = cursor.fetchmany(256)
                if not batch:
                    return
                for row in batch:
                    yield self._decode_kronos_row(row)
        finally:
            with self._lock:
                cursor.close()

    def fetch_kronos_active_record(self, trading_days: int = 30) -> dict | None:
        """أحدث نجاح يحدد cohort التقرير قبل بث بقية نافذة القياس."""
        if not self._kronos_available:
            return None
        safe_days = max(1, min(int(trading_days), 366))
        with self._lock:
            row = self._conn.execute(
                "SELECT rowid AS _rowid, * FROM kronos_forecasts "
                "WHERE status='ok' AND model_revision <> '' "
                "AND trade_date IN ("
                "SELECT DISTINCT trade_date FROM kronos_forecasts "
                "WHERE trade_date <> '' ORDER BY trade_date DESC LIMIT ?"
                ") "
                "ORDER BY asof_at DESC, created_at DESC, rowid DESC LIMIT 1",
                (safe_days,),
            ).fetchone()
        return self._decode_kronos_row(row) if row is not None else None

    def fetch_kronos_latest_record(self) -> dict | None:
        """أحدث كتابة تشغيلية بحسب created_at، مستقلة عن asof النموذج."""
        if not self._kronos_available:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT rowid AS _rowid, * FROM kronos_forecasts "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return self._decode_kronos_row(row) if row is not None else None

    @staticmethod
    def _decode_kronos_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            raw = json.loads(item.pop("returns_json") or "{}")
            item["returns"] = {int(k): float(v) for k, v in raw.items()}
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            item["returns"] = {}
        try:
            raw_actuals = json.loads(item.pop("actuals_json") or "{}")
            item["actuals"] = {
                int(k): (None if v is None else float(v))
                for k, v in raw_actuals.items()
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            item["actuals"] = {}
        try:
            raw_observed = json.loads(
                item.pop("actual_observed_at_json") or "{}"
            )
            item["actual_observed_at"] = {
                int(k): (None if v is None else str(v))
                for k, v in raw_observed.items()
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            item["actual_observed_at"] = {}
        return item

    # ── closed-loop + تهيئة التتبّع (upsert صفّ/سهم/يوم) ───────────
    def log_candidate(self, c: Candidate, now: datetime | None = None) -> None:
        day = trade_date_str(now)
        ts = _iso(now)
        price = c.snapshot.last_price
        stop = c.risk.stop_price if c.risk else None
        tg = (c.risk.targets if c.risk else []) or []
        t1 = tg[0] if len(tg) > 0 else None
        t2 = tg[1] if len(tg) > 1 else None
        t3 = tg[2] if len(tg) > 2 else None
        had_news = 1 if (c.catalyst and c.catalyst.has_news) else 0
        # بيانات تشريح الفشل
        dilution_risk = c.dilution.risk if c.dilution else None
        analyst_dir = c.analyst.direction if c.analyst else None
        catalyst_head = (c.catalyst.headline
                         if (c.catalyst and c.catalyst.has_news) else None)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracking (
                    ticker, trade_date, first_seen_at, logged_at, session,
                    first_session,
                    change_pct, score, momentum, readiness, rvol, rvol_5min,
                    float_shares, float_source, halt_state, had_news, rejected,
                    reject_reason, reason_code, short_pct, dilution_risk, analyst_dir,
                    catalyst_head, first_price, first_volume, stop_price, target1,
                    target2, target3, high_after, low_after, max_gain_pct,
                    notified_high, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?, 'open')
                ON CONFLICT(ticker, trade_date) DO UPDATE SET
                    logged_at=excluded.logged_at, session=excluded.session,
                    -- جلسة أول رصد لا تُمسّ (COALESCE يملأ صفوف ما قبل الترحيل فقط)
                    first_session=COALESCE(tracking.first_session,
                                           excluded.first_session),
                    change_pct=excluded.change_pct, score=excluded.score,
                    momentum=excluded.momentum, readiness=excluded.readiness,
                    rvol=excluded.rvol, rvol_5min=excluded.rvol_5min,
                    float_shares=excluded.float_shares,
                    float_source=excluded.float_source,
                    halt_state=excluded.halt_state, had_news=excluded.had_news,
                    rejected=excluded.rejected, reject_reason=excluded.reject_reason,
                    reason_code=excluded.reason_code,
                    short_pct=excluded.short_pct,
                    dilution_risk=excluded.dilution_risk,
                    analyst_dir=excluded.analyst_dir,
                    catalyst_head=excluded.catalyst_head,
                    -- إعادة تأسيس السعر المرجعي عند الانتقال من جلسة ممتدة إلى
                    -- الرسمية (لغير المُنبَّه عنه): طبعة البريماركت الرقيقة ليست
                    -- سعر الدخول الفعلي. لا نمسّ outcome/result.
                    first_price=CASE WHEN tracking.session <> 'رسمي'
                        AND excluded.session = 'رسمي' AND tracking.is_alert = 0
                        THEN excluded.first_price ELSE tracking.first_price END,
                    first_volume=CASE WHEN tracking.session <> 'رسمي'
                        AND excluded.session = 'رسمي' AND tracking.is_alert = 0
                        THEN excluded.first_volume ELSE tracking.first_volume END,
                    high_after=CASE WHEN tracking.session <> 'رسمي'
                        AND excluded.session = 'رسمي' AND tracking.is_alert = 0
                        THEN excluded.first_price ELSE tracking.high_after END,
                    low_after=CASE WHEN tracking.session <> 'رسمي'
                        AND excluded.session = 'رسمي' AND tracking.is_alert = 0
                        THEN excluded.first_price ELSE tracking.low_after END,
                    notified_high=CASE WHEN tracking.session <> 'رسمي'
                        AND excluded.session = 'رسمي' AND tracking.is_alert = 0
                        THEN excluded.first_price ELSE tracking.notified_high END,
                    stop_price=COALESCE(tracking.stop_price, excluded.stop_price),
                    target1=COALESCE(tracking.target1, excluded.target1),
                    target2=COALESCE(tracking.target2, excluded.target2),
                    target3=COALESCE(tracking.target3, excluded.target3)
                """,
                (
                    c.ticker, day, ts, ts, c.session.value,
                    c.session.value,   # first_session (يُثبَّت عند أول إدراج)
                    c.snapshot.change_pct, c.final_score,
                    c.momentum.score if c.momentum else None,
                    c.readiness.classic_score if c.readiness else None,
                    c.momentum.rvol if c.momentum else None,
                    c.momentum.rvol_5min if c.momentum else None,
                    c.float_shares, c.float_source.value, c.halt_state.value,
                    had_news, 1 if c.is_rejected else 0, c.rejected_reason,
                    c.reject_code or "",
                    c.short_pct, dilution_risk, analyst_dir, catalyst_head,
                    price, c.snapshot.day_volume, stop, t1, t2, t3,
                    price, price, price,
                ))
            self._conn.commit()

    # ── تتبّع النتائج + إصدار أحداث المتابعة (من السنابشوت) ───────
    def update_outcomes(self, price_map: dict[str, float],
                        now: datetime | None = None,
                        window_min: float = 90.0,
                        surge_leg_pct: float = 8.0,
                        missed_rise_pct: float = 1e9,
                        volume_map: dict[str, float] | None = None,
                        stop_dist_pct: float = 0.0) -> list[dict]:
        """يحدّث كل تتبّع مفتوح ويرجّع أحداث المتابعة:
        [{ticker, type:'target'/'stop'/'surge'/'missed', price, gain_pct, ...}].
        - target/stop/surge: للمُنبَّه عنه فقط.
        - missed: سهم **مرفوض** صعد ≥ missed_rise_pct (فرصة فائتة + سببها).
        يحسم result (win/loss/timeout) ويغلق الصفّ عند الوقف/كل الأهداف/النافذة.
        (missed_rise_pct الافتراضي ضخم = معطّل ما لم يُمرَّر.)
        """
        now = now or datetime.now(timezone.utc)
        day = trade_date_str(now)
        events: list[dict] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracking WHERE outcome='open' AND trade_date=?",
                (day,)).fetchall()
            for r in rows:
                price = price_map.get(r["ticker"])
                if price is None or price <= 0:
                    continue
                # أساس قياس النتيجة: سعر دخول البطاقة للمُنبَّه عنه (entry_price)،
                # وإلا سعر أوّل رصد (first_price) للمرفوض/صفوف ما قبل BUG-32.
                first = r["entry_price"] or r["first_price"] or price
                high = max(r["high_after"] or price, price)
                low = min(r["low_after"] or price, price)
                max_gain = (high - first) / first * 100.0 if first > 0 else 0.0
                max_draw = (low - first) / first * 100.0 if first > 0 else 0.0
                # للمرفوض (الأساس = first_price): الوقف الشرعي **تحت** الأساس؛
                # وقفٌ عند/فوقه = مجمَّد من دورة بسعر أعلى (COALESCE) فنتجاهله كي
                # لا يُطلق hit_stop زائفًا فور أوّل رصد (BUG-32: FBRX 25.9 فوق 24.9).
                # للمُنبَّه عنه (entry_price مُرسى) الوقف لقطة البطاقة الموثوقة →
                # يُعتمد دائمًا؛ بلا هذا كان وقفٌ = الدخول (stop_fixed_pct=0) يُهمَل
                # فتُحسب خسارة حقيقية timeout.
                stop_valid = bool(r["stop_price"] and first > 0
                                  and (r["entry_price"] or r["stop_price"] < first))

                # ترتيب القمة/القاع (قياس فقط): طابع تحسّن القمة، وأول لمسة
                # لمسافة الوقف من الدخول — لحسم «هل سُتوقَف قبل القمة؟» في الفائتة.
                peak_at = _iso(now) if high > (r["high_after"] or 0) else r["peak_at"]
                stop_dist_at = r["stop_dist_at"]
                if (stop_dist_pct > 0 and not stop_dist_at and first > 0
                        and low <= first * (1 - stop_dist_pct / 100.0)):
                    stop_dist_at = _iso(now)

                targets = [r["target1"], r["target2"], r["target3"]]
                targets = [t for t in targets if t]
                notified_t = r["notified_targets"] or 0
                notified_stop = r["notified_stop"] or 0
                notified_high = r["notified_high"] or first
                result = r["result"] or ""
                hit_target = r["hit_target"] or 0
                hit_stop = r["hit_stop"] or 0
                outcome = "open"
                is_alert = r["is_alert"] or 0

                # مشاركة الحجم منذ الرصد (تُدمج مع RVol لمتابعة أدقّ): قفزة على
                # حجم متزايد = حركة حقيقية مدعومة؛ على حجم خافت = حذر.
                cur_vol = (volume_map or {}).get(r["ticker"])
                first_vol = r["first_volume"] or 0.0
                participation = None
                if cur_vol and first_vol > 0:
                    g = cur_vol / first_vol
                    participation = ("قوية ⬆️" if g >= 1.5 else
                                     "معتدلة" if g >= 1.1 else "خافتة ⚠️")

                # §8/BUG-08: لمس الهدف + الوقف في **نفس النبضة** = خسارة (لا يمكن
                # معرفة الترتيب داخل النبضة، فنتحفّظ). نحسب لمسة الوقف الآن **قبل**
                # حلقة الأهداف كي تسجّل الأهداف «loss» لا «win» إن لُمس الوقف أيضًا.
                # (هدف لُمس في نبضة سابقة يبقى «win» — القاعدة لنفس النبضة فقط.)
                stop_now = bool(not notified_stop and stop_valid
                                and low <= r["stop_price"])

                # 🎯 أهداف: نبلّغ كل هدف عُبر لأول مرة (نرسل للمُنبَّه فقط)
                while notified_t < len(targets) and high >= targets[notified_t]:
                    notified_t += 1
                    hit_target = 1
                    if not result:
                        result = "loss" if stop_now else "win"
                    if is_alert:
                        # 🪜 الوقف المُرقّى بعد هذا الهدف: بعد الهدف1 = التعادل (سعر
                        # دخول المستخدم من البطاقة، لا first_price المتتبَّع الذي قد
                        # يختلف لو تأخّر التنبيه عبر دورات) → new_stop=None ونصيغ
                        # «للتعادل» في الرسالة. بعد كل هدف تالٍ = الهدف السابق (سعر
                        # مطلق مطابق للبطاقة). يُرشد لرفع الوقف — لا يغيّر أي تتبّع.
                        new_stop = (None if notified_t == 1
                                    else targets[notified_t - 2])
                        events.append({
                            "ticker": r["ticker"], "type": "target",
                            "level": notified_t, "price": targets[notified_t - 1],
                            "gain_pct": (targets[notified_t - 1] - first) / first * 100.0,
                            "participation": participation,
                            "new_stop": new_stop,
                        })

                # ⛔ الوقف: للمُنبَّه = نبلّغ مرة واحدة ويغلق التتبّع. أما الصفوف
                # المرفوضة (غير المُنبَّهة) فلا تُغلق على الوقف (BUG-15): نسجّل
                # `hit_stop` للمعايرة فقط وتبقى مفتوحة — كي لا يموت تنبيه 👻 الفرصة
                # الفائتة لو انطلق السهم لاحقًا بعد لمسه الوقف الافتراضي. النافذة
                # تغلقها. (`rejected_row` = مرفوض غير مُنبَّه؛ نفس شرط تنبيه 👻.)
                rejected_row = bool((r["rejected"] or 0) and not is_alert)
                if stop_valid and low <= r["stop_price"]:
                    hit_stop = 1
                    if not rejected_row and not notified_stop:
                        notified_stop = 1
                        if not result:
                            result = "loss"
                        if is_alert:
                            events.append({
                                "ticker": r["ticker"], "type": "stop",
                                "price": r["stop_price"],
                                "gain_pct": (r["stop_price"] - first) / first * 100.0,
                            })

                # 🚀 قفزة قوية: قمة جديدة ≥ surge فوق آخر قمة مُبلَّغة
                if high >= notified_high * (1 + surge_leg_pct / 100.0):
                    notified_high = high
                    if is_alert:
                        events.append({
                            "ticker": r["ticker"], "type": "surge",
                            "price": high,
                            "gain_pct": (high - first) / first * 100.0,
                            "participation": participation,
                        })
                else:
                    notified_high = max(notified_high, high)

                # 👻 فرصة فائتة: سهم مرفوض (غير مُنبَّه) صعد ≥ العتبة — مرة واحدة
                notified_missed = r["notified_missed"] or 0
                if ((r["rejected"] or 0) and not is_alert and not notified_missed
                        and max_gain >= missed_rise_pct):
                    notified_missed = 1
                    # BUG-41: كانت الرسالة تحمل القمة وحدها — وهي FOMO خالص.
                    # بيانات المستخدم: 412 مرفوض RVol وسيط قمتهم +1.92% ووسيط
                    # قاعهم −7.31%، و52% لمسوا مسافة الوقف. القمة بلا القاع
                    # تدفع لقرار سيّئ، والتقرير نفسه يحذّر «القمة لا تكفي».
                    events.append({
                        "ticker": r["ticker"], "type": "missed",
                        "price": high, "gain_pct": max_gain,
                        "draw_pct": max_draw, "hit_stop": bool(hit_stop),
                        # هل لُمس الوقف **قبل** القمة؟ (ترتيب زمني مؤكّد؛
                        # None = لا طوابع مسجّلة لهذا الصفّ)
                        "stop_first": (
                            bool(r["stop_dist_at"] and r["peak_at"]
                                 and r["stop_dist_at"] < r["peak_at"])
                            if r["peak_at"] else None),
                        "reason": r["reject_reason"] or "",
                    })

                # حسم الإغلاق: الوقف، أو كل الأهداف، أو انتهاء النافذة
                if notified_stop:
                    outcome = "closed"
                elif targets and notified_t >= len(targets):
                    outcome = "closed"
                else:
                    # BUG-39: للمُنبَّه عنه تُرسى النافذة على **لحظة التنبيه**
                    # لا أوّل رصد. سهم رُصد 9:35 مرفوضًا ثم نُبِّه 11:15 كانت
                    # نافذته منتهية سلفًا ⇒ يُغلق timeout فورًا ولا تصلك رسالة
                    # «بلغ الهدف» ولا «كسر الوقف» وأنت ممسك بالصفقة.
                    anchor = (r["alerted_at"] if is_alert else None) \
                        or r["first_seen_at"]
                    try:
                        seen = datetime.fromisoformat(anchor)
                        elapsed = (now - seen).total_seconds() / 60.0
                    except (TypeError, ValueError):
                        elapsed = 0.0
                    if elapsed >= window_min:
                        outcome = "closed"
                        if not result:
                            result = "timeout"

                closed_at = _iso(now) if outcome == "closed" else None
                self._conn.execute(
                    "UPDATE tracking SET high_after=?, low_after=?, max_gain_pct=?,"
                    " max_draw_pct=?, hit_target=?, hit_stop=?, notified_targets=?,"
                    " notified_stop=?, notified_high=?, notified_missed=?,"
                    " peak_at=?, stop_dist_at=?,"
                    " result=?, outcome=?, closed_at=? WHERE ticker=? AND trade_date=?",
                    (high, low, round(max_gain, 2), round(max_draw, 2),
                     hit_target, hit_stop, notified_t, notified_stop,
                     notified_high, notified_missed, peak_at, stop_dist_at,
                     result, outcome, closed_at,
                     r["ticker"], r["trade_date"]))
            self._conn.commit()
        return events

    def finalize_stale(self, now: datetime | None = None) -> int:
        """يحسم صفوف التتبّع المفتوحة من **أيام سابقة** (لم تكتمل نافذتها قبل
        إغلاق السوق) كـ win/loss/timeout حسب ما تحقّق، حتى لا تضيع من
        إحصاء أداة التطوير. يرجّع عدد المحسومة."""
        now = now or datetime.now(timezone.utc)
        day = trade_date_str(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracking WHERE outcome='open' AND trade_date < ?",
                (day,)).fetchall()
            for r in rows:
                result = r["result"] or (
                    "win" if r["hit_target"] else
                    "loss" if r["hit_stop"] else "timeout")
                self._conn.execute(
                    "UPDATE tracking SET outcome='closed', result=?, closed_at=?"
                    " WHERE ticker=? AND trade_date=?",
                    (result, _iso(now), r["ticker"], r["trade_date"]))
            self._conn.commit()
            return len(rows)

    # ── استعلامات أداة التطوير ────────────────────────────────────
    # ── صفقاتك الفعلية (الحلقة المغلقة) ───────────────────────────
    # البوت كان يعرف ما **اقترحه** ولا يعرف ما **فعلتَه**، فكل تحليلاته عن أداء
    # الاقتراحات لا عن أدائك: كم دخلت · بأي سعر · متى خرجت. لا يمسّ أي منطق
    # قائم — جدول منفصل يُملأ يدويًّا، وفارغه يعني «لا شيء يتغيّر».
    def open_trade(self, ticker: str, shares: float, entry: float,
                   now: datetime | None = None, note: str = "") -> int:
        """يسجّل دخولك صفقةً. يرجّع معرّفها."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO my_trades(ticker, trade_date, opened_at, shares,"
                " entry, note) VALUES(?,?,?,?,?,?)",
                (ticker.upper(), trade_date_str(now), _iso(now),
                 float(shares), float(entry), note or ""))
            self._conn.commit()
            return int(cur.lastrowid)

    def close_trade(self, ticker: str, exit_price: float,
                    now: datetime | None = None) -> sqlite3.Row | None:
        """يغلق **أقدم** صفقة مفتوحة لهذا الرمز. None لو لا شيء مفتوح."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM my_trades WHERE ticker=? AND exit_price IS NULL"
                " ORDER BY id LIMIT 1", (ticker.upper(),)).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE my_trades SET exit_price=?, closed_at=? WHERE id=?",
                (float(exit_price), _iso(now), row["id"]))
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM my_trades WHERE id=?", (row["id"],)).fetchone()

    def my_open_trades(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM my_trades WHERE exit_price IS NULL"
                " ORDER BY id").fetchall()

    def my_closed_trades(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM my_trades WHERE exit_price IS NOT NULL"
                " ORDER BY id").fetchall()

    def fetch_resolved(self, only_alerts: bool = False) -> list[sqlite3.Row]:
        """التتبّعات المحسومة نتيجتها (result غير فارغ)."""
        q = "SELECT * FROM tracking WHERE result != ''"
        if only_alerts:
            q += " AND is_alert=1"
        with self._lock:
            return self._conn.execute(q).fetchall()

    def fetch_day(self, day: str) -> list[sqlite3.Row]:
        """كل تتبّعات يوم معيّن (للبريفنغ والمساعد)."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM tracking WHERE trade_date=? ORDER BY score DESC",
                (day,)).fetchall()

    def fetch_row(self, ticker: str,
                  day: str | None = None) -> sqlite3.Row | None:
        """صفّ تتبّع سهم (يوم محدّد أو أحدث يوم له) — لتشريح /why."""
        with self._lock:
            if day:
                return self._conn.execute(
                    "SELECT * FROM tracking WHERE ticker=? AND trade_date=?",
                    (ticker, day)).fetchone()
            return self._conn.execute(
                "SELECT * FROM tracking WHERE ticker=? "
                "ORDER BY trade_date DESC LIMIT 1", (ticker,)).fetchone()

    def fetch_failures(self, day: str) -> list[sqlite3.Row]:
        """تنبيهات اليوم التي فشلت (خسارة/بلا حسم) — لتشريح البريفنغ."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM tracking WHERE is_alert=1 AND trade_date=? "
                "AND result IN ('loss','timeout') ORDER BY max_draw_pct ASC",
                (day,)).fetchall()

    def fetch_missed(self, min_rise_pct: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM tracking WHERE rejected=1 AND max_gain_pct >= ?"
                " ORDER BY max_gain_pct DESC", (min_rise_pct,)).fetchall()

    # ── أبطال الفترة (توريث بين الجلسات) ──────────────────────────
    def save_champions(self, session: str, day: str,
                       rows: list[tuple[str, float, float]],
                       limit: int = 15) -> None:
        """يحفظ أبطال فترة (يستبدل لقطة نفس الفترة/اليوم). rows مرتّبة تنازليًا."""
        if not session or not day:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM session_champions WHERE session=? AND trade_date=?",
                (session, day))
            for i, (sym, chg, price) in enumerate(rows[:limit]):
                if not sym:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_champions(session, trade_date,"
                    " symbol, change_pct, price, rank) VALUES(?,?,?,?,?,?)",
                    (session, day, sym, chg, price, i))
            self._conn.commit()

    def get_session_champions(self, session: str,
                              on_or_before_day: str | None = None,
                              limit: int = 15, exact: bool = False) -> list[dict]:
        """أبطال آخر لقطة محفوظة لفترة (في/قبل يوم)، مرتّبة حسب rank.
        exact=True: اليوم بالضبط لا «في/قبل» — فالغياب يتدهور إلى **فارغ** بدل
        بعث يوم قديم اعتباطيًا (BUG-06: توريث الرسمي من بريماركت-اليوم)."""
        with self._lock:
            if on_or_before_day and exact:
                row = self._conn.execute(
                    "SELECT trade_date FROM session_champions WHERE session=?"
                    " AND trade_date=? LIMIT 1",
                    (session, on_or_before_day)).fetchone()
            elif on_or_before_day:
                row = self._conn.execute(
                    "SELECT trade_date FROM session_champions WHERE session=?"
                    " AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                    (session, on_or_before_day)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT trade_date FROM session_champions WHERE session=?"
                    " ORDER BY trade_date DESC LIMIT 1", (session,)).fetchone()
            if not row:
                return []
            rows = self._conn.execute(
                "SELECT symbol, change_pct, price, rank FROM session_champions"
                " WHERE session=? AND trade_date=? ORDER BY rank ASC LIMIT ?",
                (session, row["trade_date"], limit)).fetchall()
            return [dict(r) for r in rows]

    def inherited_champions(self, session: str, today: str) -> list[str]:
        """رموز أبطال الفترة السابقة (أولوية متابعة الفترة الحالية)."""
        if session not in _CHAMP_INHERIT:
            return []
        prev_sess, day_offset = _CHAMP_INHERIT[session]
        try:
            ref_day = (datetime.fromisoformat(today).date()
                       + timedelta(days=day_offset)).isoformat()
        except ValueError:
            return []
        # إزاحة 0 (توريث داخل نفس اليوم، كالرسمي←بريماركت) = اليوم بالضبط —
        # فالغياب يعطي فارغًا لا يومًا قديمًا (BUG-06).
        return [c["symbol"] for c in
                self.get_session_champions(prev_sess, ref_day, 15,
                                           exact=(day_offset == 0))
                if c.get("symbol")]

    # ── bot_meta ──────────────────────────────────────────────────
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bot_meta(key, value) VALUES(?,?)",
                (key, value))
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
