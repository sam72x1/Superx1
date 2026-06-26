"""التخزين: منع تكرار + closed-loop + تتبّع نتائج التنبيهات (القسم 12).

SQLite على قرص دائم. ثلاث وظائف:
1. منع تكرار التنبيه (تنبيه/سهم/يوم) — يُعاد تحميله عند الإقلاع تلقائيًا.
2. closed-loop: صفّ واحد لكل سهم/يوم يحمل تحليله ونتيجته (للمعايرة لاحقًا).
3. تتبّع النتائج: نتابع سعر كل مرشّح (مُنبَّه أو مرفوض) من السنابشوت —
   هل استمر الرَنر صعودًا لهدفه (نجاح) أم انهار للوقف؟ وكم أقصى ربح؟
   هذا أساس أداة التطوير (dev_assistant).

⚠️ يعتمد على القرص الدائم (درس CCXI، القسم 14).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Candidate

logger = logging.getLogger(__name__)

# يوم التداول يُحسب بتوقيت نيويورك (السوق ET) لتجنّب اختلاف التاريخ قرب
# منتصف الليل UTC بين تسجيل المرشّح وتحديث نتيجته.
_ET = ZoneInfo("America/New_York")

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
-- صفّ واحد لكل سهم/يوم: تحليله (closed-loop) + نتيجته (تتبّع)
CREATE TABLE IF NOT EXISTS tracking (
    ticker        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    first_seen_at TEXT,
    logged_at     TEXT,
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
    reject_reason TEXT,
    -- تتبّع النتيجة
    is_alert      INTEGER DEFAULT 0,
    first_price   REAL,
    stop_price    REAL,
    target1       REAL,
    high_after    REAL,
    low_after     REAL,
    max_gain_pct  REAL DEFAULT 0,
    max_draw_pct  REAL DEFAULT 0,
    hit_target    INTEGER DEFAULT 0,
    hit_stop      INTEGER DEFAULT 0,
    outcome       TEXT DEFAULT 'open',   -- open / win / loss / timeout
    closed_at     TEXT,
    PRIMARY KEY (ticker, trade_date)
);
"""


