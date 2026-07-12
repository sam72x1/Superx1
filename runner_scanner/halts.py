"""معالجة التوقّفات LULD / T12 (القسم 7) — آلة حالة مغذّاة بـ WebSocket.

حقيقة من تحقّق الـ API: حالة التوقّف **لا تجي عبر REST**، فقط عبر بثّ
WebSocket اللحظي (قناة LULD + صفقات T). لذا نشغّل مستهلك WebSocket خفيف
في ثريد منفصل بجانب حلقة الـ REST. هذا يخفّف قرار «REST فقط» (القرار 7)
لكنه إلزامي لتحقيق «معالجة LULD كاملة» (القرار 2).

التصميم قابل للاختبار: الثريد فقط يفكّ JSON ويمرّر أحداثًا مُطبّعة إلى
HaltTracker.process_event(...) — الاختبارات تغذّي الأحداث مباشرة بدون سوكِت.

القواعد:
1. متوقّف (HALTED) → لا بطاقة، وسمه HALTED.
2. بعد الاستئناف → تجاهل أول resume_ignore_seconds ثم أعد الحساب (الحالة
   RESUMED خلال النافذة، ثم NORMAL).
3. T12 (توقّف طويل/إفصاح) → استبعاد نهائي (يبقى T12).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from .config import Config
from .models import HaltState

logger = logging.getLogger(__name__)

# مؤشرات شرط الصفقة لتوقّف/استئناف التداول (ناسداك للإشارات الصريحة 17/18)
HALT_CONDITIONS = {17}        # Trading Halt
RESUME_CONDITIONS = {18}      # Resumption

# نافذة تجاهل ما بعد الاستئناف (ثوانٍ) — القسم 7 (1–5 دقائق)
DEFAULT_RESUME_IGNORE_SEC = 180.0
# توقّف يتجاوز هذا (ثوانٍ) يُرجَّح كـ T12 / توقّف إفصاح → استبعاد
DEFAULT_T12_SECONDS = 1800.0


class HaltTracker:
    """يتتبّع حالة توقّف كل سهم، آمِن للثريدات."""

    def __init__(self, cfg: Config,
                 resume_ignore_sec: float = DEFAULT_RESUME_IGNORE_SEC,
                 t12_seconds: float = DEFAULT_T12_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.resume_ignore_sec = resume_ignore_sec
        self.t12_seconds = t12_seconds
        self._clock = clock
        self._lock = threading.RLock()
        # ticker -> dict(state, since, halted_at)
        self._states: dict[str, dict] = {}
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── واجهة الاستعلام (تُستخدم من حلقة المسح) ───────────────────
    def _entry(self, ticker: str) -> dict:
        return self._states.setdefault(
            ticker, {"state": HaltState.NORMAL, "since": self._clock(),
                     "halted_at": None})

    def state_of(self, ticker: str) -> HaltState:
        """يرجّع الحالة الحالية، مع ترقية HALTED→RESUMED→NORMAL/T12 حسب الزمن."""
        with self._lock:
            e = self._entry(ticker)
            now = self._clock()
            st = e["state"]
            if st is HaltState.HALTED and e["halted_at"] is not None:
                # توقّف طويل → T12 (استبعاد نهائي)
                if now - e["halted_at"] >= self.t12_seconds:
                    e["state"] = HaltState.T12
                    return HaltState.T12
            if st is HaltState.RESUMED:
                if now - e["since"] >= self.resume_ignore_sec:
                    e["state"] = HaltState.NORMAL
                    return HaltState.NORMAL
            return e["state"]

    def is_tradeable(self, ticker: str) -> bool:
        """صحيح فقط لو طبيعي (لا توقّف ولا داخل نافذة التجاهل ولا T12)."""
        return self.state_of(ticker) is HaltState.NORMAL

    def is_excluded(self, ticker: str) -> bool:
        """T12 = استبعاد نهائي."""
        return self.state_of(ticker) is HaltState.T12

    # ── تغذية الأحداث (من WebSocket أو الاختبارات) ────────────────
    def process_event(self, event: dict) -> None:
        """حدث WebSocket مُطبّع. ev في {LULD, T, status}."""
        ev = event.get("ev")
        ticker = event.get("sym") or event.get("T") or event.get("ticker")
        if not ticker:
            return
        with self._lock:
            if ev == "T":
                self._handle_trade(ticker, event)
            elif ev == "LULD":
                self._handle_luld(ticker, event)
            elif ev == "status":
                self._handle_status(ticker, event)

    def _set(self, ticker: str, state: HaltState) -> None:
        e = self._entry(ticker)
        now = self._clock()
        if e["state"] is HaltState.T12:
            return  # نهائي، لا رجوع
        if state is HaltState.HALTED and e["state"] is not HaltState.HALTED:
            e["halted_at"] = now
        if state is HaltState.RESUMED:
            # نافذة التجاهل تبدأ الآن
            pass
        e["state"] = state
        e["since"] = now

    def _handle_trade(self, ticker: str, event: dict) -> None:
        conds = set(event.get("c") or [])
        if conds & HALT_CONDITIONS:
            self._mark_halted(ticker)
            return
        if conds & RESUME_CONDITIONS:
            self._mark_resumed(ticker)
            return
        # صفقة عادية أثناء توقّف ظاهري → السهم يتداول فعلًا → استئناف
        e = self._entry(ticker)
        if e["state"] is HaltState.HALTED:
            self._mark_resumed(ticker)

    def _handle_luld(self, ticker: str, event: dict) -> None:
        # مؤشّر straddle/limit-state يدلّ على ضغط توقّف وشيك أو قائم.
        indicators = set(event.get("i") or [])
        # 1=limit up, 2=limit down (تقريب) → احتمال توقّف
        if indicators & {1, 2}:
            self._mark_halted(ticker)

    def _handle_status(self, ticker: str, event: dict) -> None:
        status = (event.get("status") or "").lower()
        if "halt" in status:
            self._mark_halted(ticker)
        elif "resum" in status or "trading" in status:
            self._mark_resumed(ticker)

    def _mark_halted(self, ticker: str) -> None:
        self._set(ticker, HaltState.HALTED)
        logger.info("توقّف: %s", ticker)

    def _mark_resumed(self, ticker: str) -> None:
        e = self._entry(ticker)
        if e["state"] in (HaltState.HALTED,):
            self._set(ticker, HaltState.RESUMED)
            logger.info("استئناف: %s (نافذة تجاهل %.0fث)", ticker,
                        self.resume_ignore_sec)

    # ── دورة حياة الـ WebSocket ───────────────────────────────────
    def start(self) -> None:
        """يشغّل ثريد مستهلك WebSocket (best-effort، لا يُسقط البوت لو فشل)."""
        if not self.cfg.halts_enabled:
            logger.info("التوقّفات معطّلة (HALTS_ENABLED=false)")
            return
        self._thread = threading.Thread(target=self._run_ws, daemon=True,
                                        name="halt-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        try:
            if ws is not None:
                ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _run_ws(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            logger.warning("websocket-client غير مثبّت — التوقّفات معطّلة")
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    self.cfg.massive_ws_url, timeout=30)
                with self._lock:
                    self._ws = ws
                self._ws.send(json.dumps(
                    {"action": "auth", "params": self.cfg.massive_api_key}))
                # نشترك في كل LULD والصفقات (لربط أحداث الـ band بانقطاع التداول)
                self._ws.send(json.dumps(
                    {"action": "subscribe", "params": "LULD.*,T.*"}))
                backoff = 1.0
                logger.info("WebSocket التوقّفات متصل")
                while not self._stop.is_set():
                    raw = self._ws.recv()
                    if not raw:
                        continue
                    self._dispatch_raw(raw)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                # أغلق المقبس المكسور قبل إعادة الاتصال — وإلا تتسرّب مقابس
                # نظام التشغيل عبر إعادات الاتصال في عملية طويلة العمر.
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:  # noqa: BLE001 — الإغلاق best-effort
                    pass
                logger.warning("WebSocket التوقّفات انقطع: %s — إعادة بعد %.0fث",
                               exc, backoff)
                time.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    def _dispatch_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if isinstance(ev, dict):
                self.process_event(ev)
