"""ترحيل مخطّط tracking (BUG-01): قاعدة قديمة تحصل على كل الأعمدة، واتّساق ذاتي
يمسك أي عمود يُضاف لـ_SCHEMA وينسى في _MIGRATIONS.

هذي أول تغطية لمسار الترحيل إطلاقًا — كل اختبار آخر يبني Store على ملف جديد
فيصدر CREATE TABLE المخطّط الحالي كاملًا، فترمي كل ALTER «موجود» وتُبتلع؛
أي أن الفرع الوحيد الذي يعمل على القرص المنشور هو الوحيد غير المغطّى (وهذا
سبب أن BUG-01 نجا). هنا نبني قاعدة بمخطّط قديم يدويًا لنمارس الترحيل فعلًا.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from runner_scanner.state import (
    _MIGRATIONS, _ORIGINAL_TRACKING_COLS, Store, _tracking_schema_columns)

# مخطّط tracking عند أول شحن (dd7dd34) — الأعمدة الأصلية فقط، بلا أيٍّ من
# الأعمدة المرحَّلة. نبنيه يدويًا لنحاكي قاعدة على قرص Render أقدم من التعديلات.
_OLD_DDL = """
CREATE TABLE tracking (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, first_seen_at TEXT,
    logged_at TEXT, session TEXT, change_pct REAL, score REAL, momentum REAL,
    readiness REAL, rvol REAL, rvol_5min REAL, float_shares REAL,
    float_source TEXT, halt_state TEXT, had_news INTEGER, rejected INTEGER,
    reject_reason TEXT, is_alert INTEGER DEFAULT 0, first_price REAL,
    stop_price REAL, target1 REAL, high_after REAL, low_after REAL,
    max_gain_pct REAL DEFAULT 0, max_draw_pct REAL DEFAULT 0,
    hit_target INTEGER DEFAULT 0, hit_stop INTEGER DEFAULT 0,
    outcome TEXT DEFAULT 'open', closed_at TEXT,
    PRIMARY KEY (ticker, trade_date)
);
"""


def test_migration_adds_all_columns_to_old_db():
    """قاعدة بمخطّط قديم (بلا الأعمدة الستة) → فتح Store يرحّلها كاملة.
    قبل الإصلاح كان INSERT في log_candidate يرمي «no such column: target2»."""
    db = os.path.join(tempfile.mkdtemp(), "old.sqlite3")
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_DDL)
    conn.commit()
    conn.close()
    # فتح Store على القاعدة القديمة يجب أن يرحّل بلا استثناء
    store = Store(db)
    cols = {r["name"] for r in store._conn.execute(
        "PRAGMA table_info(tracking)").fetchall()}
    for c in _tracking_schema_columns():
        assert c in cols, f"العمود {c} فاته الترحيل على قاعدة قديمة"
    # الأعمدة الستة تحديدًا (قلب BUG-01)
    for c in ("target2", "target3", "notified_targets", "notified_stop",
              "notified_high", "result"):
        assert c in cols
    store.close()


def test_schema_columns_all_covered_by_original_or_migrations():
    """اتّساق ذاتي: كل عمود tracking في _SCHEMA إمّا أصليّ أو في _MIGRATIONS.
    هذا يمسك **العمود المنسيّ القادم** — يجعل صنف BUG-01 مستحيل الإعادة."""
    migrated = {name for name, _ in _MIGRATIONS}
    covered = _ORIGINAL_TRACKING_COLS | migrated
    missing = _tracking_schema_columns() - covered
    assert not missing, f"أعمدة في _SCHEMA بلا ترحيل ولا أصل: {missing}"


def test_migration_is_idempotent_on_current_schema():
    """قاعدة حديثة (المخطّط الكامل) → الترحيل يُبتلع بلا خطأ (ALTER موجود)."""
    db = os.path.join(tempfile.mkdtemp(), "new.sqlite3")
    Store(db).close()
    Store(db).close()   # فتح ثانٍ لا يرمي
