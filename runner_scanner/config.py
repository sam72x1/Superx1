"""كل العتبات والإعدادات — قابلة للتعديل عبر متغيّرات البيئة (env).

الفلسفة: لا أرقام سحرية مبعثرة في الكود. كل عتبة بوّابة أو وزن أو حدّ
تُقرأ من هنا، عشان نقدر نعايرها لاحقًا من البيانات المتراكمة (closed-loop)
بدون لمس الكود.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    """يقرأ متغيّر بيئة كرقم عشري مع قيمة افتراضية."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class Config:
    """إعدادات التشغيل. تُبنى من البيئة عبر Config.from_env()."""

    # ── الاعتماد والاتصال ──────────────────────────────────────────
    massive_api_key: str = ""
    massive_rest_base: str = "https://api.massive.com"
    massive_ws_url: str = "wss://socket.massive.com/stocks"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── التخزين ────────────────────────────────────────────────────
    # لازم يكون على قرص دائم في Render (منع تكرار التنبيه عبر إعادة النشر).
    db_path: str = "/var/data/runner_scanner.sqlite3"

    # ── حلقة المسح ─────────────────────────────────────────────────
    poll_interval_sec: int = 45          # بين 30 و60ث (القرار 7)
    keepalive_port: int = 10000          # منفذ keep-alive لـ Render
    # أعلى N سهم صعودًا فقط نحلّلها كل دورة (قرار المستخدم: 15)
    top_n_runners: int = 15

    # ── الزناد ─────────────────────────────────────────────────────
    trigger_change_pct: float = 20.0     # +20% عن إغلاق أمس (شرط ضروري)
    max_change_pct: float = 400.0        # سقف يسقط تشوّه الانقسام العكسي
    filter_derivatives: bool = True      # استبعاد الوارنتات/اليونتات/الحقوق
    # أنواع الأوراق المقبولة (Polygon type): CS=سهم عادي، ADRC=إيصال إيداع
    allowed_ticker_types: tuple[str, ...] = ("CS", "ADRC")
    exclude_otc: bool = True             # استبعاد OTC/pink

    # ── البوابات الصارمة (القسم 6) ────────────────────────────────
    float_max: float = 20_000_000        # فلوت ≤ 20M
    rvol_min: float = 5.0                # RVol ≥ 5x (حسب الجلسة)
    volume_min: float = 300_000          # حجم يومي ≥ 300K (سيولة خروج)
    price_min: float = 1.0               # لا سنتات
    price_max: float = 30.0              # لا فوق نطاق الأسهم
    # امتداد بارابولِك: رفض لو السعر ابتعد عن VWAP بأكثر من هذا%
    parabolic_vwap_ext_pct: float = 40.0
    # أو لو صعد عن إغلاق أمس بأكثر من هذا% (منهك / خطر blow-off)
    parabolic_day_change_pct: float = 120.0

    # ── الجاهزية الفنية (قرار المستخدم: ≥ 70/100) ─────────────────
    tech_readiness_min: float = 70.0     # درجة التحليل الكلاسيكي 0–100

    # ── حدود ركيزتي الدرجة ────────────────────────────────────────
    momentum_pillar_max: float = 50.0
    readiness_pillar_max: float = 50.0
    momentum_min_floor: float = 25.0     # الزخم لازم فوق هذا (من 50)
    # عتبة الأولوية للتنبيه (الدرجة النهائية من 100)
    alert_score_min: float = 60.0

    # ── الخبر/المحفّز (قرار المستخدم: إشارة تقوية لا بوابة) ────────
    catalyst_lookback_hours: float = 48.0   # نافذة "خبر حديث"
    catalyst_score_bonus: float = 8.0       # تُضاف للدرجة عند وجود خبر

    # ── الوقف والأهداف (القسم 8) ──────────────────────────────────
    stop_min_pct: float = 4.0            # حد أدنى لمسافة الوقف (ضوضاء LULD)
    stop_max_pct: float = 20.0           # سقف أعلى لمسافة الوقف
    target_max_pct: float = 80.0         # سقف مسافة الهدف (يمنع أهدافًا بعيدة سخيفة)
    target_r_multiples: tuple[float, ...] = (1.0, 2.0, 3.0)  # أهداف كمضاعفات R

    # ── الجلسات (ET) — ساعات بتوقيت نيويورك ───────────────────────
    premarket_start_hour: float = 4.0    # بريماركت يبدأ 4:00ص (بعد مسح السنابشوت)
    regular_start_hour: float = 9.5      # 9:30ص
    regular_end_hour: float = 16.0       # 4:00م
    afterhours_end_hour: float = 20.0    # 8:00م

    # ── منع التكرار ───────────────────────────────────────────────
    dedup_per_day: bool = True           # تنبيه واحد/سهم/يوم

    # ── توريث أبطال الفترة ────────────────────────────────────────
    champions_enabled: bool = True       # متابعة أبطال الفترة السابقة بأولوية

    # ── تتبّع النتائج + أداة التطوير (القسم 12 closed-loop) ────────
    outcome_window_min: float = 90.0     # نافذة متابعة السهم بعد التنبيه (دقائق)
    missed_rise_pct: float = 30.0        # مرفوض صعد ≥ هذا = فرصة فائتة
    surge_leg_pct: float = 8.0           # قفزة جديدة ≥ هذا فوق آخر قمة = تحديث
    dev_min_sample: int = 10             # أقل عدد نتائج محسومة قبل تقرير ذو معنى
    dev_report_on_close: bool = True     # تفعيل تقرير التطوير المجدوَل
    # أيام إرسال التقرير (بتوقيت العرض/الرياض): Mon=0..Sun=6 → الأربعاء+السبت
    dev_report_weekdays: tuple[int, ...] = (2, 5)
    dev_report_hour: int = 5             # ساعة الإرسال (فجرًا بالرياض، بعد الإغلاق)

    # ── المستشار الذكي (Claude) — «العين اللي ما تنام» ────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"   # نموذج البريفنغ/المساعد
    analyst_model: str = "claude-haiku-4-5-20251001"  # تحليل كل تنبيه (أسرع)
    analyst_enabled: bool = True          # محلّل ذكي لكل تنبيه
    advisor_enabled: bool = True          # بريفنغ نهاية الجلسة
    assistant_enabled: bool = True        # مساعد تيليجرام تفاعلي
    analyst_bearish_penalty: float = 12.0 # خصم درجة عند محفّز هبوطي (طرح/تخفيف)

    # ── رادار التخفيف (SEC EDGAR) — يحذّر من الطرح القادم ──────────
    dilution_radar_enabled: bool = True   # رصد ملفات SEC التخفيفية
    # نافذة «طرح فعّال/وشيك» (424B/EFFECT): خطر مرتفع
    dilution_active_days: int = 45
    # نافذة «رفّ مُسجّل» (S-1/S-3 shelf): قدرة على التخفيف = خطر متوسط
    dilution_shelf_days: int = 180
    dilution_penalty: float = 12.0        # خصم درجة عند طرح فعّال (يضرّ كالشورت)
    # User-Agent إلزامي من SEC (سياستهم) — يُفضّل بريد حقيقي
    sec_user_agent: str = "RunnerScanner research contact@example.com"

    # ── ريندر (وعي/تحكّم) ─────────────────────────────────────────
    render_api_key: str = ""
    render_service_id: str = ""           # srv-xxxx للخدمة

    # ── العرض ─────────────────────────────────────────────────────
    display_tz: str = "Asia/Riyadh"      # توقيت عرض وقت البطاقة
    code_version: str = ""               # إصدار الكود (commit) — يُعرض بالبطاقة
    buy_zone_pct: float = 1.3            # عرض منطقة الشراء فوق السعر%
    short_warn_pct: float = 20.0         # شورت ≥ هذا = تحذير ضغط بيعي (يضرّ)

    # ── متفرقات ───────────────────────────────────────────────────
    halts_enabled: bool = True           # تشغيل مستهلك WebSocket للتوقّفات
    dry_run: bool = False                # لا يرسل تيليجرام، يطبع فقط
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            massive_api_key=_s("MASSIVE_API_KEY", ""),
            massive_rest_base=_s("MASSIVE_REST_BASE", "https://api.massive.com"),
            massive_ws_url=_s("MASSIVE_WS_URL", "wss://socket.massive.com/stocks"),
            telegram_bot_token=_s("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_s("TELEGRAM_CHAT_ID", ""),
            db_path=_s("DB_PATH", "/var/data/runner_scanner.sqlite3"),
            poll_interval_sec=_i("POLL_INTERVAL_SEC", 45),
            keepalive_port=_i("KEEPALIVE_PORT", 10000),
            trigger_change_pct=_f("TRIGGER_CHANGE_PCT", 20.0),
            max_change_pct=_f("MAX_CHANGE_PCT", 400.0),
            filter_derivatives=_b("FILTER_DERIVATIVES", True),
            allowed_ticker_types=tuple(
                t.strip() for t in _s("ALLOWED_TICKER_TYPES", "CS,ADRC").split(",")
                if t.strip()),
            exclude_otc=_b("EXCLUDE_OTC", True),
            float_max=_f("FLOAT_MAX", 20_000_000),
            rvol_min=_f("RVOL_MIN", 5.0),
            volume_min=_f("VOLUME_MIN", 300_000),
            price_min=_f("PRICE_MIN", 1.0),
            price_max=_f("PRICE_MAX", 30.0),
            parabolic_vwap_ext_pct=_f("PARABOLIC_VWAP_EXT_PCT", 40.0),
            parabolic_day_change_pct=_f("PARABOLIC_DAY_CHANGE_PCT", 120.0),
            tech_readiness_min=_f("TECH_READINESS_MIN", 70.0),
            momentum_pillar_max=_f("MOMENTUM_PILLAR_MAX", 50.0),
            readiness_pillar_max=_f("READINESS_PILLAR_MAX", 50.0),
            momentum_min_floor=_f("MOMENTUM_MIN_FLOOR", 25.0),
            alert_score_min=_f("ALERT_SCORE_MIN", 60.0),
            catalyst_lookback_hours=_f("CATALYST_LOOKBACK_HOURS", 48.0),
            catalyst_score_bonus=_f("CATALYST_SCORE_BONUS", 8.0),
            stop_min_pct=_f("STOP_MIN_PCT", 4.0),
            stop_max_pct=_f("STOP_MAX_PCT", 20.0),
            target_max_pct=_f("TARGET_MAX_PCT", 80.0),
            premarket_start_hour=_f("PREMARKET_START_HOUR", 4.0),
            regular_start_hour=_f("REGULAR_START_HOUR", 9.5),
            regular_end_hour=_f("REGULAR_END_HOUR", 16.0),
            afterhours_end_hour=_f("AFTERHOURS_END_HOUR", 20.0),
            dedup_per_day=_b("DEDUP_PER_DAY", True),
            champions_enabled=_b("CHAMPIONS_ENABLED", True),
            anthropic_api_key=_s("ANTHROPIC_API_KEY", ""),
            anthropic_model=_s("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            analyst_model=_s("ANALYST_MODEL", "claude-haiku-4-5-20251001"),
            analyst_enabled=_b("ANALYST_ENABLED", True),
            advisor_enabled=_b("ADVISOR_ENABLED", True),
            assistant_enabled=_b("ASSISTANT_ENABLED", True),
            analyst_bearish_penalty=_f("ANALYST_BEARISH_PENALTY", 12.0),
            dilution_radar_enabled=_b("DILUTION_RADAR_ENABLED", True),
            dilution_active_days=_i("DILUTION_ACTIVE_DAYS", 45),
            dilution_shelf_days=_i("DILUTION_SHELF_DAYS", 180),
            dilution_penalty=_f("DILUTION_PENALTY", 12.0),
            sec_user_agent=_s("SEC_USER_AGENT",
                              "RunnerScanner research contact@example.com"),
            render_api_key=_s("RENDER_API_KEY", ""),
            render_service_id=_s("RENDER_SERVICE_ID", ""),
            display_tz=_s("DISPLAY_TZ", "Asia/Riyadh"),
            code_version=_s("CODE_VERSION", _s("RENDER_GIT_COMMIT", ""))[:7],
            buy_zone_pct=_f("BUY_ZONE_PCT", 1.3),
            short_warn_pct=_f("SHORT_WARN_PCT", 20.0),
            top_n_runners=_i("TOP_N_RUNNERS", 15),
            outcome_window_min=_f("OUTCOME_WINDOW_MIN", 90.0),
            missed_rise_pct=_f("MISSED_RISE_PCT", 30.0),
            surge_leg_pct=_f("SURGE_LEG_PCT", 8.0),
            dev_min_sample=_i("DEV_MIN_SAMPLE", 10),
            dev_report_on_close=_b("DEV_REPORT_ON_CLOSE", True),
            dev_report_weekdays=tuple(
                int(x) for x in _s("DEV_REPORT_WEEKDAYS", "2,5").split(",")
                if x.strip().lstrip("-").isdigit()),
            dev_report_hour=_i("DEV_REPORT_HOUR", 5),
            halts_enabled=_b("HALTS_ENABLED", True),
            dry_run=_b("DRY_RUN", False),
            log_level=_s("LOG_LEVEL", "INFO"),
        )

    def missing_required(self) -> list[str]:
        """يرجّع أسماء المتغيّرات الإلزامية الناقصة (لرسالة خطأ واضحة)."""
        missing = []
        if not self.massive_api_key:
            missing.append("MASSIVE_API_KEY")
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing
