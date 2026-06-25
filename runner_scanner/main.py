"""نقطة الدخول: حلقة REST + keep-alive + ثريد التوقّفات + خط المعالجة.

التشغيل:  python -m runner_scanner.main
الاستضافة: Render Background Worker + قرص دائم (DB_PATH على القرص).
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import detector
from .alerts import TelegramSender, build_card, prioritize
from .config import Config
from .halts import HaltTracker
from .massive_client import MassiveClient, MassiveError
from .models import Candidate
from .monitor import HealthMonitor
from .pipeline import process_candidate
from .sessions import classify_session, is_scanning_window, now_et
from .state import Store

logger = logging.getLogger(__name__)

# أقصى عدد مرشّحين نحلّلهم بعمق لكل دورة (الأعلى تغيّرًا أولًا) — حماية من
# اليوم الحار. البقية تُحلَّل في الدورات التالية.
MAX_DEEP_PER_CYCLE = 40


class _KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"runner_scanner alive")

    def log_message(self, *args):  # كتم لوق الـ HTTP
        return


def _start_keepalive(port: int) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), _KeepAliveHandler)
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="keepalive").start()
    logger.info("keep-alive يستمع على المنفذ %d", port)
    return server


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = MassiveClient(cfg)
        self.store = Store(cfg.db_path)
        self.telegram = TelegramSender(cfg)
        self.halts = HaltTracker(cfg)
        self.monitor = HealthMonitor(
            notify=self.telegram.send,
            stall_seconds=max(300.0, cfg.poll_interval_sec * 8),
        )
        self._stop = threading.Event()

    # ── دورة مسح واحدة ────────────────────────────────────────────
    def run_cycle(self, et_now=None) -> int:
        """دورة واحدة: كشف → معالجة → تنبيه. يرجّع عدد التنبيهات المرسلة."""
        et_now = et_now or now_et()
        session = classify_session(self.cfg, et_now)
        snapshot = self.client.full_snapshot()
        runners = detector.detect_runners(self.cfg, snapshot)
        logger.info("الجلسة %s — مرشّحون فوق العتبة: %d", session.value,
                    len(runners))

        accepted: list[Candidate] = []
        deep = 0
        for snap in runners:
            if deep >= MAX_DEEP_PER_CYCLE:
                logger.info("بلغنا حد التحليل العميق (%d)، البقية للدورة القادمة",
                            MAX_DEEP_PER_CYCLE)
                break
            # منع التكرار (تنبيه/سهم/يوم) — يُعاد تحميله من DB عند الإقلاع
            if self.cfg.dedup_per_day and \
                    self.store.already_alerted(snap.ticker):
                continue
            deep += 1
            try:
                cand = process_candidate(
                    self.cfg, self.client, snap, halts=self.halts,
                    session=session, et_now=et_now)
            except MassiveError as exc:
                logger.warning("معالجة %s فشلت: %s", snap.ticker, exc)
                continue
            self.store.log_candidate(cand)   # closed-loop لكل مرشّح
            if not cand.is_rejected:
                accepted.append(cand)

        # ترتيب الأولوية ثم الإرسال
        sent = 0
        for cand in prioritize(accepted):
            if self.telegram.send(build_card(self.cfg, cand)):
                self.store.mark_alerted(cand.ticker, cand.final_score)
                sent += 1
        if sent:
            logger.info("أُرسل %d تنبيه", sent)
        return sent

    # ── الحلقة الرئيسية ───────────────────────────────────────────
    def loop(self) -> None:
        self.halts.start()
        while not self._stop.is_set():
            cycle_start = time.monotonic()
            try:
                if is_scanning_window(self.cfg):
                    self.run_cycle()
                    self.monitor.heartbeat()
                    self.monitor.clear_fault("api")
                else:
                    logger.debug("خارج نافذة المسح — انتظار")
                    self.monitor.heartbeat()  # خارج الجلسة ليس عطلًا
            except MassiveError as exc:
                self.monitor.raise_fault("api", str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception("خطأ غير متوقّع في الدورة")
                self.monitor.raise_fault("cycle", str(exc))
            self.monitor.check_stall()
            # نوم حتى الدورة التالية (يحترم وقت الدورة المنقضي)
            elapsed = time.monotonic() - cycle_start
            self._stop.wait(max(1.0, self.cfg.poll_interval_sec - elapsed))

    def shutdown(self) -> None:
        logger.info("إيقاف الماسح...")
        self._stop.set()
        self.halts.stop()
        self.store.close()


def main() -> int:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    missing = cfg.missing_required()
    if missing and not cfg.dry_run:
        logger.error("متغيّرات إلزامية ناقصة: %s", ", ".join(missing))
        logger.error("أنشئ .env (انظر .env.example) قبل التشغيل.")
        return 2

    _start_keepalive(cfg.keepalive_port)
    scanner = Scanner(cfg)

    def _handle_signal(signum, _frame):
        logger.info("استلمنا إشارة %s", signum)
        scanner.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("🚀 ماسح الرَنرات اشتغل (poll=%ds, dry_run=%s)",
                cfg.poll_interval_sec, cfg.dry_run)
    try:
        scanner.loop()
    finally:
        scanner.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
