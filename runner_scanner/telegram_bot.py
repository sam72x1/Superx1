"""مساعد تيليجرام تفاعلي — تكلّمه ويرد ببيانات البوت الحيّة + Claude.

ثريد يستمع لرسائلك (getUpdates) ويردّ على الأوامر:
  /status   — حالة البوت + ريندر
  /top      — أقوى الأسهم في آخر مسح
  /report   — تقرير التطوير + ملفات CSV الآن
  /briefing — بريفنغ المستشار الآن
  /backtest — باكتيست + معايرة العتبات A/B الآن (يدويًا)
  /ask ...  — اسأل Claude عن بوتك (يستخدم بياناتك الحيّة)
  /restart  — إعادة تشغيل الخدمة على ريندر (يتطلّب: /restart confirm)

أمان: يردّ فقط على محادثتك (TELEGRAM_CHAT_ID). لا ينفّذ أي إجراء إلا
بأمر صريح منك («ما يسوي شي إلا يعلمك»).
"""

from __future__ import annotations

import logging
import os
import threading

import requests

from . import advisor, postmortem
from .alerts import TelegramSender
from .dev_assistant import send_report_and_files
from .sessions import classify_session, now_et
from .state import trade_date_str
from .textutil import esc

logger = logging.getLogger(__name__)

_HELP = (
    "🤖 <b>مساعدك الشخصي</b>\n"
    "اكتب سؤالك مباشرة (بلا أي أمر) وأجاوبك 💬\n"
    "أو استخدم الأوامر:\n"
    "/status — حالة البوت وريندر\n"
    "/top — أقوى الأسهم الآن\n"
    "/report — تقرير التطوير + ملفات CSV\n"
    "/briefing — بريفنغ المستشار\n"
    "/backtest — باكتيست + معايرة العتبات A/B الآن\n"
    "/ask سؤالك — اسأل المستشار الذكي\n"
    "/why RMZ — لماذا فشل/نجح سهم؟ (تشريح)\n"
    "/diag RMZ — بيانات السهم الخام (تشخيص الفيد)\n"
    "/sha — إصدار الكود المنشور (للتأكّد من آخر تحديث)\n"
    "/restart — إعادة تشغيل الخدمة (يتطلّب تأكيد)"
)

# كلمات تُعامَل كطلب إصدار (تُعرض رقمًا حقيقيًا، لا تُمرَّر لـ Claude لئلا يهلوس)
_VERSION_WORDS = {"sha", "version", "إصدار", "الإصدار", "الاصدار", "النسخة"}

_ASK_SYSTEM = (
    "أنت مساعد ومستشار الماسح الشامل للمستخدم. أجب عن أسئلته حول حالة البوت "
    "وبياناته بالعربي بإيجاز ودقّة، اعتمادًا على البيانات المعطاة فقط. "
    "أنت لا تنفّذ أي إجراء — فقط تُبلغ وتقترح."
)


