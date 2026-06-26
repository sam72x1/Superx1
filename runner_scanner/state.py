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

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Candidate, Session

logger = logging.getLogger(__name__)

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
    -- بيانات إضافية لتشريح الفشل (لماذا فشل السهم)
    short_pct       REAL,
    dilution_risk   TEXT,
    analyst_dir     TEXT,
    catalyst_head   TEXT,
    -- تتبّع النتيجة + الأحداث
    is_alert        INTEGER DEFAULT 0,
    first_price     REAL,
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
            # ترقية قواعد قديمة: إضافة أعمدة تشريح الفشل إن غابت
            for col, typ in (("short_pct", "REAL"), ("dilution_risk", "TEXT"),
                             ("analyst_dir", "TEXT"), ("catalyst_head", "TEXT"),
                             ("notified_missed", "INTEGER DEFAULT 0")):
                try:
                    self._conn.execute(
                        f"ALTER TABLE tracking ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass   # العمود موجود مسبقًا
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
                    change_pct, score, momentum, readiness, rvol, rvol_5min,
                    float_shares, float_source, halt_state, had_news, rejected,
                    reject_reason, short_pct, dilution_risk, analyst_dir,
                    catalyst_head, first_price, stop_price, target1, target2,
                    target3, high_after, low_after, max_gain_pct, notified_high,
                    outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?, 'open')
                ON CONFLICT(ticker, trade_date) DO UPDATE SET
                    logged_at=excluded.logged_at, session=excluded.session,
                    change_pct=excluded.change_pct, score=excluded.score,
                    momentum=excluded.momentum, readiness=excluded.readiness,
                    rvol=excluded.rvol, rvol_5min=excluded.rvol_5min,
                    float_shares=excluded.float_shares,
                    float_source=excluded.float_source,
                    halt_state=excluded.halt_state, had_news=excluded.had_news,
                    rejected=excluded.rejected, reject_reason=excluded.reject_reason,
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
                    c.snapshot.change_pct, c.final_score,
                    c.momentum.score if c.momentum else None,
                    c.readiness.classic_score if c.readiness else None,
                    c.momentum.rvol if c.momentum else None,
                    c.momentum.rvol_5min if c.momentum else None,
                    c.float_shares, c.float_source.value, c.halt_state.value,
                    had_news, 1 if c.is_rejected else 0, c.rejected_reason,
                    c.short_pct, dilution_risk, analyst_dir, catalyst_head,
                    price, stop, t1, t2, t3, price, price, price,
                ))
            self._conn.commit()

    # ── تتبّع النتائج + إصدار أحداث المتابعة (من السنابشوت) ───────
    def update_outcomes(self, price_map: dict[str, float],
                        now: datetime | None = None,
                        window_min: float = 90.0,
                        surge_leg_pct: float = 8.0,
                        missed_rise_pct: float = 1e9) -> list[dict]:
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
                first = r["first_price"] or price
                high = max(r["high_after"] or price, price)
                low = min(r["low_after"] or price, price)
                max_gain = (high - first) / first * 100.0 if first > 0 else 0.0
                max_draw = (low - first) / first * 100.0 if first > 0 else 0.0

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

                # 🎯 أهداف: نبلّغ كل هدف عُبر لأول مرة (نرسل للمُنبَّه فقط)
                while notified_t < len(targets) and high >= targets[notified_t]:
                    notified_t += 1
                    hit_target = 1
                    if not result:
                        result = "win"
                    if is_alert:
                        events.append({
                            "ticker": r["ticker"], "type": "target",
                            "level": notified_t, "price": targets[notified_t - 1],
                            "gain_pct": (targets[notified_t - 1] - first) / first * 100.0,
                        })

                # ⛔ الوقف: نبلّغ مرة واحدة (ويغلق التتبّع)
                if not notified_stop and r["stop_price"] and low <= r["stop_price"]:
                    notified_stop = 1
                    hit_stop = 1
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
                        })
                else:
                    notified_high = max(notified_high, high)

                # 👻 فرصة فائتة: سهم مرفوض (غير مُنبَّه) صعد ≥ العتبة — مرة واحدة
                notified_missed = r["notified_missed"] or 0
                if ((r["rejected"] or 0) and not is_alert and not notified_missed
                        and max_gain >= missed_rise_pct):
                    notified_missed = 1
                    events.append({
                        "ticker": r["ticker"], "type": "missed",
                        "price": high, "gain_pct": max_gain,
                        "reason": r["reject_reason"] or "",
                    })

                # حسم الإغلاق: الوقف، أو كل الأهداف، أو انتهاء النافذة
                if notified_stop:
                    outcome = "closed"
                elif targets and notified_t >= len(targets):
                    outcome = "closed"
                else:
                    try:
                        seen = datetime.fromisoformat(r["first_seen_at"])
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
                    " result=?, outcome=?, closed_at=? WHERE ticker=? AND trade_date=?",
                    (high, low, round(max_gain, 2), round(max_draw, 2),
                     hit_target, hit_stop, notified_t, notified_stop,
                     notified_high, notified_missed, result, outcome, closed_at,
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
                              limit: int = 15) -> list[dict]:
        """أبطال آخر لقطة محفوظة لفترة (في/قبل يوم)، مرتّبة حسب rank."""
        with self._lock:
            if on_or_before_day:
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
        return [c["symbol"] for c in
                self.get_session_champions(prev_sess, ref_day, 15)
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
