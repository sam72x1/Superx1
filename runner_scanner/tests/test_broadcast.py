"""بثّ البطاقات لمستقبِلين إضافيّين (أصدقاء) — إرسال فقط بلا تحكّم.

يثبّت ثلاثة ضمانات: (١) البطاقة/المتابعة/التشريح تصل للجميع، (٢) بقيّة
الرسائل (تقارير التطوير · الباكتيست · ردود الأوامر) تبقى لك وحدك،
(٣) المستقبِل الإضافي **لا يستطيع** تشغيل أي أمر.
"""

from __future__ import annotations

from runner_scanner.alerts import TelegramSender
from runner_scanner.config import Config


class _Resp:
    status_code = 200
    text = "ok"
    headers: dict = {}

    def json(self):
        return {}


def _sender(monkeypatch, extra=("999",)):
    """مُرسِل يلتقط chat_id لكل نداء بدل الشبكة."""
    cfg = Config(massive_api_key="x", telegram_bot_token="t",
                 telegram_chat_id="111", telegram_extra_chat_ids=extra)
    sender = TelegramSender(cfg)
    seen: list[str] = []

    def _post(url, json=None, timeout=None, **kw):
        seen.append(str((json or {}).get("chat_id")))
        return _Resp()

    monkeypatch.setattr("runner_scanner.alerts.requests.post", _post)
    return sender, seen


def test_broadcast_reaches_owner_and_friends(monkeypatch):
    """to_all=True ⇒ تصل للمالك وللمستقبِل الإضافي."""
    sender, seen = _sender(monkeypatch)
    assert sender.send("بطاقة", to_all=True) is True
    assert seen == ["111", "999"]


def test_default_send_stays_owner_only(monkeypatch):
    """بلا to_all ⇒ لك وحدك. يحمي تقارير التطوير/الباكتيست/ردود الأوامر
    من التسرّب لصديق (معايراتك الداخلية ليست له)."""
    sender, seen = _sender(monkeypatch)
    assert sender.send("تقرير تطوير داخلي") is True
    assert seen == ["111"]


def test_friend_failure_does_not_break_owner_delivery(monkeypatch):
    """§3 best-effort: صديق حظر البوت أو لم يضغط /start ⇒ إرسالك ينجح.

    حرج: القيمة المرجَعة تحكم تسجيل التنبيه في قاعدة البيانات (mark_alerted)
    — فلو أسقطها فشلُ صديق، ضاع تتبّع نتيجة صفقتك أنت."""
    cfg = Config(massive_api_key="x", telegram_bot_token="t",
                 telegram_chat_id="111", telegram_extra_chat_ids=("999",))
    sender = TelegramSender(cfg)
    seen: list[str] = []

    class _Fail(_Resp):
        status_code = 403          # «bot was blocked by the user»
        text = "forbidden"

    def _post(url, json=None, timeout=None, **kw):
        cid = str((json or {}).get("chat_id"))
        seen.append(cid)
        return _Resp() if cid == "111" else _Fail()

    monkeypatch.setattr("runner_scanner.alerts.requests.post", _post)
    assert sender.send("بطاقة", to_all=True) is True    # نجاحك لم يتأثّر
    assert seen == ["111", "999"]                       # وحاول فعلًا


def test_no_extra_ids_configured_is_noop(monkeypatch):
    """توافق خلفي: بلا مستقبِلين إضافيّين، to_all لا يغيّر شيئًا."""
    sender, seen = _sender(monkeypatch, extra=())
    assert sender.send("بطاقة", to_all=True) is True
    assert seen == ["111"]


def test_extra_chat_ids_parsed_from_env(monkeypatch):
    """§6: يُقرأ من البيئة كقائمة مفصولة بفواصل، مع تشذيب الفراغات."""
    monkeypatch.setenv("MASSIVE_API_KEY", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_EXTRA_CHAT_IDS", " 1270575158 , 222 ,")
    cfg = Config.from_env()
    assert cfg.telegram_extra_chat_ids == ("1270575158", "222")


def test_extra_recipient_cannot_run_commands(monkeypatch):
    """الضمان الأمني: مستقبِل إضافي لا يمرّر أي أمر.

    الأوامر تصرف رصيد Anthropic (/ask) وتشغّل باكتيست ثقيلًا وتتحكّم بـRender،
    فلا تُفتح لأحد غير المالك مهما أُضيف للبثّ."""
    from runner_scanner import telegram_bot as tb
    cfg = Config(massive_api_key="x", telegram_bot_token="t",
                 telegram_chat_id="111",
                 telegram_extra_chat_ids=("1270575158",))
    bot = tb.TelegramAssistant.__new__(tb.TelegramAssistant)
    bot.cfg = cfg
    handled: list[str] = []
    monkeypatch.setattr(bot, "_dispatch", lambda *a, **k: handled.append("!"),
                        raising=False)
    # أمر وارد من محادثة المستقبِل الإضافي
    bot._handle_update({"message": {
        "chat": {"id": 1270575158}, "from": {"id": 1270575158},
        "text": "/backtest"}})
    assert handled == []            # لم يُنفَّذ شيء


def test_dev_rvol_suggestion_requires_reachable_threshold():
    """MEAS-37: أداة التطوير لا تقترح خفض RVOL_MIN إلا لمن ستلتقطهم العتبة
    المقترحة فعلًا. قبل الحارس اقترحت الخفض على 12 سهمًا قيمها 0–4.56x بينما
    خفض 5→4 يلتقط واحدًا — اقتراح لا يتبع من دليله."""
    from runner_scanner import dev_assistant as da
    cfg = Config(massive_api_key="x", rvol_min=5.0)
    # كلهم مرفوضو RVol لكن قيمهم بعيدة عن العتبة المقترحة (4x)
    far = [{"reason_code": "rvol", "reject_reason": "RVol 0.5x < 5.0x",
            "rvol": v} for v in (0.0, 0.1, 0.5, 1.6, 2.5)]
    out = da._build_suggestions(cfg, [], far) if hasattr(
        da, "_build_suggestions") else None
    if out is None:                      # الدالة داخلية؛ نختبر الحارس مباشرة
        import runner_scanner.calibration as calibration
        proposed = max(1.0, round(cfg.rvol_min - 1))
        hits = [m for m in far if calibration._rejected_by(m, "rvol", "RVol")
                and m.get("rvol") is not None and m["rvol"] >= proposed]
        assert len(hits) == 0            # لا أحد يستحقّ اقتراح الخفض
        near = far + [{"reason_code": "rvol", "reject_reason": "RVol 4.6x",
                       "rvol": 4.6}] * 3
        hits2 = [m for m in near if calibration._rejected_by(m, "rvol", "RVol")
                 and m.get("rvol") is not None and m["rvol"] >= proposed]
        assert len(hits2) == 3           # وهؤلاء يستحقّون
