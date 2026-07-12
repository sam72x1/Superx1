"""بطاقة تيليجرام + ترتيب الأولوية + إرسال.

البطاقة تعرض الحقول اللي طلبها المستخدم (من صورة الـ scanner):
الرمز · نسبة الارتفاع وقت الإشعار · الماركت كاب · الفلوت · الحجم ·
RVol · 5min Δ% · 5min RVol — بالإضافة إلى ⛔ الوقف · 🎯 الأهداف ·
💪 الدرجة، ووسم الجلسة والمحفّز وتحذيرات التوقّف.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .models import Candidate, FloatSource, Session
from .sessions import session_move_hint_pct
from .textutil import esc

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


# نماذج شموع القمة الهبوطية (تحذير للسهم الصاعد)
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
        f"🟢 <b>${esc(c.ticker)}</b>  +{s.change_pct:.1f}%",
    ]
    if c.is_champion:
        lines.append("🏆 بطل الفترة السابقة (متابعة بأولوية)")
    # ⭐ «سهم الماركت» النموذجي (منهجية المستخدم): صاعد من البري + نافذة افتتاح + ضغط
    if c.is_market_stock:
        lines.append("⭐ سهم ماركت نموذجي: صاعد من البري + ضغط نافذة الافتتاح")
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
    # ⚠️ التخفيف (SEC) — طرح/إصدار أسهم يضرّ السهم الصاعد (كالشورت)
    if c.dilution is not None and c.dilution.is_active:
        icon = "🔴" if c.dilution.risk == "مرتفع" else "🟠"
        # §5: note مبنيّ من سلسلة نموذج SEC الخام (JSON خارجي) — لولا esc()
        # لأسقط محرف < واحد البطاقة كلها (400). risk مضبوط داخليًا، نهرّبه للاتّساق.
        lines.append(
            f"{icon} تخفيف {esc(c.dilution.risk)} (SEC): {esc(c.dilution.note)}")
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
        # 🎯 الأهداف بنسبها + نوع كل هدف (منهجية المستخدم: ه١ مقاومة · ه٢
        # متوسط ٢٠/٥٠ · ه٣ قمة تأرجح). النوع للعرض لا يغيّر ترتيب السعر التصاعدي.
        for i, t in enumerate(rp.targets, start=1):
            kind = (f" · {rp.target_kinds[i - 1]}"
                    if rp.target_kinds and i <= len(rp.target_kinds) else "")
            lines.append(
                f"🎯 الهدف {i}: {_money(t)} (+{_pct_from(entry, t):.0f}%){kind}")
        # ⛔ الوقف
        lines.append(
            f"⛔ الوقف: {_money(rp.stop_price)} (-{rp.stop_pct:.0f}%)")
        # 🪜 ترقية الوقف مع كل هدف يتحقّق (يقفل الربح تدريجيًا): بعد الهدف1 ارفع
        # الوقف للتعادل، وبعد كل هدف تالٍ ارفعه للهدف السابق. إرشاد لا تنفيذ.
        if rp.targets:
            steps = [f"هدف1→{_money(entry)} (تعادل)"]
            for i in range(2, len(rp.targets) + 1):
                steps.append(f"هدف{i}→{_money(rp.targets[i - 2])}")
            lines.append("🪜 رقِّ الوقف مع كل هدف: " + " · ".join(steps))
        lines.append("↑ الوقف والأهداف من الشارت (دعوم/مقاومات حقيقية)")
        # ⚖️ عائد/مخاطرة الهدف1 (معلومة لتقرّر الإمساك يدويًا — لا يغيّر الفرز):
        # الباكتيست (6 أشهر) بيّن أن «الهدف القريب» يُصاب كثيرًا لكن ربحه ضئيل،
        # بينما الأهداف الأبعد مجالها أوسع. تُعلِمك لتختار متى تمسك بعد الهدف1.
        if rp.stop_pct and rp.targets:
            rr = _pct_from(entry, rp.targets[0]) / rp.stop_pct
            tag = ("ضئيل — هدف قريب، ربح صغير" if rr < 0.5
                   else "متوازن" if rr < 1.0
                   else "مرتفع — هدف أبعد، مجال أوسع")
            lines.append(f"⚖️ عائد/مخاطرة الهدف1: {rr:.1f} ({tag})")
        # 📐 المتوسطات ٢٠/٥٠ (منهجية المستخدم) — مؤطّرة بما هي فعلًا للرنر:
        # فوق السعر = هدف استعادة · تحت السعر = دعم/تأكيد اتجاه صاعد.
        if rp.ma20 or rp.ma50:
            parts = []
            if rp.ma20:
                parts.append(f"٢٠:{_money(rp.ma20)}")
            if rp.ma50:
                parts.append(f"٥٠:{_money(rp.ma50)}")
            above_price = [x for x in (rp.ma20, rp.ma50) if x and x > entry]
            rel = ("متوسط فوق السعر (مقاومة/استعادة)"
                   if above_price else "السعر فوقهما — دعم/اتجاه صاعد")
            lines.append(f"📐 المتوسطات — {' · '.join(parts)} ({rel})")

    # 🧠 رؤية المحلّل الذكي (Claude) — نص حرّ من النموذج → يُهرَّب
    if c.analyst is not None and c.analyst.thesis:
        a = c.analyst
        lines.append(f"🧠 المحلّل: {esc(a.thesis)} "
                     f"(محفّز {esc(a.direction)} {a.materiality}/10)")
        if a.warning:
            lines.append(f"🔴 تحذير المحلّل: {esc(a.warning)}")

    # 📰 ملخص الخبر — العنوان/المصدر خارجيان → يُهرَّبان (وإلا تُرفَض البطاقة)
    if c.catalyst is not None and c.catalyst.has_news:
        cat = c.catalyst.category or "📰 خبر"
        head = esc((c.catalyst.headline or "").strip()[:110])
        lines.append(f"📰 الخبر — {cat}: {head}")
        if c.catalyst.publisher:
            lines.append(f"   ↳ المصدر: {esc(c.catalyst.publisher)}")
    else:
        lines.append("📰 الخبر: لا يوجد محفّز خبري حديث ⚠️")

    # ⚠️ حركة متقدّمة جدًا اليوم = احتمال قرب نهاية الموجة (الخامسة الأضعف):
    # إرشاد بمراقبة الجني وتشديد الوقف (منهجية المستخدم) — لا يمنع التنبيه.
    if cfg.late_wave_caution_enabled and s.change_pct >= cfg.late_wave_run_pct:
        lines.append(
            f"⚠️ حركة متقدّمة (+{s.change_pct:.0f}%) — قد تكون موجة أخيرة أضعف "
            "(الخامسة)؛ راقب الجني وشدّد الوقف.")

    # تحذير خارج الجلسة الرسمية (LULD لا يحمي)
    if c.session in (Session.PREMARKET, Session.AFTERHOURS):
        lines.append("⚠️ خارج الجلسة الرسمية: LULD لا يحمي، احتمال فجوة.")
    # تنبيه البريماركت: أداؤه التاريخي في الباكتيست أضعف بوضوح (إعلام لا حذف)
    if c.session is Session.PREMARKET and cfg.premarket_caution_enabled:
        lines.append("🔅 جلسة بريماركت — نجاحها التاريخي أضعف، تحقّق يدويًا قبل الدخول.")
    # تنبيه الأفترهاوس: عيّنة 6 أشهر ضعيفة (33% متحفّظًا، أغلبها بلا حسم) — إعلام لا حذف
    if c.session is Session.AFTERHOURS and cfg.afterhours_caution_enabled:
        lines.append("🌙 أفترهاوس — عيّنة تاريخية ضعيفة (33% نجاحًا، أغلبها بلا حسم)؛ تحقّق يدويًا.")

    # 📊 الحركة النموذجية لهذه الجلسة (سياق تقريبي من خبرة المستخدم، لا وعد)
    if cfg.session_move_hint_enabled:
        hint = session_move_hint_pct(cfg, c.session, now)
        if hint:
            lines.append(f"📊 الحركة النموذجية لهذه الجلسة: ~{hint:.0f}% "
                         "(تاريخي تقريبي، لا وعد)")

    code = cfg.code_version or "dev"
    lines.append(f"⏰ {_local_time(cfg, now)} (الرياض) · {c.session.value}")
    lines.append(f"🧾 إصدار الكود: {code}")
    return "\n".join(lines)


def prioritize(candidates: list[Candidate]) -> list[Candidate]:
    """ترتيب أولوية: غير-البريماركت أولًا (أقوى تاريخيًا)، ثم الأعلى درجة.
    البريماركت يُدفَع لأسفل (أداؤه أضعف في الباكتيست) — إعلام لا حذف."""
    return sorted(
        candidates,
        key=lambda c: (0 if c.session is Session.PREMARKET else 1, c.final_score),
        reverse=True)


def build_followup(cfg: Config, event: dict, now: datetime | None = None) -> str:
    """يبني رسالة تحديث متابعة لحدث (🎯 هدف · ⛔ وقف · 🚀 قفزة)."""
    tkr = esc(event.get("ticker", ""))   # رمز من الفيد → يُهرَّب (يدخل كل الفروع)
    price = event.get("price")
    gain = event.get("gain_pct", 0.0)
    etype = event.get("type")
    when = f"⏰ {_local_time(cfg, now)} (الرياض)"
    part = event.get("participation")
    part_line = f"\n📊 مشاركة الحجم: {part}" if part else ""
    if etype == "target":
        lvl = event.get("level", 1)
        ns = event.get("new_stop")
        # 🪜 تذكير ترقية الوقف مع كل هدف (يقفل الربح) — إرشاد لا تنفيذ. الهدف1:
        # «للتعادل (سعر دخولك)» بلا رقم (سعر دخول المستخدم، لا first_price)؛ التالي:
        # المستوى المطلق (الهدف السابق) المطابق للبطاقة.
        if lvl == 1:
            ratchet = "\n🪜 ارفع وقفك للتعادل (سعر دخولك)"
        elif ns:
            ratchet = f"\n🪜 ارفع وقفك إلى {_money(ns)}"
        else:
            ratchet = ""
        return (f"🎯 <b>${tkr}</b> وصل الهدف {lvl}!  "
                f"{_money(price)} (+{gain:.0f}%){part_line}{ratchet}\n{when}")
    if etype == "stop":
        return (f"⛔ <b>${tkr}</b> كسر الوقف  "
                f"{_money(price)} ({gain:.0f}%)\n{when}")
    if etype == "surge":
        return (f"🚀 <b>${tkr}</b> قفزة قوية!  "
                f"{_money(price)} (+{gain:.0f}% من الدخول){part_line}\n{when}")
    if etype == "missed":
        reason = esc(event.get("reason", "") or "غير مسجّل")
        return (f"👻 <b>${tkr}</b> فرصة فائتة — صعد +{gain:.0f}%!  {_money(price)}\n"
                f"كان مرفوضًا بسبب: {reason}\n"
                f"<i>راجِع هذي البوّابة لو تكرّرت.</i>\n{when}")
    return f"ℹ️ <b>${tkr}</b> تحديث: {_money(price)}\n{when}"


_TG_LIMIT = 4096   # أقصى طول رسالة في Telegram (محارف)


def _split_message(text: str, limit: int = _TG_LIMIT) -> list[str]:
    """يقسّم رسالة طويلة إلى أجزاء ≤ الحد، على حدود الأسطر (يحافظ على الوسوم
    لأن كل سطر مكتفٍ بوسومه). سطر أطول من الحد (نادر) يُقصّ قسرًا."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:                 # سطر ضخم → قصّ قسري
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


