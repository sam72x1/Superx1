"""عقد JSON والتحقق الصارم قبل وصول البيانات إلى النموذج."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import ServiceConfig


_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}\Z")
_TOP_LEVEL_FIELDS = {"ticker", "bars", "future_timestamps", "horizons"}
_BAR_REQUIRED_FIELDS = {"timestamp", "open", "high", "low", "close", "volume"}
_BAR_OPTIONAL_FIELDS = {"amount"}
_MAX_PRICE = 1_000_000_000.0
_MAX_VOLUME = 1_000_000_000_000_000_000.0
_MAX_AMOUNT = 1_000_000_000_000_000_000_000_000.0
_FORECAST_INTERVAL = timedelta(minutes=5)


class RequestValidationError(ValueError):
    """خطأ آمن يمكن عرضه لعميل الـAPI."""

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    model_timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None


@dataclass(frozen=True)
class FutureTimestamp:
    timestamp: datetime
    model_timestamp: datetime


@dataclass(frozen=True)
class ForecastRequest:
    ticker: str
    bars: tuple[Bar, ...]
    future_timestamps: tuple[FutureTimestamp, ...]
    horizons: tuple[int, ...]

    @property
    def lookback(self) -> int:
        return len(self.bars)

    @property
    def pred_len(self) -> int:
        return len(self.future_timestamps)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestValidationError(f"المفتاح {key!r} مكرر", "invalid_json")
        result[key] = value
    return result


def _reject_nonfinite_literal(value: str) -> None:
    raise RequestValidationError(f"القيمة {value} ليست رقم JSON صالحًا", "invalid_json")


def parse_json_body(raw_body: bytes) -> Any:
    try:
        text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RequestValidationError("جسم الطلب ليس UTF-8 صالحًا", "invalid_json") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_literal,
        )
    except RequestValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RequestValidationError("جسم الطلب ليس JSON صالحًا", "invalid_json") from exc


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestValidationError(f"{location} يجب أن يكون كائن JSON")
    return value


def _strict_fields(
    value: dict[str, Any],
    required: set[str],
    optional: set[str],
    location: str,
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise RequestValidationError(f"{location} ينقصه: {', '.join(missing)}")
    if unknown:
        raise RequestValidationError(f"{location} يحتوي حقولًا غير معروفة: {', '.join(unknown)}")


def _number(
    value: Any,
    location: str,
    *,
    minimum: float,
    maximum: float,
    minimum_inclusive: bool = True,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestValidationError(f"{location} يجب أن يكون رقمًا")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestValidationError(f"{location} يجب أن يكون رقمًا محدودًا") from exc
    lower_valid = parsed >= minimum if minimum_inclusive else parsed > minimum
    if not math.isfinite(parsed) or not lower_valid or parsed > maximum:
        comparator = "على الأقل" if minimum_inclusive else "أكبر من"
        raise RequestValidationError(
            f"{location} يجب أن يكون رقمًا محدودًا {comparator} {minimum} وحتى {maximum}"
        )
    return parsed


def _timestamp(
    value: Any,
    location: str,
    market_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise RequestValidationError(f"{location} يجب أن يكون طابع ISO 8601 نصيًا")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RequestValidationError(f"{location} ليس طابع ISO 8601 صالحًا") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestValidationError(f"{location} يجب أن يحتوي إزاحة زمنية مثل Z أو -04:00")

    # ─ يحافظ Kronos على ساعة نيويورك المحلية في الترميز الزمني، بينما تبقى
    #   النسخة الواعية بالمنطقة للمقارنة الآمنة بين اللحظات. هذا مهم لأن عميل
    #   الماسح يرسل UTC، وتحويل 13:30Z إلى 09:30 يطابق افتتاح السوق الصيفي.
    model_timestamp = parsed.astimezone(market_timezone).replace(tzinfo=None)
    return parsed, model_timestamp


def _bar(value: Any, index: int, market_timezone: ZoneInfo) -> Bar:
    location = f"bars[{index}]"
    item = _require_object(value, location)
    _strict_fields(item, _BAR_REQUIRED_FIELDS, _BAR_OPTIONAL_FIELDS, location)
    timestamp, model_timestamp = _timestamp(
        item["timestamp"],
        f"{location}.timestamp",
        market_timezone,
    )
    open_price = _number(
        item["open"], f"{location}.open", minimum=0.0, maximum=_MAX_PRICE, minimum_inclusive=False
    )
    high = _number(
        item["high"], f"{location}.high", minimum=0.0, maximum=_MAX_PRICE, minimum_inclusive=False
    )
    low = _number(
        item["low"], f"{location}.low", minimum=0.0, maximum=_MAX_PRICE, minimum_inclusive=False
    )
    close = _number(
        item["close"], f"{location}.close", minimum=0.0, maximum=_MAX_PRICE, minimum_inclusive=False
    )
    volume = _number(item["volume"], f"{location}.volume", minimum=0.0, maximum=_MAX_VOLUME)
    amount = None
    if "amount" in item:
        amount = _number(item["amount"], f"{location}.amount", minimum=0.0, maximum=_MAX_AMOUNT)

    tolerance = max(high, open_price, low, close) * 1e-12
    if high + tolerance < max(open_price, low, close):
        raise RequestValidationError(f"{location}.high أقل من إحدى قيم OHLC")
    if low - tolerance > min(open_price, high, close):
        raise RequestValidationError(f"{location}.low أعلى من إحدى قيم OHLC")

    return Bar(timestamp, model_timestamp, open_price, high, low, close, volume, amount)


def validate_forecast_request(payload: Any, config: ServiceConfig) -> ForecastRequest:
    document = _require_object(payload, "الطلب")
    _strict_fields(document, _TOP_LEVEL_FIELDS, set(), "الطلب")
    market_timezone = ZoneInfo(config.market_timezone)

    ticker = document["ticker"]
    if not isinstance(ticker, str) or _TICKER_RE.fullmatch(ticker) is None:
        raise RequestValidationError("ticker يجب أن يكون رمزًا كبير الأحرف صالحًا بطول 1–15")

    bars_value = document["bars"]
    if not isinstance(bars_value, list):
        raise RequestValidationError("bars يجب أن تكون قائمة")
    if not config.min_lookback <= len(bars_value) <= config.max_context:
        raise RequestValidationError(
            f"عدد bars يجب أن يكون بين {config.min_lookback} و{config.max_context}"
        )
    bars = tuple(
        _bar(value, index, market_timezone)
        for index, value in enumerate(bars_value)
    )
    for previous, current in zip(bars, bars[1:]):
        if current.timestamp <= previous.timestamp:
            raise RequestValidationError("timestamps داخل bars يجب أن تكون متزايدة بلا تكرار")

    future_value = document["future_timestamps"]
    if not isinstance(future_value, list):
        raise RequestValidationError("future_timestamps يجب أن تكون قائمة")
    if not 1 <= len(future_value) <= config.max_pred_len:
        raise RequestValidationError(
            f"عدد future_timestamps يجب أن يكون بين 1 و{config.max_pred_len}"
        )
    future = tuple(
        FutureTimestamp(
            *_timestamp(
                value,
                f"future_timestamps[{index}]",
                market_timezone,
            )
        )
        for index, value in enumerate(future_value)
    )
    if future[0].timestamp != bars[-1].timestamp + _FORECAST_INTERVAL:
        raise RequestValidationError(
            "أول future_timestamp يجب أن يلي آخر bar بخمس دقائق"
        )
    for previous, current in zip(future, future[1:]):
        if current.timestamp != previous.timestamp + _FORECAST_INTERVAL:
            raise RequestValidationError(
                "future_timestamps يجب أن تتتابع بفاصل خمس دقائق"
            )

    horizons_value = document["horizons"]
    if not isinstance(horizons_value, list) or not horizons_value:
        raise RequestValidationError("horizons يجب أن تكون قائمة غير فارغة")
    if len(horizons_value) > config.max_horizons:
        raise RequestValidationError(f"عدد horizons يتجاوز الحد {config.max_horizons}")
    horizons: list[int] = []
    for index, value in enumerate(horizons_value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RequestValidationError(f"horizons[{index}] يجب أن يكون عددًا صحيحًا")
        if not 1 <= value <= len(future):
            raise RequestValidationError(
                f"horizons[{index}] يجب أن يكون بين 1 وpred_len={len(future)}"
            )
        horizons.append(value)
    if horizons != sorted(set(horizons)):
        raise RequestValidationError("horizons يجب أن تكون مرتبة تصاعديًا وبلا تكرار")

    return ForecastRequest(ticker, bars, future, tuple(horizons))
