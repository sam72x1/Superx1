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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from . import advisor, detector, market_calendar, postmortem
from .alerts import TelegramSender, build_card, build_followup, prioritize
from .analyst import ClaudeAnalyst
from .cache import DailyCache
from .config import Config
from .llm import ClaudeClient
from .kronos_shadow import (
    KronosShadowClient,
    KronosShadowWorker,
    kronos_selection_revision,
)
from .render_client import RenderClient
from .dev_assistant import send_report_and_files
from .halts import HaltTracker
from .massive_client import MassiveClient, MassiveError
from .models import Candidate, Session
from .monitor import HealthMonitor
from .pipeline import process_candidate
from .sec_radar import SecRadar
from .sessions import (
    classify_session, is_opening_range, is_scanning_window, now_et,
)
from .short_interest import ShortInterestProvider
from .state import Store, trade_date_str
from .telegram_bot import TelegramAssistant
from .textutil import esc

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


def _kronos_session_end(
    cfg: Config, session: Session, et_now: datetime,
) -> datetime | None:
    """نهاية الجلسة الحالية، مع احترام الإغلاق المبكر."""
    if session is Session.CLOSED:
        return None
    if session is Session.PREMARKET:
        end_hour = cfg.regular_start_hour
    elif market_calendar.is_early_close(et_now.date()):
        end_hour = market_calendar.EARLY_CLOSE_HOUR
    elif session is Session.REGULAR:
        end_hour = cfg.regular_end_hour
    else:
        end_hour = cfg.afterhours_end_hour
    return et_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(hours=end_hour)


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
        # تعفّن معرّف نموذج Anthropic (401/404) = عطل صامت للذكاء → أبلِغ المشرف.
        self.claude.on_config_error = lambda msg: self.monitor.raise_fault(
            "anthropic", msg)
        self.last_runners: list = []       # (رمز، نسبة) لآخر مسح (للمساعد)
        self.last_scan_et = None
        self.assistant = TelegramAssistant(self)   # مساعد تيليجرام تفاعلي
        self._stop = threading.Event()
        self._shutdown_done = False    # حارس تفكيك idempotent (BUG-25)
        self._kronos_now_fn = lambda: datetime.now(timezone.utc)
        self.kronos: KronosShadowWorker | None = None
        if cfg.kronos_shadow_enabled:
            if not self.store.kronos_available:
                logger.warning(
                    "Kronos Shadow مفعّل لكن مخزن القياس غير متاح — تم تخطّيه: %s",
                    self.store.kronos_error,
                )
            elif not cfg.kronos_service_url:
                logger.warning(
                    "Kronos Shadow مفعّل لكن KRONOS_SERVICE_URL فارغ — تم تخطّيه")
            else:
                try:
                    self.kronos = KronosShadowWorker(
                        client=KronosShadowClient(
                            cfg.kronos_service_url,
                            token=cfg.kronos_service_token,
                            timeout=cfg.kronos_timeout_sec,
                        ),
                        # عميل مستقل: فشل/بطء جلب سياق Kronos لا يقفل عميل الدورة.
                        massive_client_factory=lambda: MassiveClient(cfg),
                        store=self.store,
                        context_days=cfg.kronos_context_days,
                        lookback=cfg.kronos_lookback,
                        pred_len=cfg.kronos_pred_len,
                        horizons=cfg.kronos_horizons,
                        queue_size=cfg.kronos_queue_size,
                        max_context_lag_sec=cfg.kronos_max_context_lag_sec,
                        observation_grace_min=cfg.kronos_observation_grace_min,
                        selection_revision=kronos_selection_revision(cfg),
                    )
                    self.kronos.start()
                    logger.info("Kronos Shadow يعمل بوضع القياس فقط")
                except Exception:  # اختياري تمامًا؛ لا يُفشل إقلاع الماسح
                    self.kronos = None
                    logger.exception("تعذّر تشغيل Kronos Shadow — الماسح مستمر")

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

        # PERF-18: ضبط اشتراك صفقات WebSocket على المجمّع الحالي (no-op في الوضع
        # الافتراضي T.*). LULD.* يبقى شبكة أمان للتوقّفات لكل رمز.
        self.halts.update_subscription(
            {e.ticker for e in top} | champ_syms)

        # «سهم الماركت» النموذجي (منهجية المستخدم): صاعد من البري + نافذة افتتاح
        # + ضغط. نجمع أبطال بريماركت اليوم مرّة، ونسم البطاقة لاحقًا (إعلام لا فرز).
        market_champs: set[str] = set()
        if is_opening_range(self.cfg, et_now):
            market_champs = {
                c["symbol"] for c in self.store.get_session_champions(
                    Session.PREMARKET.value, et_date)}

        # حسم أي تتبّعات معلّقة من أيام سابقة (لم تكتمل نافذتها قبل الإغلاق)
        self.store.finalize_stale(et_now)

        # تحديث نتائج التنبيهات المفتوحة من نفس السنابشوت (بلا نداء إضافي)
        # ويصدّر أحداث متابعة (🎯 هدف · ⛔ وقف · 🚀 قفزة) نرسلها فورًا.
        price_map = {e.ticker: e.last_price for e in snapshot if e.is_valid}
        volume_map = {e.ticker: e.day_volume for e in snapshot if e.is_valid}
        observed_at_map = {}
        for entry in snapshot:
            if not entry.is_valid or entry.price_observed_ns <= 0:
                continue
            try:
                observed_at_map[entry.ticker] = datetime.fromtimestamp(
                    entry.price_observed_ns / 1_000_000_000.0, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue
        # إغلاق الحلقة لتوقعات Kronos السابقة من نفس snapshot المجاني. لا يدخل
        # هذا القياس في البوابات أو الدرجة أو البطاقة.
        try:
            # et_now ثُبّت قبل HTTP لأجل determinism في منطق الدورة، لكن جلب
            # snapshot قد يتأخر عبر retry. قياس Kronos يستخدم ساعة ما بعد الرد.
            measurement_now = self._kronos_now_fn()
            self.store.update_kronos_actuals(
                price_map, measurement_now,
                grace_min=self.cfg.kronos_observation_grace_min,
                observed_at_map=observed_at_map)
        except Exception:
            # Kronos اختياري وShadow؛ فساد صف أو عطل SQLite هنا لا يجوز أن
            # يمنع تحليل الأسهم أو إرسال التنبيهات الأساسية.
            logger.exception("تعذّر إغلاق حلقة Kronos — دورة الماسح مستمرة")
        events = self.store.update_outcomes(
            price_map, et_now, window_min=self.cfg.outcome_window_min,
            surge_leg_pct=self.cfg.surge_leg_pct,
            missed_rise_pct=(self.cfg.missed_rise_pct
                             if self.cfg.missed_alert_enabled else 1e9),
            volume_map=volume_map, stop_dist_pct=self.cfg.stop_fixed_pct)
        for ev in events:
            if not self.telegram.send(build_followup(self.cfg, ev),
                                      to_all=True):
                logger.warning("فشل إرسال حدث متابعة محسوم في DB (قد يُفقد): %s %s",
                               ev.get("ticker"), ev.get("type"))
                # BUG-40: حدث الوقف أهمّ رسالة يرسلها البوت — أنت داخل صفقة
                # خاسرة. والصفّ يُغلق في القاعدة فلا يُعاد إرساله (دورة
                # open→closed، §7). فبدل الضياع الصامت نصدر عطلًا مرئيًّا:
                # «أُعلمك أنني عجزت عن إعلامك». إرشاد لا تصرّف — هوية البوت.
                if ev.get("type") == "stop":
                    key = f"stop_alert_lost:{ev.get('ticker')}"
                    self.monitor.raise_fault(
                        key,
                        f"⚠️ تعذّر إرسال تنبيه كسر الوقف لـ{ev.get('ticker')} "
                        "— راجع صفقتك يدويًّا الآن.")
                    # هذا **حدث لحظي** لا حالة قائمة: الصفّ يُغلق فلا يُعاد
                    # إرساله، ولا مسار تعافٍ يمسح المفتاح. تركُه يراكم مفتاحًا
                    # لكل رمز فيظهر البوت «معطلًا» في /status وفي البريفنغ إلى
                    # الأبد. نمسحه فورًا: أُبلغناك مرّة، وأي فشل لاحق لرمز آخر
                    # (أو للرمز نفسه في يوم آخر) يستحقّ إبلاغًا جديدًا.
                    self.monitor.clear_fault(key)
            # 🔍 تشريح لحظي عند كسر الوقف: لماذا فشل السهم؟
            if ev.get("type") == "stop" and self.cfg.postmortem_on_stop:
                row = self.store.fetch_row(ev["ticker"], et_date)
                if row is not None:
                    self.telegram.send(postmortem.build_failure_message(
                        self.cfg, row, client=self.claude), to_all=True)
        if events:
            logger.info("أُرسل %d تحديث متابعة", len(events))

        # حفظ أبطال هذي الفترة (أعلى 15 صعودًا) للتوريث للفترة التالية — **قبل**
        # رجوع البريماركت (BUG-06): الأبطال رصدٌ للسنابشوت لا فعل تنبيه، فحفظهم
        # لا يخالف تعطيل تنبيهات البريماركت. الرسمي يرث بريماركت-اليوم
        # (_CHAMP_INHERIT[REGULAR]=(PREMARKET,0))، وبلا هذا الحفظ يكون التوريث
        # ميتًا (لا يُكتب أبدًا في المسار الحيّ) أو يبعث يومًا قديمًا اعتباطيًا.
        if self.cfg.champions_enabled and top:
            # أبطال بحجم تداول فعلي فقط (لا طبعة بريماركت رقيقة تُورَّث كأولوية)
            self.store.save_champions(
                session.value, et_date,
                [(e.ticker, e.change_pct, e.last_price) for e in top
                 if e.day_volume > 0])

        # جلسة البريماركت معطّلة (قرار المستخدم بالبيانات: 8 أشهر → بريماركت 59%
        # مقابل رسمي 87%؛ تعطيلها يرفع النجاح الكلي ~6 نقاط). نحدّث المفتوح فوق
        # وننهي هنا بلا تنبيهات جديدة. /top لا يزال يعرض رنرات البريماركت (إعلام).
        if session is Session.PREMARKET and not self.cfg.premarket_alerts_enabled:
            logger.info("البريماركت معطّل — مراقبة المفتوح فقط، بلا تنبيهات جديدة")
            return 0

        accepted: list[Candidate] = []
        kronos_session_end = _kronos_session_end(self.cfg, session, et_now)
        kronos_pred_len = int(getattr(
            self.kronos, "pred_len", self.cfg.kronos_pred_len))
        kronos_window_open = (
            kronos_session_end is not None
            and et_now + timedelta(minutes=kronos_pred_len * 5)
            < kronos_session_end
        )
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
            # سهم ماركت نموذجي: بطل بريماركت + نافذة افتتاح + ضغط (أحجام متصاعدة)
            cand.is_market_stock = (
                snap.ticker in market_champs and cand.momentum is not None
                and cand.momentum.volume_rising)
            self.store.log_candidate(cand)   # closed-loop لكل مرشّح
            if not cand.is_rejected:
                accepted.append(cand)
                if self.kronos is not None and kronos_window_open:
                    try:
                        # لا نمرّر وقت بداية الدورة: العامل يلتقط وقت enqueue
                        # الحقيقي ثم يعيد فحص الوقت عند التنفيذ وبعد الاستدلال.
                        self.kronos.submit(
                            cand.ticker,
                            session=session.value,
                            session_end_at=kronos_session_end,
                        )
                    except Exception:  # Shadow لا يغيّر مسار التنبيه تحت أي ظرف
                        logger.debug("تعذّر إدراج %s في Kronos Shadow",
                                     cand.ticker, exc_info=True)

        # ترتيب الأولوية ثم الإرسال
        sent = 0
        for cand in prioritize(accepted):
            if self.telegram.send(build_card(self.cfg, cand), to_all=True):
                # BUG-32: ثبّت لقطة البطاقة (دخول/وقف/أهداف) أساسًا لقياس النتيجة
                # كي لا تُقاس من سعر أول رصد سابق (يُطلق hit_stop زائفًا).
                rp = cand.risk
                self.store.mark_alerted(
                    cand.ticker, cand.final_score,
                    entry_price=(rp.entry_ref if rp else cand.snapshot.last_price),
                    stop_price=(rp.stop_price if rp else None),
                    targets=(rp.targets if rp else None))
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
        # بريفنغ «نهاية الجلسة» يخصّ أيام التداول فقط: في نهايات الأسبوع والعطلات
        # لا جلسة أصلًا، فلا نرسل بريفنغًا يصف يوم عطلة كـ«جلسة صفرية» مضلّلة.
        if et_now.weekday() >= 5 or market_calendar.is_holiday(et_now.date()):
            return
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

    def _run_backtest_bg(self, et_now, quick: bool = False,
                         with_grid: bool = True,
                         start: str | None = None, end: str | None = None) -> None:
        from datetime import timedelta
        from dataclasses import replace
        from . import backtest, backtest_grid, backtest_notes
        from .massive_client import MassiveClient
        # تواريخ صريحة (شهر محدّد) أو نافذة افتراضية من الآن للخلف
        if start is None or end is None:
            lookback = (self.cfg.backtest_quick_days if quick
                        else self.cfg.backtest_lookback_days)
            end = (et_now.date() - timedelta(days=1)).isoformat()
            start = (et_now.date() - timedelta(days=lookback)).isoformat()
        # عميل سريع الفشل للباكتيست (مهلة قصيرة، إعادة واحدة): النداء البطيء
        # يُتخطّى بسرعة بدل أن تضاعف الإعادات الطويلة الزمن (آلاف النداءات).
        bt_cfg = replace(self.cfg, http_timeout=self.cfg.backtest_http_timeout,
                         http_max_retries=1)
        run_cfg = bt_cfg
        if quick:   # معاينة عاجلة: مجمّع أصغر وخطوة أخشن (الكامل للوظيفة الأسبوعية)
            run_cfg = replace(bt_cfg, backtest_top_n=self.cfg.backtest_quick_top_n,
                              backtest_scan_step_bars=self.cfg.backtest_quick_step)
        # عميل خام بلا مذكّر: المذكّر يكدّس بيانات الشهر كله في الذاكرة (سبب
        # تجاوز حدّ ذاكرة Render). run_backtest يحرّر كاش كل يوم بعده.
        client = MassiveClient(bt_cfg)
        label = "سريع (معاينة)" if quick else "كامل"
        try:
            self.telegram.send(
                f"🧪 <b>باكتيست {label}</b> {start} → {end}\n"
                "<i>يقيس حافة الاستراتيجية على الماضي…</i>")

            # مؤشّر تقدّم متكرّر (كل يوم تقريبًا) — لا يكون صندوقًا أسود
            def _progress(i, total, day, n):
                stepn = max(1, total // 10)
                if i == 1 or i == total or i % stepn == 0:
                    self.telegram.send(
                        f"⏳ يوم {i}/{total} ({day}) · صفقات حتى الآن: {n}")

            res = backtest.run_backtest(run_cfg, client, start, end,
                                        progress=_progress)
            report = backtest.format_report(res)
            self.telegram.send(report)
            logger.info("اكتمل الباكتيست (%d صفقة)", len(res.trades))
            # حفظ التشغيلات الكاملة فقط (المعاينة السريعة غير ممثِّلة → لا تُحفَظ
            # ولا تُدمَج). best-effort §3: فشل الحفظ/الإرسال لا يمنع التقرير نفسه.
            if not quick:
                try:
                    path = backtest.save_run(self.cfg, res, report)
                    if path:
                        self.telegram.send_document(
                            path, f"📦 بيانات باكتيست {start} → {end} "
                            f"({res.days} يوم · {len(res.trades)} صفقة) — "
                            "احفظه؛ يُدمَج لاحقًا بـ/backtest دمج")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("حفظ/إرسال بيانات الباكتيست فشل: %s", exc)
            # ── معايرة العتبات A/B — للوظيفة الأسبوعية فقط (ثقيلة 7×) ──
            grid = None
            if with_grid and not quick and self.cfg.backtest_grid_enabled:
                grid = backtest_grid.run_grid(run_cfg, client, start, end)
                self.telegram.send(backtest_grid.format_grid_report(grid))
                logger.info("اكتملت معايرة العتبات A/B")
            # ── ملاحظات تحليلية (تشرح الأرقام والقمع وتقترح — للمراجعة) ──
            if self.cfg.backtest_notes_enabled:
                self.telegram.send(backtest_notes.build_notes(
                    run_cfg, res, grid=grid, client=self.claude))
                logger.info("أُرسلت ملاحظات الباكتيست")
        except Exception as exc:  # noqa: BLE001
            logger.exception("الباكتيست التلقائي فشل")
            # §5: exc قد يحمل جسم استجابة خام فيه <>& → هرّبه قبل HTML (BUG-05)
            self.telegram.send(f"⚠️ تعذّر الباكتيست التلقائي: {esc(exc)}")

    def shutdown(self) -> None:
        # idempotent (BUG-25): يُنادى من finally الحلقة، وقد يُنادى مرّة ثانية —
        # تفكيك مزدوج يغلق القاعدة تحت خيوط حيّة (ProgrammingError). المعالج
        # الإشاري يكتفي بـ_stop.set()، والتفكيك الفعلي هنا مرّة واحدة فقط.
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("إيقاف الماسح...")
        self._stop.set()
        self.halts.stop()
        self.assistant.stop()
        if self.kronos is not None:
            self.kronos.stop(timeout=max(
                self.cfg.http_timeout, self.cfg.kronos_timeout_sec) + 1.0)
            if self.kronos.is_alive:
                logger.warning("خيط Kronos لم يتوقف قبل إغلاق قاعدة البيانات")
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
        # لا نفكّك من داخل المعالج (قد ينفَّذ وسط run_cycle على الخيط الرئيسي)
        # — نكتفي بطلب التوقّف، ويتكفّل finally الحلقة بالتفكيك مرّة واحدة (BUG-25).
        logger.info("استلمنا إشارة %s — طلب توقّف", signum)
        scanner._stop.set()

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
