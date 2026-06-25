"""بطاقة تيليجرام + ترتيب الأولوية + إرسال.

البطاقة تعرض الحقول اللي طلبها المستخدم (من صورة الـ scanner):
الرمز · نسبة الارتفاع وقت الإشعار · الماركت كاب · الفلوت · الحجم ·
RVol · 5min Δ% · 5min RVol — بالإضافة إلى ⛔ الوقف · 🎯 الأهداف ·
💪 الدرجة، ووسم الجلسة والمحفّز وتحذيرات التوقّف.
"""

from __future__ import annotations

import logging

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
            return f"{n / div:.2f}{unit}"
    return f"{n:.0f}"


def _money(p: float | None) -> str:
    return f"${p:.2f}" if p is not None else "—"


def build_card(cfg: Config, c: Candidate) -> str:
    """يبني نص بطاقة تيليجرام (HTML)."""
    s = c.snapshot
    m = c.momentum
    rk = c.readiness
    rp = c.risk

    float_label = _human(c.float_shares)
    if c.float_source is FloatSource.SHARES_OUTSTANDING:
        float_label += " (أسهم قائمة، ليس فلوت)"
    elif c.float_source is FloatSource.UNKNOWN:
        float_label = "غير معروف ⚠️"

    lines = [
        f"🚀 <b>${c.ticker}</b> — رَنر +{s.change_pct:.1f}% "
        f"<i>(وقت الإشعار)</i>",
        f"💪 الدرجة: <b>{c.final_score:.0f}/100</b>"
        f"  ·  🗓 {c.session.value}",
        "",
        f"💵 السعر: {_money(s.last_price)}   "
        f"🏷 الماركت كاب: {_human(c.market_cap)}",
        f"🪙 الفلوت: {float_label}   📦 الحجم: {_human(s.day_volume)}",
    ]

    if m is not None:
        lines.append(
            f"📊 RVol: {m.rvol:.1f}x   "
            f"⚡ 5min Δ: {m.change_5min_pct:+.1f}%   "
            f"🔥 5min RVol: {m.rvol_5min:.1f}x"
        )
        vwap_pos = "فوق VWAP ✅" if m.above_vwap else "تحت VWAP ⚠️"
        lines.append(f"📈 {vwap_pos} ({m.vwap_distance_pct:+.1f}%)")

    if rk is not None:
        hist = " · تاريخ محدود ⚠️" if rk.limited_history else ""
        lines.append(
            f"🎓 الجاهزية الفنية: <b>{rk.classic_score:.0f}/100</b> "
            f"(اتجاه {rk.trend}، RSI {rk.rsi:.0f}){hist}"
        )

    if rp is not None:
        targets = " · ".join(_money(t) for t in rp.targets)
        lines += [
            "",
            f"⛔ الوقف: {_money(rp.stop_price)} (-{rp.stop_pct:.1f}% · {rp.stop_basis})",
            f"🎯 الأهداف: {targets}",
        ]

    if c.catalyst is not None and c.catalyst.has_news:
        head = c.catalyst.headline[:90]
        lines += ["", f"📰 محفّز ✓: {head}"]
        if c.catalyst.url:
            lines.append(f"🔗 {c.catalyst.url}")

    # تحذيرات الجلسة (LULD لا يحمي خارج الجلسة الرسمية)
    if c.session in (Session.PREMARKET, Session.AFTERHOURS):
        lines += ["", "⚠️ خارج الجلسة الرسمية: LULD لا يحمي، احتمال فجوة عند الفتح."]

    lines += ["", "<i>تنبيه فقط — القرار والتنفيذ عليك.</i>"]
    return "\n".join(lines)


def prioritize(candidates: list[Candidate]) -> list[Candidate]:
    """ترتيب أولوية: الأعلى درجة أولًا (لا يُغرق اليوم الحار)."""
    return sorted(candidates, key=lambda c: c.final_score, reverse=True)


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
