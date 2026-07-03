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


def _ftuple(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """يقرأ قائمة أرقام مفصولة بفواصل (مثل عتبات شبكة المعايرة) بأمان."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    out: list[float] = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(float(x))
        except ValueError:
            pass
    return tuple(out) if out else default


@dataclass
class Config:
    """إعدادات التشغيل. تُبنى من البيئة عبر Config.from_env()."""

    # ── الاعتماد والاتصال ──────────────────────────────────────────
    massive_api_key: str = ""
    massive_rest_base: str = "https://api.massive.com"
    massive_ws_url: str = "wss://socket.massive.com/stocks"
    # مهلة كل نداء REST (ث) + إعادة المحاولة على الأعطال العابرة (مهلة/429/5xx)
    # — يمنع فشلًا شبكيًا عابرًا من كسر دورة/باكتيست (best-effort، القسم 3).
    http_timeout: float = 20.0
    http_max_retries: int = 3
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
    # حدّ أدنى للحركة؛ «أعلى N» هو الفلتر الحقيقي. منخفض كي لا يفوت قادة
    # الأيام الهادئة (والبوّابات الأخرى تصفّي الضعيف).
    trigger_change_pct: float = 10.0
    max_change_pct: float = 400.0        # سقف يسقط تشوّه الانقسام العكسي
    filter_derivatives: bool = True      # استبعاد الوارنتات/اليونتات/الحقوق
    # أنواع الأوراق المقبولة (Polygon type): CS=سهم عادي، ADRC=إيصال إيداع
    allowed_ticker_types: tuple[str, ...] = ("CS", "ADRC")
    exclude_otc: bool = True             # استبعاد OTC/pink

    # ── البوابات الصارمة (القسم 6) ────────────────────────────────
    float_max: float = 40_000_000        # فلوت ≤ 40M
    rvol_min: float = 5.0                # RVol ≥ 5x (حسب الجلسة)
    volume_min: float = 300_000          # عتبة الحجم المطلق (مستخدمة فقط لو فُعّلت)
    # قرار المستخدم: RVol هو المقياس الوحيد للنشاط في الجلسات الثلاث؛ الحجم
    # المطلق مُلغى (نسبي > مطلق). يمكن إعادته بـ VOLUME_GATE_ENABLED=true.
    volume_gate_enabled: bool = False
    price_min: float = 1.0               # لا سنتات
    price_max: float = 30.0              # لا فوق نطاق الأسهم
    # امتداد بارابولِك: رفض لو السعر ابتعد عن VWAP بأكثر من هذا%
    parabolic_vwap_ext_pct: float = 40.0
    # أو لو صعد عن إغلاق أمس بأكثر من هذا% (منهك / خطر blow-off)
    parabolic_day_change_pct: float = 120.0

    # ── الجاهزية الفنية (قرار المستخدم: ≥ 60/100) ─────────────────
    tech_readiness_min: float = 60.0     # درجة التحليل الكلاسيكي 0–100
    min_history_bars: int = 50           # أقل تاريخ يومي لتأكيد الجاهزية (وإلا غير مؤكَّدة)

    # ── حدود ركيزتي الدرجة ────────────────────────────────────────
    momentum_pillar_max: float = 50.0
    readiness_pillar_max: float = 50.0
    # وزن ADX/DMI داخل درجة الفريم (قوة الاتجاه). ADX≥25 هو **المؤشر الوحيد
    # المتّسق** عبر 5 أشهر (يفوز أكثر 4/4)، ووزنه الفعلي على الدرجة كان ضئيلًا
    # (~0.4–2.1/100)، فرُفع من 5 إلى 7 (بزيادة محافظة). قابل للمعايرة/الباكتيست.
    adx_weight: float = 7.0
    momentum_min_floor: float = 25.0     # الزخم لازم فوق هذا (من 50)
    # عتبة الأولوية للتنبيه (الدرجة النهائية من 100)
    alert_score_min: float = 60.0

    # ── الخبر/المحفّز (قرار المستخدم: إشارة تقوية لا بوابة) ────────
    catalyst_lookback_hours: float = 48.0   # نافذة "خبر حديث"
    catalyst_score_bonus: float = 8.0       # تُضاف للدرجة عند وجود خبر

    # ── الوقف والأهداف (القسم 8) ──────────────────────────────────
    stop_fixed_pct: float = 7.0          # الوقف = الدخول − هذه النسبة% بالضبط (قرار المستخدم)
    stop_min_pct: float = 4.0            # (غير مستخدَم للوقف الثابت؛ مُبقى للتوافق)
    stop_max_pct: float = 20.0           # (غير مستخدَم للوقف الثابت؛ مُبقى للتوافق)
    target_max_pct: float = 80.0         # سقف مسافة الهدف (يمنع أهدافًا بعيدة سخيفة)
    # حد أدنى لسقف ربح الأهداف%: صفقة سقفها (أبعد هدف) أقل = «لا تستحق المخاطرة».
    # قرار المستخدم على 5 أشهر: تحت 10% لا يستحق المخاطرة. 0 = معطّل.
    min_target_profit_pct: float = 10.0
    # نسبة البيع عند الهدف1 في **قياس** الخروج الجزئي بالباكتيست (ظل، لا يغيّر الحيّ).
    partial_exit_fraction: float = 0.5
    min_bar_trades: int = 3              # أقل عدد صفقات لاعتبار قمة الشمعة مقاومة حقيقية
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
    missed_alert_enabled: bool = True     # تنبيه لحظي بالفرص الفائتة + سببها
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
    postmortem_enabled: bool = True       # تشريح سبب فشل/نجاح السهم (Claude)
    postmortem_on_stop: bool = True       # تشريح لحظي فور كسر الوقف

    # ── الباكتيست التلقائي (يشتغل بنفسه ويرسل النتيجة — بلا تدخّل) ──
    backtest_enabled: bool = True         # باكتيست أسبوعي تلقائي في الخلفية
    backtest_lookback_days: int = 45      # نافذة الباكتيست (أيام تقويم ≈ 30 تداول)
    backtest_weekday: int = 5             # يوم التشغيل (الرياض): السبت=5
    backtest_hour: int = 6                # ساعة التشغيل فجرًا (الرياض)
    # محاكاة المسح المتكرّر: يفحص كل مرشّح عند كل N شمعة 5د حتى أول نجاح (مثل
    # البوت الحي الذي يعيد فحص المرفوض كل دورة). 1 = كل شمعة (أدقّ مطابقة للحي).
    backtest_scan_step_bars: int = 1
    # عدد المرشّحين/يوم في الباكتيست (منفصل عن top_n_runners الحي = 15). الحي
    # يغطّي 3 جلسات (حتى 45 مختلفًا)؛ نوسّع المجمّع هنا ليقارب اتحاد قادتها.
    # الترتيب بالقمة اليومية (تشمل الجلسات الممتدة) — تقريب أمين، رخيص الجلب.
    backtest_top_n: int = 45
    # مهلة قصيرة لنداءات الباكتيست (ث): النداء البطيء يُتخطّى بسرعة بدل أن
    # تضاعف الإعادات الطويلة الزمن (الباكتيست = آلاف النداءات، فالفشل السريع أهمّ).
    backtest_http_timeout: float = 8.0
    # وضع «سريع» للتشغيل اليدوي (/backtest): معاينة عاجلة بدل انتظار ساعات.
    # الوظيفة الأسبوعية تبقى كاملة (45 يوم/45 مرشّح) في الخلفية.
    backtest_quick_days: int = 5          # نافذة المعاينة السريعة (أيام تقويم)
    backtest_quick_top_n: int = 12        # مرشّحون/يوم في المعاينة السريعة
    backtest_quick_step: int = 2          # فحص كل شمعتين 5د في المعاينة السريعة
    # جلب متوازٍ: عدد الأسهم التي تُعالَج معًا (يسرّع الشهر من ساعة لدقائق).
    # محافظ كي لا يصطدم بحدّ معدّل Massive؛ ارفعه لو خطّتك تسمح.
    backtest_workers: int = 8
    # قياس الظل: يسجّل لكل سهم مرفوض بـRVol نتيجة افتراضية + أقصى RVol بلغه،
    # لنعرف هل عتبة RVol=5x تفوّت فرصًا (قياس فقط، لا يغيّر أي قرار حيّ).
    backtest_shadow_rvol: bool = True

    # ── معايرة العتبات A/B (يقترح أفضل عتبات تاريخيًا — لا يطبّق) ───
    # يجرّب تغيير عتبة واحدة كل مرة على نفس البيانات (no-lookahead) ويرتّب
    # حسب النجاح. الناتج اقتراح للمراجعة فقط (البوت دليل لا منفّذ).
    # ثقيلة على الذاكرة (تعيد تشغيل الباكتيست 7× بكاش مشترك) → مطفأة افتراضيًا
    # على خوادم 512MB؛ فعّلها على خادم أكبر. الباكتيست العادي (تقرير+قمع+ملاحظات)
    # يبقى شغّالًا. التشغيل اليدوي لا يشغّل الشبكة أصلًا (أخفّ).
    backtest_grid_enabled: bool = False
    # قيم تجربة الجاهزية الفنية (TECH_READINESS_MIN)
    backtest_grid_readiness: tuple[float, ...] = (55.0, 60.0, 65.0, 70.0)
    # قيم تجربة سقف الفلوت (FLOAT_MAX)
    backtest_grid_float_max: tuple[float, ...] = (40_000_000, 60_000_000, 75_000_000)
    # قيم تجربة حدّ البارابولِك (PARABOLIC_DAY_CHANGE_PCT)
    backtest_grid_parabolic: tuple[float, ...] = (120.0, 150.0)
    # أقل عدد صفقات محسومة (نجاح+خسارة) قبل الوثوق بنسبة نجاح تركيبة
    backtest_grid_min_decisive: int = 8
    # أقل تحسّن (نقاط مئوية) فوق الأساس كي نقترح التغيير (يمنع ضوضاء صغيرة)
    backtest_grid_min_edge: float = 3.0
    # ملاحظات تحليلية تُرسَل مع الباكتيست (تشرح الأرقام والقمع وتقترح — لا تنفّذ)
    backtest_notes_enabled: bool = True
    # مجلد حفظ نتائج كل تشغيل باكتيست كامل (JSON = مصدر الدمج + نسخة نص التقرير)
    # على القرص الدائم. الفراغ = <مجلد قاعدة البيانات>/backtests (نفس قرص Render).
    backtest_save_dir: str = ""

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
    # تحذير البريماركت: الباكتيست أظهر نجاحه التاريخي أضعف بوضوح (≈53% مقابل
    # ≈88% للرسمي). إعلام فقط على البطاقة + أولوية أخفض — لا حذف (دليل لا منفّذ).
    premarket_caution_enabled: bool = True
    # تنبيهات البريماركت: **معطّلة** (أولوية المستخدم = الدقّة). البريماركت أقل
    # جلسة دقّة (8 أشهر: 59% مقابل 87% رسمي)؛ تعطيله يرفع الدقّة الكلية 81.6%→88%.
    # مراقبة المفتوح تبقى. فعّلها بـ PREMARKET_ALERTS_ENABLED=true لتغطية أوسع.
    premarket_alerts_enabled: bool = False

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
            http_timeout=_f("HTTP_TIMEOUT", 20.0),
            http_max_retries=_i("HTTP_MAX_RETRIES", 3),
            telegram_bot_token=_s("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_s("TELEGRAM_CHAT_ID", ""),
            db_path=_s("DB_PATH", "/var/data/runner_scanner.sqlite3"),
            poll_interval_sec=_i("POLL_INTERVAL_SEC", 45),
            keepalive_port=_i("KEEPALIVE_PORT", 10000),
            trigger_change_pct=_f("TRIGGER_CHANGE_PCT", 10.0),
            max_change_pct=_f("MAX_CHANGE_PCT", 400.0),
            filter_derivatives=_b("FILTER_DERIVATIVES", True),
            allowed_ticker_types=tuple(
                t.strip() for t in _s("ALLOWED_TICKER_TYPES", "CS,ADRC").split(",")
                if t.strip()),
            exclude_otc=_b("EXCLUDE_OTC", True),
            float_max=_f("FLOAT_MAX", 40_000_000),
            rvol_min=_f("RVOL_MIN", 5.0),
            volume_min=_f("VOLUME_MIN", 300_000),
            volume_gate_enabled=_b("VOLUME_GATE_ENABLED", False),
            price_min=_f("PRICE_MIN", 1.0),
            price_max=_f("PRICE_MAX", 30.0),
            parabolic_vwap_ext_pct=_f("PARABOLIC_VWAP_EXT_PCT", 40.0),
            parabolic_day_change_pct=_f("PARABOLIC_DAY_CHANGE_PCT", 120.0),
            tech_readiness_min=_f("TECH_READINESS_MIN", 60.0),
            min_history_bars=_i("MIN_HISTORY_BARS", 50),
            momentum_pillar_max=_f("MOMENTUM_PILLAR_MAX", 50.0),
            readiness_pillar_max=_f("READINESS_PILLAR_MAX", 50.0),
            adx_weight=_f("ADX_WEIGHT", 7.0),
            momentum_min_floor=_f("MOMENTUM_MIN_FLOOR", 25.0),
            alert_score_min=_f("ALERT_SCORE_MIN", 60.0),
            catalyst_lookback_hours=_f("CATALYST_LOOKBACK_HOURS", 48.0),
            catalyst_score_bonus=_f("CATALYST_SCORE_BONUS", 8.0),
            stop_fixed_pct=_f("STOP_FIXED_PCT", 7.0),
            stop_min_pct=_f("STOP_MIN_PCT", 4.0),
            stop_max_pct=_f("STOP_MAX_PCT", 20.0),
            target_max_pct=_f("TARGET_MAX_PCT", 80.0),
            min_target_profit_pct=_f("MIN_TARGET_PROFIT_PCT", 10.0),
            partial_exit_fraction=_f("PARTIAL_EXIT_FRACTION", 0.5),
            min_bar_trades=_i("MIN_BAR_TRADES", 3),
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
            postmortem_enabled=_b("POSTMORTEM_ENABLED", True),
            postmortem_on_stop=_b("POSTMORTEM_ON_STOP", True),
            backtest_enabled=_b("BACKTEST_ENABLED", True),
            backtest_lookback_days=_i("BACKTEST_LOOKBACK_DAYS", 45),
            backtest_weekday=_i("BACKTEST_WEEKDAY", 5),
            backtest_hour=_i("BACKTEST_HOUR", 6),
            backtest_scan_step_bars=_i("BACKTEST_SCAN_STEP_BARS", 1),
            backtest_top_n=_i("BACKTEST_TOP_N", 45),
            backtest_http_timeout=_f("BACKTEST_HTTP_TIMEOUT", 8.0),
            backtest_quick_days=_i("BACKTEST_QUICK_DAYS", 5),
            backtest_quick_top_n=_i("BACKTEST_QUICK_TOP_N", 12),
            backtest_quick_step=_i("BACKTEST_QUICK_STEP", 2),
            backtest_workers=_i("BACKTEST_WORKERS", 8),
            backtest_shadow_rvol=_b("BACKTEST_SHADOW_RVOL", True),
            backtest_grid_enabled=_b("BACKTEST_GRID_ENABLED", False),
            backtest_grid_readiness=_ftuple(
                "BACKTEST_GRID_READINESS", (55.0, 60.0, 65.0, 70.0)),
            backtest_grid_float_max=_ftuple(
                "BACKTEST_GRID_FLOAT_MAX",
                (40_000_000, 60_000_000, 75_000_000)),
            backtest_grid_parabolic=_ftuple(
                "BACKTEST_GRID_PARABOLIC", (120.0, 150.0)),
            backtest_grid_min_decisive=_i("BACKTEST_GRID_MIN_DECISIVE", 8),
            backtest_grid_min_edge=_f("BACKTEST_GRID_MIN_EDGE", 3.0),
            backtest_notes_enabled=_b("BACKTEST_NOTES_ENABLED", True),
            backtest_save_dir=_s("BACKTEST_SAVE_DIR", ""),
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
            premarket_caution_enabled=_b("PREMARKET_CAUTION_ENABLED", True),
            premarket_alerts_enabled=_b("PREMARKET_ALERTS_ENABLED", False),
            top_n_runners=_i("TOP_N_RUNNERS", 15),
            outcome_window_min=_f("OUTCOME_WINDOW_MIN", 90.0),
            missed_rise_pct=_f("MISSED_RISE_PCT", 30.0),
            missed_alert_enabled=_b("MISSED_ALERT_ENABLED", True),
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