class TelegramSender:
    """يرسل البطاقات عبر Telegram Bot API (sendMessage)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._url = (
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
        )

    @staticmethod
    def _retry_after(resp: requests.Response) -> float:
        """يستخرج مدّة الانتظار من رد 429 (parameters.retry_after أو الترويسة)."""
        try:
            ra = (resp.json().get("parameters") or {}).get("retry_after")
            if ra:
                return float(ra)
        except ValueError:
            pass
        try:
            return float(resp.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            return 1.0

    def send(self, text: str, _retries: int = 2) -> bool:
        if self.cfg.dry_run:
            logger.info("[DRY_RUN] بطاقة:\n%s", text)
            print(text)
            return True
        # تقسيم الرسائل الطويلة (تيليجرام يرفض ما يتجاوز 4096 → ضياع كامل)
        ok = True
        for chunk in _split_message(text):
            ok = self._send_chunk(chunk, _retries) and ok
        return ok

    def _send_chunk(self, text: str, _retries: int = 2) -> bool:
        payload = {
            "chat_id": self.cfg.telegram_chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        for attempt in range(_retries + 1):
            try:
                resp = requests.post(self._url, json=payload, timeout=15)
            except requests.RequestException as exc:
                logger.error("فشل إرسال تيليجرام: %s", exc)
                return False
            if resp.status_code == 429 and attempt < _retries:
                wait = min(self._retry_after(resp), 30.0)
                logger.warning("تيليجرام 429 — انتظار %.0fث ثم إعادة", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                logger.error("تيليجرام رفض (%s): %s", resp.status_code,
                             resp.text[:200])
                return False
            return True
        return False

    def send_document(self, path: str, caption: str = "",
                      _retries: int = 2) -> bool:
        """يرسل ملفًا (CSV...) عبر sendDocument مع معالجة 429."""
        if self.cfg.dry_run:
            logger.info("[DRY_RUN] ملف: %s (%s)", path, caption)
            print(f"[ملف] {path} — {caption}")
            return True
        url = (f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"
               "/sendDocument")
        for attempt in range(_retries + 1):
            try:
                with open(path, "rb") as fh:   # نعيد فتح الملف لكل محاولة
                    resp = requests.post(url, data={
                        "chat_id": self.cfg.telegram_chat_id,
                        "caption": caption[:1000],
                    }, files={"document": fh}, timeout=30)
            except (requests.RequestException, OSError) as exc:
                logger.error("فشل إرسال الملف: %s", exc)
                return False
            if resp.status_code == 429 and attempt < _retries:
                time.sleep(min(self._retry_after(resp), 30.0))
                continue
            if resp.status_code != 200:
                logger.error("تيليجرام رفض الملف (%s): %s", resp.status_code,
                             resp.text[:200])
                return False
            return True
        return False