class TelegramAssistant:
    """مستهلك getUpdates + موجّه أوامر. يعتمد على مكوّنات Scanner."""

    def __init__(self, scanner):
        self.sc = scanner
        self.cfg = scanner.cfg
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._base = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"

    # ── دورة الحياة ───────────────────────────────────────────────
    def start(self) -> None:
        if not self.cfg.assistant_enabled or self.cfg.dry_run:
            return
        if not (self.cfg.telegram_bot_token and self.cfg.telegram_chat_id):
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tg-assistant")
        self._thread.start()
        logger.info("المساعد التفاعلي يستمع...")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                resp = requests.get(f"{self._base}/getUpdates", params={
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": '["message"]',
                }, timeout=35)
                if resp.status_code == 429:
                    # احترام Retry-After من تيليجرام بدل ثابت 5ث
                    self._stop.wait(min(TelegramSender._retry_after(resp), 30.0))
                    continue
                if resp.status_code != 200:
                    self._stop.wait(5)
                    continue
                for upd in resp.json().get("result", []):
                    self._offset = upd["update_id"] + 1
                    self._handle_update(upd)
            except (requests.RequestException, ValueError) as exc:
                logger.debug("getUpdates: %s", exc)
                self._stop.wait(5)

    # ── التوجيه ───────────────────────────────────────────────────
    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat != str(self.cfg.telegram_chat_id) or not text:
            return   # أمان: محادثتك فقط
        try:
            self._dispatch(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("أمر فشل: %s", exc)
            self._reply(f"تعذّر تنفيذ الأمر: {exc}")

    def _dispatch(self, text: str) -> None:
        # رسالة عادية (لا تبدأ بـ /) = سؤال مباشر للمساعد الذكي.
        # «مساعد شخصي» طبيعي: تكلّمه بلا أوامر، و/ask يبقى متاحًا أيضًا.
        if not text.startswith("/"):
            # كلمة «sha»/«إصدار» → رقم الإصدار الحقيقي (لا نمرّرها لـ Claude)
            if text.strip().lower() in _VERSION_WORDS:
                self._reply(self._version_text())
                return
            self._handle_ask(text)
            return
        cmd = text.split()[0].lower().lstrip("/")
        arg = text[len(text.split()[0]):].strip()
        if cmd in ("start", "help"):
            self._reply(_HELP)
        elif cmd == "status":
            self._reply(self._status_text())
        elif cmd == "top":
            self._reply(self._top_text())
        elif cmd == "report":
            self._reply("جاري تجهيز تقرير التطوير...")
            send_report_and_files(self.sc.store, self.cfg, self.sc.telegram)
        elif cmd == "briefing":
            self._reply(advisor.build_briefing(
                self.cfg, self.sc.store,
                render_summary=self.sc.render.summary(),
                health_faults=self.sc.monitor.active_faults(),
                client=self.sc.claude))
        elif cmd == "backtest":
            self._handle_backtest()
        elif cmd == "ask":
            self._handle_ask(arg)
        elif cmd == "why":
            self._handle_why(arg)
        elif cmd == "diag":
            self._handle_diag(arg)
        elif cmd in ("sha", "version"):
            self._reply(self._version_text())
        elif cmd == "restart":
            self._handle_restart(arg)
        else:
            self._reply("أمر غير معروف. /help للأوامر.")

    # ── الأوامر ───────────────────────────────────────────────────
    def _status_text(self) -> str:
        session = classify_session(self.cfg, now_et())
        faults = self.sc.monitor.active_faults()
        health = "أعطال: " + ", ".join(faults) if faults else "سليم ✅"
        last = getattr(self.sc, "last_scan_et", None)
        return (
            f"📟 <b>حالة البوت</b>\n"
            f"الجلسة: {session.value} · المسح كل {self.cfg.poll_interval_sec}ث\n"
            f"آخر مسح: {last.strftime('%H:%M ET') if last else '—'}\n"
            f"الصحة: {health}\n"
            f"{self.sc.render.summary()}"
        )

    def _top_text(self) -> str:
        runners = getattr(self.sc, "last_runners", [])
        if not runners:
            return "ما فيه أسهم في آخر مسح (أو ما بدأ بعد)."
        lines = ["🔝 <b>أقوى الأسهم (آخر مسح)</b>"]
        for tkr, chg in runners[:15]:
            lines.append(f"  • {tkr}: +{chg:.1f}%")
        return "\n".join(lines)

    def _handle_ask(self, question: str) -> None:
        if not question:
            self._reply("اكتب سؤالك بعد /ask")
            return
        if not self.sc.claude.available:
            self._reply("المساعد الذكي غير مفعّل (أضِف ANTHROPIC_API_KEY).")
            return
        ctx = self._context_text()
        prompt = f"بيانات البوت الآن:\n{ctx}\n\nسؤال المستخدم: {question}"
        ans = self.sc.claude.chat(self.cfg.anthropic_model, _ASK_SYSTEM, prompt)
        # رد Claude حرّ → يُهرَّب قبل الإرسال بصيغة HTML
        self._reply(esc(ans) if ans else "تعذّر الحصول على رد.")

    def _version_text(self) -> str:
        """إصدار الكود المنشور فعليًا (SHA من البيئة — رقم حقيقي لا تخمين)."""
        sha = self.cfg.code_version or "غير معروف"
        full = os.getenv("RENDER_GIT_COMMIT", "") or "—"
        branch = os.getenv("RENDER_GIT_BRANCH", "") or "—"
        lines = [
            "📦 <b>إصدار الكود المنشور (SHA)</b>",
            f"SHA: <code>{esc(sha)}</code>",
            f"الكامل: <code>{esc(full)}</code>",
            f"الفرع: {esc(branch)}",
        ]
        # مقارنة بآخر نشر على Render (لو مربوط) — تأكيد أنك على الأحدث
        if self.sc.render.available:
            dep = self.sc.render.latest_deploy()
            if dep and dep.get("commit_id"):
                same = dep["commit_id"][:7] == (sha or "")[:7]
                mark = "✅ مطابق لآخر نشر" if same else "⚠️ مختلف عن آخر نشر"
                lines.append(
                    f"آخر نشر على Render: <code>{esc(dep['commit_id'])}</code>"
                    f" ({esc(dep['status'])}) {mark}")
        lines.append("↳ قارن SHA بآخر commit على GitHub للتأكّد أنك على الأحدث.")
        return "\n".join(lines)

    def _handle_diag(self, arg: str) -> None:
        """يطبع بيانات السنابشوت الخام لتأكيد فرضيات الفيد (خاصة البريماركت):
        هل day.v جزئي/صفر؟ هل day.vw=0؟ — تحقّق ميداني لإصلاحات تدقيق الواقع."""
        tkr = arg.strip().upper().lstrip("$").split()[0] if arg.strip() else ""
        if not tkr:
            self._reply("اكتب الرمز بعد /diag — مثال: <code>/diag ABCD</code>")
            return
        session = classify_session(self.cfg, now_et())
        try:
            s = self.sc.client.single_snapshot(tkr)
        except Exception as exc:  # noqa: BLE001
            self._reply(f"تعذّر جلب بيانات ${esc(tkr)}: {esc(str(exc))}")
            return
        if s is None:
            self._reply(f"ما فيه بيانات لـ ${esc(tkr)}.")
            return
        self._reply(
            f"🔬 <b>تشخيص ${esc(tkr)}</b> · الجلسة: {session.value}\n"
            f"السعر: {s.last_price} · إغلاق أمس: {s.prev_close} · "
            f"التغيّر: {s.change_pct:+.1f}%\n"
            f"day.open: {s.day_open} · day.high: {s.day_high} · "
            f"day.low: {s.day_low}\n"
            f"<b>day.volume: {s.day_volume:,.0f}</b> · "
            f"<b>day.vwap: {s.day_vwap}</b>\n"
            f"صالح للتحليل: {'نعم' if s.is_valid else 'لا'}\n"
            "<i>↳ في البريماركت: إن كان day.volume جزئيًا/صفرًا و day.vwap=0 "
            "فهذا يؤكّد منطق إصلاحات الجلسات الممتدة.</i>")

    def _handle_why(self, arg: str) -> None:
        tkr = arg.strip().upper().lstrip("$").split()[0] if arg.strip() else ""
        if not tkr:
            self._reply("اكتب الرمز بعد /why — مثال: <code>/why ABCD</code>")
            return
        row = self.sc.store.fetch_row(tkr)
        if row is None:
            self._reply(f"ما لقيت تتبّعًا لـ ${tkr} (لم يُنبَّه عنه أو لم يُعالَج).")
            return
        self._reply(postmortem.build_why_message(
            self.cfg, row, client=self.sc.claude))

    def _handle_restart(self, arg: str) -> None:
        if arg.lower() != "confirm":
            self._reply("⚠️ إعادة تشغيل الخدمة على ريندر. للتأكيد أرسل:\n"
                        "<code>/restart confirm</code>")
            return
        if not self.sc.render.available:
            self._reply("ريندر غير مربوط (أضِف RENDER_API_KEY و RENDER_SERVICE_ID).")
            return
        self._reply("جاري إعادة التشغيل..." if self.sc.render.restart()
                    else "تعذّرت إعادة التشغيل.")

    def _handle_backtest(self) -> None:
        """تشغيل الباكتيست + معايرة العتبات A/B الآن (يدويًا) — بلا انتظار السبت
        وبلا قفل التكرار الأسبوعي. يشتغل في الخلفية والنتائج تصلك تباعًا."""
        if not self.cfg.massive_api_key:
            self._reply("الباكتيست يحتاج MASSIVE_API_KEY.")
            return
        self._reply("🚀 بدء باكتيست <b>سريع (معاينة)</b> الآن… "
                    "(دقائق قليلة، تصلك النتائج تباعًا)\n"
                    "<i>الباكتيست الكامل يشتغل تلقائيًا كل سبت في الخلفية.</i>")
        threading.Thread(target=self.sc._run_backtest_bg, args=(now_et(),),
                         kwargs={"quick": True},
                         daemon=True, name="backtest-manual").start()

    def _context_text(self) -> str:
        s = advisor._summarize_day(self.cfg, self.sc.store, now_et())
        runners = getattr(self.sc, "last_runners", [])
        return (
            f"الجلسة: {classify_session(self.cfg, now_et()).value}\n"
            f"تنبيهات اليوم: {len(s['alerts'])} "
            f"({len(s['wins'])}✅/{len(s['losses'])}🛑) · فرص فائتة: {len(s['missed'])}\n"
            f"أقوى الأسهم الآن: "
            f"{', '.join(f'{t}+{c:.0f}%' for t, c in runners[:8]) or '—'}\n"
            f"العتبات: RVol≥{self.cfg.rvol_min}x · فلوت≤{self.cfg.float_max:,.0f}"
            f" · جاهزية≥{self.cfg.tech_readiness_min:.0f} · درجة≥{self.cfg.alert_score_min:.0f}\n"
            f"{self.sc.render.summary()}"
        )

    def _reply(self, text: str) -> None:
        self.sc.telegram.send(text)
