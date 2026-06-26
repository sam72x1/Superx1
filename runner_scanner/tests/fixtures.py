"""بيانات وهمية وعميل وهمي للاختبارات (بلا إنترنت)."""

from __future__ import annotations

import math

from runner_scanner.models import Bar, Catalyst, FloatSource, SnapshotEntry


def make_snapshot(ticker="RUNR", last=2.5, prev=2.0, vol=1_500_000,
                  change_pct=25.0) -> SnapshotEntry:
    return SnapshotEntry(
        ticker=ticker, last_price=last, prev_close=prev,
        day_open=prev, day_high=last * 1.08, day_low=prev * 0.98,
        day_volume=vol, day_vwap=(last + prev) / 2, change_pct=change_pct,
    )


def rising_5min_bars(n=12, base=2.0, vol0=80_000, vstep=9_000) -> list[Bar]:
    return [
        Bar(t_ms=i, o=base + i * 0.02, h=base + i * 0.03,
            l=base + i * 0.01, c=base + i * 0.025, v=vol0 + i * vstep, n=50)
        for i in range(n)
    ]


def flat_1min_bars(n=60, price=2.4, vol=20_000) -> list[Bar]:
    return [Bar(t_ms=i, o=price, h=price * 1.02, l=price * 0.98,
                c=price, v=vol) for i in range(n)]


def uptrend_daily_bars(n=260) -> list[Bar]:
    closes = [8 + math.sin(i / 4) + i * 0.02 for i in range(n)]
    return [
        Bar(t_ms=i, o=closes[i] - 0.1, h=closes[i] + 0.2,
            l=closes[i] - 0.2, c=closes[i], v=1_000_000 + (i % 4) * 300_000,
            n=100)
        for i in range(n)
    ]


def downtrend_daily_bars(n=260) -> list[Bar]:
    closes = [30 - i * 0.05 + math.sin(i / 5) for i in range(n)]
    closes = [max(1.0, c) for c in closes]
    return [
        Bar(t_ms=i, o=closes[i] + 0.1, h=closes[i] + 0.2,
            l=closes[i] - 0.2, c=closes[i], v=900_000, n=80)
        for i in range(n)
    ]


class FakeClient:
    """عميل Massive وهمي قابل للتخصيص لكل سيناريو اختبار."""

    def __init__(self, *, float_shares=1_890_000,
                 float_source=FloatSource.FLOAT_ENDPOINT,
                 shares=3_000_000, daily=None, bars5=None, bars1=None,
                 news=True, short=None):
        self._float = float_shares
        self._float_source = float_source
        self._shares = shares
        self._daily = daily if daily is not None else uptrend_daily_bars()
        self._bars5 = bars5 if bars5 is not None else rising_5min_bars()
        self._bars1 = bars1 if bars1 is not None else flat_1min_bars()
        self._news = news
        self._short = short

    def free_float(self, ticker):
        return self._float, self._float_source

    def shares_outstanding(self, ticker):
        return self._shares

    def short_interest(self, ticker):
        return self._short

    def bars_5min(self, ticker, start, end):
        return list(self._bars5)

    def bars_1min(self, ticker, start, end):
        return list(self._bars1)

    def bars_daily(self, ticker, start, end):
        return list(self._daily)

    def latest_news(self, ticker, published_gte_utc, limit=5):
        if not self._news:
            return None
        return Catalyst(has_news=True, headline="Phase 3 trial success",
                        publisher="GlobeNewswire",
                        published_utc="2026-06-25T11:00:00Z")
