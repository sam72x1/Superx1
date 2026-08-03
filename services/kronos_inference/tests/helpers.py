"""مصانع بيانات اختبار صالحة."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def valid_payload(*, lookback: int = 32, pred_len: int = 3) -> dict[str, object]:
    start = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    bars: list[dict[str, object]] = []
    for index in range(lookback):
        price = 10.0 + (index * 0.01)
        bars.append(
            {
                "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.1,
                "close": price + 0.1,
                "volume": 1_000 + index,
                "amount": (1_000 + index) * price,
            }
        )
    last = start + timedelta(minutes=5 * (lookback - 1))
    future = [
        (last + timedelta(minutes=5 * (index + 1))).isoformat()
        for index in range(pred_len)
    ]
    return {
        "ticker": "TEST",
        "bars": bars,
        "future_timestamps": future,
        "horizons": list(range(1, pred_len + 1)),
    }
