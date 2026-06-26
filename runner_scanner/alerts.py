"""بطاقة تيليجرام + ترتيب الأولوية + إرسال.

البطاقة تعرض الحقول اللي طلبها المستخدم (من صورة الـ scanner):
الرمز · نسبة الارتفاع وقت الإشعار · الماركت كاب · الفلوت · الحجم ·
RVol · 5min Δ% · 5min RVol — بالإضافة إلى ⛔ الوقف · 🎯 الأهداف ·
💪 الدرجة، ووسم الجلسة والمحفّز وتحذيرات التوقّف.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .models import Candidate, FloatSource, Session

logger = logging.getLogger(__name__)


def _human(n: float | None) -> str:
    """تنسيق مختصر للأرقام: 1.89M، 744.96K..."""
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def _money(p: float | None) -> str:
    return f"${p:.2f}" if p is not None else "—"


# نماذج شموع القمة الهبوطية (تحذير للرَنر الصاعد)
_BEARISH_CANDLES = {
    "نجمة المساء", "ثلاثة غربان سود", "ابتلاع هابط", "غطاء داكن",
    "شهاب", "رجل مشنوق", "شاهد القبر", "ماروبوزو هابط",
}


def _strength_bar(score: float) -> tuple[str, str]:
    """يرجّع (شريط من 10 خانات، تصنيف نصّي) للقوة."""
    filled = max(0, min(10, round(score / 10.0)))
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 90:
        label = "🔥 قوي جدًا"
    elif score >= 80:
        label = "💪 قوي"
    elif score >= 70:
        label = "👍 جيد"
    else:
        label = "عادي"
    return bar, label


def _pct_from(entry: float, price: float) -> float:
    return (price - entry) / entry * 100.0 if entry else 0.0


def _local_time(cfg: Config, now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(cfg.display_tz)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Riyadh")
    return now.astimezone(tz).strftime("%H:%M")


def build_card(cfg: Config, c: Candidate, now: datetime | None = None) -> str:
    """يبني بطاقة تيليجرام بصيغة موحّدة + ملخص الخبر (HTML)."""
    s = c.snapshot
    m = c.momentum
    rk = c.readiness
    rp = c.risk
    entry = rp.entry_ref if rp else s.last_price
    bar, strength = _strength_bar(c.final_score)

    # 💎 الفلوت
    if c.float_source is FloatSource.UNKNOWN or c.float_shares is None:
        float_line = "💎 الفلوت: غير معروف ⚠️"
    else:
        suffix = " (أسهم قائمة)" \
            if c.float_source is FloatSource.SHARES_OUTSTANDING else ""
        float_line = f"💎 الفلوت: {_human(c.float_shares)}{suffix}"

    # كل مؤشر في سطر مستقل (مثل أعمدة الـ scanner)
    lines = [
        f"🟢 <b>${c.ticker}</b>  +{s.change_pct:.1f}%",
    ]
    if c.is_champion:
        lines.append("🏆 بطل الفترة السابقة (متابعة بأولوية)")
    lines += [
        f"💪 القوة: {c.final_score:.0f}/100  {bar}  {strength}",
        f"💰 السعر: {_money(s.last_price)}",
        f"🏷 الماركت كاب: {_human(c.market_cap)}",
        float_line,
    ]
    # 🩳 الشورت (يضرّ السهم) — نسبة الفلوت + حجم الشورت اليومي.
    # تعذّر = «—» لا صفر؛ نحذّر عند الارتفاع ولا نكافئه بالدرجة.
    if c.short_pct is not None:
        warn = " ⚠️ ضغط بيعي" if c.short_pct >= cfg.short_warn_pct else ""
        lines.append(f"🩳 الشورت (فلوت): {c.short_pct:.0f}%{warn}")
    if c.short_vol_pct is not None:
        lines.append(f"🩳 شورت الحجم اليومي: {c.short_vol_pct:.0f}%")
    if c.short_pct is None and c.short_vol_pct is None:
        lines.append("🩳 الشورت: — (تعذّر الجلب)")
    lines.append(f"📦 الحجم: {_human(s.day_volume)}")
    if m is not None:
        lines.append(f"📊 RVol: {m.rvol:.1f}x")
        lines.append(f"⚡ 5min Δ%: {m.change_5min_pct:+.1f}%")
        lines.append(f"🔥 5min RVol: {m.rvol_5min:.1f}x")
    if rk is not None:
        adx_part = f" · ADX {rk.adx:.0f}" if rk.adx else ""
        lines.append(f"🎓 الجاهزية الفنية: {rk.classic_score:.0f}/100{adx_part}")
        if rk.candle in _BEARISH_CANDLES:
            lines.append(f"⚠️ شمعة قمة هبوطية يومية: {rk.candle}")

    # 📉 الدعم الثاني (الدخول) + الدعم الأول
    if rp is not None:
        if rp.support_near is not None:
            lines.append(f"📉 الدعم الثاني (الدخول): {_money(rp.support_near)}")
        if rp.support_deep is not None:
            lines.append(f"📉 الدعم الأول: {_money(rp.support_deep)}")
        # 🛒 منطقة الشراء
        if rp.buy_low is not None and rp.buy_high is not None:
            lines.append(
                f"🛒 الشراء: من {_money(rp.buy_low)} إلى {_money(rp.buy_high)}")
        # 🎯 الأهداف بنسبها
        for i, t in enumerate(rp.targets, start=1):
            lines.append(
                f"🎯 الهدف {i}: {_money(t)} (+{_pct_from(entry, t):.0f}%)")
        # ⛔ الوقف
        lines.append(
            f"⛔ الوقف: {_money(rp.stop_price)} (-{rp.stop_pct:.0f}%)")
        lines.append("↑ الوقف والأهداف من الشارت (دعوم/مقاومات حقيقية)")

    # 📰 ملخص الخبر (مطلب المستخدم)
    if c.catalyst is not None and c.catalyst.has_news:
        cat = c.catalyst.category or "📰 خبر"
        head = (c.catalyst.headline or "").strip()[:110]
        lines.append(f"📰 الخبر — {cat}: {head}")
        if c.catalyst.publisher:
            lines.append(f"   ↳ المصدر: {c.catalyst.publisher}")
    else:
        lines.append("📰 الخبر: لا يوجد محفّز خبري حديث ⚠️")

    # تحذير خارج الجلسة الرسمية (LULD لا يحمي)
    if c.session in (Session.PREMARKET, Session.AFTERHOURS):
        lines.append("⚠️ خارج الجلسة الرسمية: LULD لا يحمي، احتمال فجوة.")

    code = cfg.code_version or "dev"
    lines.append(f"⏰ {_local_time(cfg, now)} (الرياض) · {c.session.value}")
    lines.append(f"🧾 إصدار الكود: {code}")
    return "\n".join(lines)


def prioritize(candidates: list[Candidate]) -> list[Candidate]:
    """ترتيب أولوية: الأعلى درجة أولًا (لا يُغرق اليوم الحار)."""
    return sorted(candidates, key=lambda c: c.final_score, reverse=True)


def build_followup(cfg: Config, event: dict, now: datetime | None = None) -> str:
    """يبني رسالة تحديث متابعة لحدث (🎯 هدف · ⛔ وقف · 🚀 قفزة)."""
    tkr = event.get("ticker", "")
    price = event.get("price")
    gain = event.get("gain_pct", 0.0)
    etype = event.get("type")
    when = f"⏰ {_local_time(cfg, now)} (الرياض)"
    if etype == "target":
        lvl = event.get("level", 1)
        return (f"🎯 <b>${tkr}</b> وصل الهدف {lvl}!  "
                f"{_money(price)} (+{gain:.0f}%)\n{when}")
    if etype == "stop":
        return (f"⛔ <b>${tkr}</b> كسر الوقف  "
                f"{_money(price)} ({gain:.0f}%)\n{when}")
    if etype == "surge":
        return (f"🚀 <b>${tkr}</b> قفزة قوية!  "
                f"{_money(price)} (+{gain:.0f}% من الدخول)\n{when}")
    return f"ℹ️ <b>${tkr}</b> تحديث: {_money(price)}\n{when}"


class TelegramSender:
    """يرسل البطاقات عبر Telegram Bot API (sendMessage)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._url = (
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
        )

    def send(self, text: str) -> bool:
        if self.cfg.dry_run:
            logger.info("[DRY_RUN] بطاقة:\n%s", text)
            print(text)
            return True
        try:
            resp = requests.post(self._url, json={
                "chat_id": self.cfg.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
            if resp.status_code != 200:
                logger.error("تيليجرام رفض (%s): %s", resp.status_code,
                             resp.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            logger.error("فشل إرسال تيليجرام: %s", exc)
            return False

    def send_document(self, path: str, caption: str = "") -> bool:
        """يرسل ملفًا (CSV...) عبر sendDocument."""
        if self.cfg.dry_run:
            logger.info("[DRY_RUN] ملف: %s (%s)", path, caption)
            print(f"[ملف] {path} — {caption}")
            return True
        url = (f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"
               "/sendDocument")
        try:
            with open(path, "rb") as fh:
                resp = requests.post(url, data={
                    "chat_id": self.cfg.telegram_chat_id,
                    "caption": caption[:1000],
                }, files={"document": fh}, timeout=30)
            if resp.status_code != 200:
                logger.error("تيليجرام رفض الملف (%s): %s", resp.status_code,
                             resp.text[:200])
                return False
            return True
        except (requests.RequestException, OSError) as exc:
            logger.error("فشل إرسال الملف: %s", exc)
            return False
