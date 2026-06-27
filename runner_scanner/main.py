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
from zoneinfo import ZoneInfo

from . import advisor, detector, market_calendar, postmortem
from .alerts import TelegramSender, build_card, build_followup, prioritize
from .analyst import ClaudeAnalyst
from .cache import DailyCache
from .config import Config
from .llm import ClaudeClient
from .render_client import RenderClient
from .dev_assistant import send_report_and_files
from .halts import HaltTracker
from .massive_client import MassiveClient, MassiveError
from .models import Candidate, Session
from .monitor import HealthMonitor
from .pipeline import process_candidate
from .sec_radar import SecRadar
from .sessions import classify_session, is_scanning_window, now_et
from .short_interest import ShortInterestProvider
from .state import Store, trade_date_str
from .telegram_bot import TelegramAssistant

logger = logging.getLogger(__name__)


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
        self.short = ShortInterestProvider()
        self.cache = DailyCache()   # كاش يومي للبيانات البطيئة
        self.analyst = ClaudeAnalyst(cfg)   # محلّل ذكي لكل تنبيه
        self.sec_radar = SecRadar(cfg)      # رادار التخفيف (SEC EDGAR)
        self.render = RenderClient(cfg)     # وعي/تحكّم ريندر
        self.claude = ClaudeClient(cfg.anthropic_api_key)   # للبريفنغ/المساعد
        self.monitor = HealthMonitor(
            notify=self.telegram.send,
            stall_seconds=max(300.0, cfg.poll_interval_sec * 8),
        )
        self.last_runners: list = []       # (رمز، نسبة) لآخر مسح (للمساعد)
        self.last_scan_et = None
        self.assistant = TelegramAssistant(self)   # مساعد تيليجرام تفاعلي
        self._stop = threading.Event()

    # ── دورة مسح واحدة ────────────────────────────────────────────
    def run_cycle(self, et_now=None) -> int:
        """دورة واحدة: كشف → معالجة → تنبيه. يرجّع عدد التنبيهات المرسلة."""
        et_now = et_now or now_et()
        session = classify_session(self.cfg, et_now)
        snapshot = self.client.full_snapshot()
        runners = detector.detect_runners(self.cfg, snapshot)
        # أعلى N سهم صعودًا فقط (قرار المستخدم: 15) — detect_runners مرتّب تنازليًا
        top = (runners[:self.cfg.top_n_runners]
               if self.cfg.top_n_runners > 0 else runners)
        et_date = trade_date_str(et_now)
        self.last_runners = [(e.ticker, e.change_pct) for e in top]
        self.last_scan_et = et_now

        # أبطال الفترة السابقة الموروثون (أولوية متابعة، حتى لو خرجوا من الـ15)
        champ_syms: set[str] = set()
        champ_entries: list = []
        if self.cfg.champions_enabled:
            top_tickers = {e.ticker for e in top}
            by_ticker = {e.ticker: e for e in snapshot if e.is_valid}
            for sym in self.store.inherited_champions(session.value, et_date):
                if sym in by_ticker and sym not in top_tickers:
                    champ_entries.append(by_ticker[sym])
                    champ_syms.add(sym)
        logger.info("الجلسة %s — فوق العتبة: %d · أعلى %d + %d بطل موروث",
                    session.value, len(runners), len(top), len(champ_entries))

        # حسم أي تتبّعات معلّقة من أيام سابقة (لم تكتمل نافذتها قبل الإغلاق)
        self.store.finalize_stale(et_now)

        # تحديث نتائج التنبيهات المفتوحة من نفس السنابشوت (بلا نداء إضافي)
        # ويصدّر أحداث متابعة (🎯 هدف · ⛔ وقف · 🚀 قفزة) نرسلها فورًا.
        price_map = {e.ticker: e.last_price for e in snapshot if e.is_valid}
        volume_map = {e.ticker: e.day_volume for e in snapshot if e.is_valid}
        events = self.store.update_outcomes(
            price_map, et_now, window_min=self.cfg.outcome_window_min,
            surge_leg_pct=self.cfg.surge_leg_pct,
            missed_rise_pct=(self.cfg.missed_rise_pct
                             if self.cfg.missed_alert_enabled else 1e9),
            volume_map=volume_map)
        for ev in events:
            if not self.telegram.send(build_followup(self.cfg, ev)):
                logger.warning("فشل إرسال حدث متابعة محسوم في DB (قد يُفقد): %s %s",
                               ev.get("ticker"), ev.get("type"))
            # 🔍 تشريح لحظي عند كسر الوقف: لماذا فشل السهم؟
            if ev.get("type") == "stop" and self.cfg.postmortem_on_stop:
                row = self.store.fetch_row(ev["ticker"], et_date)
                if row is not None:
                    self.telegram.send(postmortem.build_failure_message(
                        self.cfg, row, client=self.claude))
        if events:
            logger.info("أُرسل %d تحديث متابعة", len(events))

        accepted: list[Candidate] = []
        # الأبطال الموروثون أولًا (أولوية متابعة)، ثم أعلى 15 صعودًا
        for snap in champ_entries + top:
            # منع التكرار (تنبيه/سهم/يوم) — يُعاد تحميله من DB عند الإقلاع
            if self.cfg.dedup_per_day and \
                    self.store.already_alerted(snap.ticker):
                continue
            try:
                cand = process_candidate(
                    self.cfg, self.client, snap, halts=self.halts,
                    session=session, et_now=et_now, short_provider=self.short,
                    cache=self.cache, analyst=self.analyst,
                    sec_radar=self.sec_radar)
            except MassiveError as exc:
                logger.warning("معالجة %s فشلت: %s", snap.ticker, exc)
                continue
            except Exception:  # noqa: BLE001 — اعزل فشل سهم واحد عن بقية الدورة
                logger.exception("معالجة %s فشلت باستثناء غير متوقّع", snap.ticker)
                continue
            cand.is_champion = snap.ticker in champ_syms
            self.store.log_candidate(cand)   # closed-loop لكل مرشّح
            if not cand.is_rejected:
                accepted.append(cand)

        # حفظ أبطال هذي الفترة (أعلى 15 صعودًا) للتوريث للفترة التالية
        if self.cfg.champions_enabled and top:
            # أبطال بحجم تداول فعلي فقط (لا طبعة بريماركت رقيقة تُورَّث كأولوية)
            self.store.save_champions(
                session.value, et_date,
                [(e.ticker, e.change_pct, e.last_price) for e in top
                 if e.day_volume > 0])

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
        self.assistant.start()   # مساعد تيليجرام تفاعلي
        # رسالة إقلاع: تأكيد أن البوت نُشر وموصول بتيليجرام
        session = classify_session(self.cfg)
        self.telegram.send(
            "🚀 <b>الماسح الشامل اشتغل</b>\n"
            f"الجلسة الحالية: {session.value} · "
            f"المسح كل {self.cfg.poll_interval_sec}ث\n"
            f"📦 SHA: {self.cfg.code_version or 'غير معروف'}\n"
            "<i>صامت عند الصحة، ينبّه فقط عند سهم مؤهّل أو عطل.</i>")
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
                # احترام Retry-After عند 429 (تهدئة بدل قصف المزوّد)
                wait = getattr(exc, "retry_after", None)
                if wait:
                    self._stop.wait(min(float(wait), 120.0))
            except Exception as exc:  # noqa: BLE001
                logger.exception("خطأ غير متوقّع في الدورة")
                self.monitor.raise_fault("cycle", str(exc))
            self._maybe_daily_report()
            self._maybe_advisor_briefing()
            self._maybe_backtest()
            self.monitor.check_stall()
            # نوم حتى الدورة التالية (يحترم وقت الدورة المنقضي)
            elapsed = time.monotonic() - cycle_start
            self._stop.wait(max(1.0, self.cfg.poll_interval_sec - elapsed))

    def _maybe_daily_report(self, et_now=None) -> None:
        """يرسل تقرير التطوير + ملفات CSV على جدول (افتراضيًا الأربعاء والسبت
        فجرًا بتوقيت الرياض، بعد إغلاق السوق) لتتراكم النتائج بين تقريرين."""
        if not self.cfg.dev_report_on_close:
            return
        et_now = et_now or now_et()
        try:
            tz = ZoneInfo(self.cfg.display_tz)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("Asia/Riyadh")
        local = et_now.astimezone(tz)
        if local.weekday() not in self.cfg.dev_report_weekdays:
            return
        if local.hour < self.cfg.dev_report_hour:
            return
        key = local.strftime("%Y-%m-%d")     # تقرير واحد لكل يوم مجدوَل
        if self.store.get_meta("last_dev_report") == key:
            return
        # لا ترسل تقريرًا فاضيًا (لا نشاط متراكم)
        has_activity = (self.store.fetch_resolved() or
                        self.store.fetch_missed(self.cfg.missed_rise_pct))
        if not has_activity:
            return
        send_report_and_files(self.store, self.cfg, self.telegram)
        self.store.set_meta("last_dev_report", key)
        logger.info("أُرسل تقرير التطوير المجدوَل + ملفات CSV (%s)", key)

    def _maybe_advisor_briefing(self, et_now=None) -> None:
        """بريفنغ المستشار في نهاية يوم التداول (بعد الإغلاق)، مرة/يوم."""
        if not self.cfg.advisor_enabled:
            return
        et_now = et_now or now_et()
        if classify_session(self.cfg, et_now) is not Session.CLOSED:
            return
        end_hour = (market_calendar.EARLY_CLOSE_HOUR
                    if market_calendar.is_early_close(et_now.date())
                    else self.cfg.afterhours_end_hour)
        if et_now.hour + et_now.minute / 60.0 < end_hour:
            return   # لسه ما انتهى يوم التداول (تجنّب إطلاق بعد منتصف الليل)
        key = trade_date_str(et_now)
        if self.store.get_meta("last_advisor") == key:
            return
        text = advisor.build_briefing(
            self.cfg, self.store, render_summary=self.render.summary(),
            health_faults=self.monitor.active_faults(), now=et_now,
            client=self.claude)
        if self.telegram.send(text):
            self.store.set_meta("last_advisor", key)
            logger.info("أُرسل بريفنغ المستشار (%s)", key)

    def _maybe_backtest(self, et_now=None) -> None:
        """باكتيست أسبوعي **تلقائي** في الخلفية (بلا تدخّل): يقيس حافة الاستراتيجية
        على آخر ~30 يوم تداول ويرسل النتيجة على تيليجرام. مرة/أسبوع."""
        if not self.cfg.backtest_enabled or self.cfg.dry_run:
            return
        if not self.cfg.massive_api_key:
            return
        et_now = et_now or now_et()
        try:
            tz = ZoneInfo(self.cfg.display_tz)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("Asia/Riyadh")
        local = et_now.astimezone(tz)
        if local.weekday() != self.cfg.backtest_weekday:
            return
        if local.hour < self.cfg.backtest_hour:
            return
        key = local.strftime("%Y-W%W")        # مفتاح أسبوعي
        if self.store.get_meta("last_backtest") == key:
            return
        self.store.set_meta("last_backtest", key)   # قبل البدء: يمنع إعادة الإطلاق
        threading.Thread(target=self._run_backtest_bg, args=(et_now,),
                         daemon=True, name="backtest").start()

    def _run_backtest_bg(self, et_now) -> None:
        from datetime import timedelta
        from . import backtest, backtest_grid
        end = (et_now.date() - timedelta(days=1)).isoformat()
        start = (et_now.date()
                 - timedelta(days=self.cfg.backtest_lookback_days)).isoformat()
        try:
            self.telegram.send(
                f"🧪 <b>باكتيست تلقائي</b> {start} → {end}\n"
                "<i>يقيس حافة الاستراتيجية على الماضي — قد يأخذ دقائق…</i>")
            # عميل مُذكّر: الجلب الشبكي مرة واحدة يُشارَك بين التقرير والمعايرة
            client = backtest_grid.memoized(self.client)
            res = backtest.run_backtest(self.cfg, client, start, end)
            self.telegram.send(backtest.format_report(res))
            logger.info("اكتمل الباكتيست التلقائي (%d صفقة)", len(res.trades))
            # ── معايرة العتبات A/B (اقتراح أفضل عتبات — لا تطبيق) ──
            if self.cfg.backtest_grid_enabled:
                grid = backtest_grid.run_grid(self.cfg, client, start, end)
                self.telegram.send(backtest_grid.format_grid_report(grid))
                logger.info("اكتملت معايرة العتبات A/B")
        except Exception as exc:  # noqa: BLE001
            logger.exception("الباكتيست التلقائي فشل")
            self.telegram.send(f"⚠️ تعذّر الباكتيست التلقائي: {exc}")

    def shutdown(self) -> None:
        logger.info("إيقاف الماسح...")
        self._stop.set()
        self.halts.stop()
        self.assistant.stop()
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

    logger.info("🚀 الماسح الشامل اشتغل (poll=%ds, dry_run=%s)",
                cfg.poll_interval_sec, cfg.dry_run)
    # سطر صريح للبحث عنه بكلمة «SHA» في سجلّات Render للتأكّد من الإصدار المنشور
    logger.info("📦 SHA الإصدار المنشور: %s", cfg.code_version or "غير معروف")
    try:
        scanner.loop()
    finally:
        scanner.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
