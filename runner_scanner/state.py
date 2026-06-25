"""التخزين: منع تكرار التنبيه + closed-loop logging (القسم 12).

SQLite على قرص دائم. منع التكرار يُعاد تحميله عند الإقلاع تلقائيًا (لأنه
مخزَّن في الجدول) → إعادة التشغيل/النشر لا تكرّر التنبيه.

⚠️ يعتمد على القرص الدائم. بدونه يتكرّر الإزعاج (درس CCXI، القسم 14).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .models import Candidate

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    alerted_at  TEXT NOT NULL,
    score       REAL,
    PRIMARY KEY (ticker, trade_date)
);
CREATE TABLE IF NOT EXISTS closed_loop (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    logged_at     TEXT NOT NULL,
    session       TEXT,
    change_pct    REAL,
    score         REAL,
    momentum      REAL,
    readiness     REAL,
    rvol          REAL,
    rvol_5min     REAL,
    float_shares  REAL,
    float_source  TEXT,
    halt_state    TEXT,
    had_news      INTEGER,
    rejected      INTEGER,
    reject_reason TEXT
);
CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def trade_date_str(now: datetime | None = None) -> str:
    """تاريخ يوم التداول (UTC date) كمفتاح منع التكرار."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


class Store:
    """طبقة SQLite. آمِنة للثريدات عبر قفل + اتصال check_same_thread=False."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── منع التكرار ───────────────────────────────────────────────
    def already_alerted(self, ticker: str, now: datetime | None = None) -> bool:
        day = trade_date_str(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE ticker=? AND trade_date=?",
                (ticker, day)).fetchone()
            return row is not None

    def mark_alerted(self, ticker: str, score: float,
                     now: datetime | None = None) -> None:
        day = trade_date_str(now)
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts(ticker, trade_date, alerted_at, score)"
                " VALUES(?,?,?,?)", (ticker, day, ts, score))
            self._conn.commit()

    # ── closed-loop logging ───────────────────────────────────────
    def log_candidate(self, c: Candidate, now: datetime | None = None) -> None:
        day = trade_date_str(now)
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO closed_loop(ticker, trade_date, logged_at, session,"
                " change_pct, score, momentum, readiness, rvol, rvol_5min,"
                " float_shares, float_source, halt_state, had_news, rejected,"
                " reject_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c.ticker, day, ts, c.session.value,
                    c.snapshot.change_pct, c.final_score,
                    c.momentum.score if c.momentum else None,
                    c.readiness.classic_score if c.readiness else None,
                    c.momentum.rvol if c.momentum else None,
                    c.momentum.rvol_5min if c.momentum else None,
                    c.float_shares, c.float_source.value,
                    c.halt_state.value,
                    1 if (c.catalyst and c.catalyst.has_news) else 0,
                    1 if c.is_rejected else 0,
                    c.rejected_reason,
                ))
            self._conn.commit()

    # ── bot_meta (للمعايرة/الحالة) ────────────────────────────────
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