def trade_date_str(now: datetime | None = None) -> str:
    """تاريخ يوم التداول (بتوقيت ET) كمفتاح موحّد."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_ET).strftime("%Y-%m-%d")


def _iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


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
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts(ticker, trade_date, alerted_at, score)"
                " VALUES(?,?,?,?)", (ticker, day, _iso(now), score))
            self._conn.execute(
                "UPDATE tracking SET is_alert=1 WHERE ticker=? AND trade_date=?",
                (ticker, day))
            self._conn.commit()

    # ── closed-loop + تهيئة التتبّع (upsert صفّ واحد/سهم/يوم) ──────
    def log_candidate(self, c: Candidate, now: datetime | None = None) -> None:
        """يسجّل تحليل المرشّح ويهيّئ تتبّع نتيجته. لا يكرّر first_price."""
        day = trade_date_str(now)
        ts = _iso(now)
        price = c.snapshot.last_price
        stop = c.risk.stop_price if c.risk else None
        target1 = c.risk.targets[0] if (c.risk and c.risk.targets) else None
        had_news = 1 if (c.catalyst and c.catalyst.has_news) else 0
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracking (
                    ticker, trade_date, first_seen_at, logged_at, session,
                    change_pct, score, momentum, readiness, rvol, rvol_5min,
                    float_shares, float_source, halt_state, had_news, rejected,
                    reject_reason, first_price, stop_price, target1,
                    high_after, low_after, max_gain_pct, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open')
                ON CONFLICT(ticker, trade_date) DO UPDATE SET
                    logged_at=excluded.logged_at,
                    session=excluded.session,
                    change_pct=excluded.change_pct,
                    score=excluded.score,
                    momentum=excluded.momentum,
                    readiness=excluded.readiness,
                    rvol=excluded.rvol,
                    rvol_5min=excluded.rvol_5min,
                    float_shares=excluded.float_shares,
                    float_source=excluded.float_source,
                    halt_state=excluded.halt_state,
                    had_news=excluded.had_news,
                    rejected=excluded.rejected,
                    reject_reason=excluded.reject_reason,
                    -- لا نمسّ first_price/high/low/outcome (تبقى من أول رؤية)
                    stop_price=COALESCE(tracking.stop_price, excluded.stop_price),
                    target1=COALESCE(tracking.target1, excluded.target1)
                """,
                (
                    c.ticker, day, ts, ts, c.session.value,
                    c.snapshot.change_pct,
                    c.final_score,
                    c.momentum.score if c.momentum else None,
                    c.readiness.classic_score if c.readiness else None,
                    c.momentum.rvol if c.momentum else None,
                    c.momentum.rvol_5min if c.momentum else None,
                    c.float_shares, c.float_source.value, c.halt_state.value,
                    had_news, 1 if c.is_rejected else 0, c.rejected_reason,
                    price, stop, target1, price, price,
                ))
            self._conn.commit()

    # ── تتبّع النتائج من السنابشوت (بلا نداء API إضافي) ───────────
    def update_outcomes(self, price_map: dict[str, float],
                        now: datetime | None = None,
                        window_min: float = 90.0) -> int:
        """يحدّث القمم/القيعان لكل تتبّع مفتوح، ويحسم النتيجة عند بلوغ
        الهدف/الوقف أو انتهاء النافذة. يرجّع عدد المحسومة هذي الدورة."""
        now = now or datetime.now(timezone.utc)
        day = trade_date_str(now)
        closed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracking WHERE outcome='open' AND trade_date=?",
                (day,)).fetchall()
            for r in rows:
                price = price_map.get(r["ticker"])
                if price is None or price <= 0:
                    continue
                first = r["first_price"] or price
                high = max(r["high_after"] or price, price)
                low = min(r["low_after"] or price, price)
                max_gain = (high - first) / first * 100.0 if first > 0 else 0.0
                max_draw = (low - first) / first * 100.0 if first > 0 else 0.0
                hit_target = 1 if (r["target1"] and high >= r["target1"]) else 0
                hit_stop = 1 if (r["stop_price"] and low <= r["stop_price"]) else 0

                outcome = "open"
                # نحسم: الهدف أولًا (نجاح)، ثم الوقف (خسارة)، ثم انتهاء النافذة
                if hit_target:
                    outcome = "win"
                elif hit_stop:
                    outcome = "loss"
                else:
                    try:
                        seen = datetime.fromisoformat(r["first_seen_at"])
                        elapsed = (now - seen).total_seconds() / 60.0
                    except (TypeError, ValueError):
                        elapsed = 0.0
                    if elapsed >= window_min:
                        outcome = "timeout"

                closed_at = _iso(now) if outcome != "open" else None
                if outcome != "open":
                    closed += 1
                self._conn.execute(
                    "UPDATE tracking SET high_after=?, low_after=?, max_gain_pct=?,"
                    " max_draw_pct=?, hit_target=?, hit_stop=?, outcome=?, closed_at=?"
                    " WHERE ticker=? AND trade_date=?",
                    (high, low, round(max_gain, 2), round(max_draw, 2),
                     hit_target, hit_stop, outcome, closed_at,
                     r["ticker"], r["trade_date"]))
            self._conn.commit()
        return closed

    # ── استعلامات أداة التطوير ────────────────────────────────────
    def fetch_resolved(self, only_alerts: bool = False) -> list[sqlite3.Row]:
        """كل التتبّعات المحسومة (win/loss/timeout). للتحليل التطويري."""
        q = "SELECT * FROM tracking WHERE outcome != 'open'"
        if only_alerts:
            q += " AND is_alert=1"
        with self._lock:
            return self._conn.execute(q).fetchall()

    def fetch_missed(self, min_rise_pct: float) -> list[sqlite3.Row]:
        """المرفوضون اللي صعدوا ≥ نسبة بعد الرفض (فرص فائتة)."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM tracking WHERE rejected=1 AND max_gain_pct >= ?"
                " ORDER BY max_gain_pct DESC", (min_rise_pct,)).fetchall()

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
