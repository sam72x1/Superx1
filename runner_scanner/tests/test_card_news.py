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
