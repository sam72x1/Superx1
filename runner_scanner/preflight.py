"""فحص جاهزية: تأكّد أن المفاتيح والاتصالات تشتغل قبل التشغيل الحقيقي.

التشغيل:  python -m runner_scanner.preflight

يفحص:
1. وجود المتغيّرات الإلزامية.
2. مصادقة Massive + رجوع بيانات + تقدير إذا كانت لحظية (لا متأخّرة 15د).
3. توكن تيليجرام صالح + يرسل رسالة تجريبية إلى chat_id.

يطبع تقريرًا واضحًا ويرجّع رمز خروج 0 (نجاح) أو 1 (فشل).
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import requests

from .config import Config
from .massive_client import MassiveClient, MassiveError
from .sessions import classify_session, now_et
from .models import Session

_PROBE_TICKER = "AAPL"   # رمز سائل للتشخيص


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def check_env(cfg: Config) -> bool:
    print("1) المتغيّرات الإلزامية:")
    missing = cfg.missing_required()
    if missing:
        _fail(f"ناقصة: {', '.join(missing)} — املأها في .env أو لوحة ريندر")
        return False
    _ok("MASSIVE_API_KEY · TELEGRAM_BOT_TOKEN · TELEGRAM_CHAT_ID موجودة")
    return True


def check_massive(cfg: Config) -> bool:
    print("2) Massive (مصدر البيانات):")
    client = MassiveClient(cfg)
    try:
        client.market_status()
        _ok("المصادقة نجحت (المفتاح صحيح)")
    except MassiveError as exc:
        _fail(f"فشل الاتصال/المصادقة: {exc}")
        return False

    # تقدير اللحظية من طابع آخر تحديث لرمز سائل
    try:
        snap = client.single_snapshot(_PROBE_TICKER)
    except MassiveError as exc:
        _warn(f"تعذّر جلب سنابشوت {_PROBE_TICKER}: {exc}")
        return True   # المصادقة نجحت؛ نكمل

    if snap is None or not snap.is_valid:
        _warn(f"ما رجعت بيانات صالحة لـ {_PROBE_TICKER} (قد يكون السوق مغلقًا)")
        return True

    _ok(f"بيانات {_PROBE_TICKER} رجعت (سعر {snap.last_price})")
    session = classify_session(cfg)
    if snap.updated_ns and session is Session.REGULAR:
        age_sec = time.time() - (snap.updated_ns / 1e9)
        if age_sec > 15 * 60:
            _warn(f"آخر تحديث قبل {age_sec/60:.0f} دقيقة أثناء الجلسة الرسمية "
                  "— يبدو أن اشتراكك **متأخّر 15د** لا لحظي! راجع تصنيفك "
                  "(non-professional + إقرار SIP).")
        else:
            _ok(f"البيانات تبدو لحظية (آخر تحديث قبل {max(0,age_sec):.0f}ث)")
    else:
        _warn("تعذّر تقدير اللحظية الآن (خارج الجلسة الرسمية) — "
              "تأكّد يدويًا أن باقتك real-time.")
    return True


def check_telegram(cfg: Config) -> bool:
    print("3) تيليجرام:")
    base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    try:
        me = requests.get(f"{base}/getMe", timeout=15).json()
    except requests.RequestException as exc:
        _fail(f"تعذّر الوصول لتيليجرام: {exc}")
        return False
    if not me.get("ok"):
        _fail(f"توكن غير صالح: {me.get('description', me)}")
        return False
    _ok(f"التوكن صحيح (@{me['result'].get('username')})")

    # رسالة تجريبية
    text = ("🧪 <b>فحص جاهزية الماسح الشامل</b>\n"
            f"الوقت (ET): {now_et().strftime('%Y-%m-%d %H:%M')}\n"
            "إذا وصلتك هذي الرسالة فالربط مع تيليجرام شغّال ✅")
    try:
        resp = requests.post(f"{base}/sendMessage", json={
            "chat_id": cfg.telegram_chat_id, "text": text,
            "parse_mode": "HTML"}, timeout=15).json()
    except requests.RequestException as exc:
        _fail(f"فشل إرسال الرسالة التجريبية: {exc}")
        return False
    if not resp.get("ok"):
        _fail(f"تعذّر الإرسال إلى chat_id={cfg.telegram_chat_id}: "
              f"{resp.get('description', resp)}")
        return False
    _ok("وصلت رسالة تجريبية إلى محادثتك — افحص تيليجرام")
    return True


def main() -> int:
    cfg = Config.from_env()
    print("══════════ فحص جاهزية الماسح الشامل ══════════")
    results = [
        check_env(cfg),
    ]
    if results[0]:
        results.append(check_massive(cfg))
        results.append(check_telegram(cfg))
    print("──────────────────────────────────────────────")
    if all(results):
        print("✅ كل شي جاهز — تقدر تشغّل: python -m runner_scanner.main")
        return 0
    print("❌ فيه مشاكل أعلاه — صلّحها قبل التشغيل.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
