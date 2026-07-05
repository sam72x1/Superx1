"""اختبارات بطاقة التنبيه الجديدة + تصنيف الخبر."""

from __future__ import annotations

from datetime import datetime, timezone

from runner_scanner.alerts import build_card, _strength_bar
from runner_scanner.catalyst import classify_news
from runner_scanner.config import Config
from runner_scanner.models import (
    Candidate, Catalyst, FloatSource, MomentumResult, ReadinessResult,
    RiskPlan, Session, SnapshotEntry,
)

CFG = Config(code_version="abc1234")


def _card_candidate(news=True, headline="Acme Inc Announces Strategic Partnership",
                    short=1.0, session=Session.REGULAR):
    snap = SnapshotEntry("LICN", 1.54, 1.26, 1.26, 1.60, 1.25, 3_000_000,
                         1.45, 22.2)
    c = Candidate(snapshot=snap, session=session)
    c.final_score = 100
    c.float_shares = 16_400_000
    c.float_source = FloatSource.FLOAT_ENDPOINT
    c.market_cap = 25_000_000
    c.short_pct = short
    c.momentum = MomentumResult(score=48, rvol=14, rvol_5min=22,
                                change_5min_pct=4, vwap_distance_pct=3,
                                above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(
        classic_score=88, pillar_score=44, trend="صاعد", rsi=63,
        macd_bull=True, divergence="لا شيء", above_ma50=True,
        above_ma200=True, golden_cross=True)
    c.risk = RiskPlan(stop_price=1.43, stop_pct=7, entry_ref=1.54,
                      targets=[1.69, 1.85, 2.00], stop_basis="دعم 5د",
                      support_near=1.38, support_deep=1.31,
                      buy_low=1.54, buy_high=1.56)
    if news:
        c.catalyst = Catalyst(has_news=True, headline=headline,
                              publisher="GlobeNewswire",
                              category=classify_news(headline))
    else:
        c.catalyst = Catalyst(has_news=False)
    return c


def test_card_matches_template_lines():
    card = build_card(CFG, _card_candidate(),
                      now=datetime(2026, 6, 26, 15, 31, tzinfo=timezone.utc))
    assert "🟢 <b>$LICN</b>  +22.2%" in card
    assert "💪 القوة: 100/100" in card and "🔥 قوي جدًا" in card
    # كل مؤشر في سطر مستقل (مثل أعمدة الـ scanner)
    assert "🏷 الماركت كاب: 25.0M" in card
    assert "💎 الفلوت: 16.4M" in card
    assert "🩳 الشورت (فلوت): 1%" in card
    assert "📦 الحجم: 3.0M" in card
    assert "📊 RVol: 14.0x" in card
    assert "⚡ 5min Δ%: +4.0%" in card
    assert "🔥 5min RVol: 22.0x" in card
    assert "📉 الدعم الثاني (الدخول): $1.38" in card
    assert "📉 الدعم الأول: $1.31" in card
    assert "🛒 الشراء: من $1.54 إلى $1.56" in card
    assert "🎯 الهدف 1: $1.69 (+10%)" in card
    assert "🎯 الهدف 3: $2.00 (+30%)" in card
    assert "⛔ الوقف: $1.43 (-7%)" in card
    assert "من الشارت" in card    # الوقف والأهداف من الشارت لا عشوائية
    assert "(الرياض)" in card and "18:31" in card    # توقيت الرياض
    assert "🧾 إصدار الكود: abc1234" in card


def test_card_shows_target1_rr_info():
    """اعتماد 1: البطاقة تعرض عائد/مخاطرة الهدف1 كمعلومة (لا تغيّر الفرز)."""
    # هدف1 1.69 من دخول 1.54 = +9.7% ÷ وقف 7% = R/R 1.4 → «مرتفع»
    card = build_card(CFG, _card_candidate(),
                      now=datetime(2026, 6, 26, 15, 31, tzinfo=timezone.utc))
    assert "⚖️ عائد/مخاطرة الهدف1: 1.4" in card
    assert "مرتفع" in card
    # هدف قريب → «ضئيل»
    c = _card_candidate()
    c.risk.targets = [1.57, 1.85, 2.00]      # +1.9% ÷ 7% = 0.28 < 0.5
    low = build_card(CFG, c)
    assert "ضئيل" in low


def test_card_shows_stop_ratchet_ladder():
    """طلب المستخدم: البطاقة تُرشد لترقية الوقف مع كل هدف (تعادل ثم الهدف السابق)."""
    card = build_card(CFG, _card_candidate())
    # دخول 1.54 · أهداف [1.69, 1.85, 2.00]
    assert "🪜 رقِّ الوقف مع كل هدف" in card
    assert "هدف1→$1.54 (تعادل)" in card
    assert "هدف2→$1.69" in card and "هدف3→$1.85" in card


def test_followup_target_reminds_to_raise_stop():
    """طلب المستخدم: رسالة تحقيق الهدف تُذكّر برفع الوقف للمستوى المُرقّى."""
    from runner_scanner.alerts import build_followup
    # الهدف2: مستوى مطلق (الهدف1)
    msg = build_followup(CFG, {"ticker": "LICN", "type": "target", "level": 2,
                               "price": 1.85, "gain_pct": 20.0, "new_stop": 1.69})
    assert "وصل الهدف 2" in msg and "ارفع وقفك إلى $1.69" in msg
    # الهدف1: التعادل (سعر دخول المستخدم) بلا رقم مطلق قد يخالف سعر البطاقة
    m1 = build_followup(CFG, {"ticker": "LICN", "type": "target", "level": 1,
                              "price": 1.69, "gain_pct": 10.0, "new_stop": None})
    assert "وصل الهدف 1" in m1 and "ارفع وقفك للتعادل (سعر دخولك)" in m1


def test_card_shows_target_kinds():
    """منهجية المستخدم: كل هدف موسوم بنوعه (مقاومة/متوسط/قمة تأرجح)."""
    c = _card_candidate()
    c.risk.target_kinds = ["مقاومة", "متوسط ٢٠", "قمة تأرجح"]
    card = build_card(CFG, c)
    assert "🎯 الهدف 1: $1.69 (+10%) · مقاومة" in card
    assert "متوسط ٢٠" in card and "قمة تأرجح" in card


def test_card_market_stock_label():
    """منهجية المستخدم: سهم صاعد من البري + ضغط الافتتاح يُوسم «سهم ماركت»."""
    c = _card_candidate()
    c.is_market_stock = True
    assert "⭐ سهم ماركت نموذجي" in build_card(CFG, c)
    assert "سهم ماركت نموذجي" not in build_card(CFG, _card_candidate())


def test_card_session_move_hint():
    """سطر «الحركة النموذجية لهذه الجلسة» (سياق تقريبي لا وعد)."""
    card = build_card(CFG, _card_candidate())
    assert "الحركة النموذجية لهذه الجلسة" in card and "لا وعد" in card
    cfg = Config(code_version="x", session_move_hint_enabled=False)
    assert "الحركة النموذجية" not in build_card(cfg, _card_candidate())


def test_card_late_wave_caution():
    """حركة متقدّمة جدًا اليوم → تحذير «موجة أخيرة أضعف» (إرشاد لا حذف)."""
    c = _card_candidate()
    c.snapshot.change_pct = 75.0             # ≥ العتبة 60%
    card = build_card(CFG, c)
    assert "حركة متقدّمة" in card and "موجة أخيرة أضعف" in card
    assert "موجة أخيرة أضعف" not in build_card(CFG, _card_candidate())


def test_card_includes_news_summary():
    card = build_card(CFG, _card_candidate(headline="Big Pharma Gets FDA Approval"))
    assert "📰 الخبر —" in card
    assert "💊 موافقة/تجارب سريرية" in card    # صُنّف كموافقة FDA


def test_card_without_news_shows_placeholder():
    card = build_card(CFG, _card_candidate(news=False))
    assert "لا يوجد محفّز خبري حديث" in card


def test_card_short_unknown_shows_dash():
    # تعذّر الجلب ≠ صفر → «—» لا رقم
    card = build_card(CFG, _card_candidate(short=None))
    assert "🩳 الشورت: — (تعذّر الجلب)" in card


def test_card_high_short_warns():
    card = build_card(CFG, _card_candidate(short=35.0))
    assert "🩳 الشورت (فلوت): 35%" in card
    assert "ضغط بيعي" in card          # الشورت يضرّ → تحذير لا مكافأة


def test_card_extended_hours_warning():
    card = build_card(CFG, _card_candidate(session=Session.PREMARKET))
    assert "خارج الجلسة الرسمية" in card


def test_card_premarket_caution_line():
    """البريماركت يحمل تحذير «نجاحه التاريخي أضعف» (إعلام لا حذف)."""
    card = build_card(CFG, _card_candidate(session=Session.PREMARKET))
    assert "بريماركت" in card and "أضعف" in card
    # الرسمي بلا هذا التحذير
    assert "نجاحها التاريخي أضعف" not in build_card(
        CFG, _card_candidate(session=Session.REGULAR))


def test_card_premarket_caution_can_disable():
    cfg = Config(code_version="x", premarket_caution_enabled=False)
    card = build_card(cfg, _card_candidate(session=Session.PREMARKET))
    assert "نجاحها التاريخي أضعف" not in card


def test_prioritize_demotes_premarket():
    """الرسمي يسبق البريماركت حتى لو درجته أعلى قليلًا."""
    from runner_scanner.alerts import prioritize
    pm = _card_candidate(session=Session.PREMARKET); pm.final_score = 95
    reg = _card_candidate(session=Session.REGULAR); reg.final_score = 80
    order = prioritize([pm, reg])
    assert order[0].session is Session.REGULAR     # الرسمي أولًا رغم درجة أقل


def test_strength_bar_levels():
    bar, label = _strength_bar(100)
    assert bar == "██████████" and "قوي جدًا" in label
    bar, label = _strength_bar(75)
    assert label == "👍 جيد"


def test_classify_news_categories():
    assert "أرباح" in classify_news("Acme reports Q3 earnings beat, revenue up")
    assert "شراكة" in classify_news("Acme partners with Microsoft")
    assert "موافقة" in classify_news("FDA approval granted for new drug")
    assert "اندماج" in classify_news("Acme to be acquired in $2B merger")
    assert "سلبي" in classify_news("Acme announces $50M public offering")
    assert classify_news("Some unrelated headline") == "📰 خبر"
