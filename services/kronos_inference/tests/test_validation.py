from __future__ import annotations

import json
import unittest

from services.kronos_inference.config import ServiceConfig
from services.kronos_inference.tests.helpers import valid_payload
from services.kronos_inference.validation import (
    RequestValidationError,
    parse_json_body,
    validate_forecast_request,
)


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ServiceConfig()

    def test_valid_request_derives_lengths(self) -> None:
        request = validate_forecast_request(valid_payload(), self.config)
        self.assertEqual(request.lookback, 32)
        self.assertEqual(request.pred_len, 3)
        self.assertEqual(request.horizons, (1, 2, 3))
        self.assertEqual(request.bars[0].model_timestamp.hour, 9)
        self.assertEqual(request.bars[0].model_timestamp.minute, 30)

    def test_duplicate_json_key_and_nonfinite_literal_are_rejected(self) -> None:
        with self.assertRaises(RequestValidationError) as duplicate:
            parse_json_body(b'{"ticker":"A","ticker":"B"}')
        self.assertEqual(duplicate.exception.code, "invalid_json")
        with self.assertRaises(RequestValidationError):
            parse_json_body(b'{"value":NaN}')

    def test_unknown_field_is_rejected(self) -> None:
        payload = valid_payload()
        payload["pred_len"] = 3
        with self.assertRaisesRegex(RequestValidationError, "غير معروفة"):
            validate_forecast_request(payload, self.config)

    def test_ohlc_invariant_is_enforced(self) -> None:
        payload = valid_payload()
        payload["bars"][0]["high"] = 1.0  # type: ignore[index]
        with self.assertRaisesRegex(RequestValidationError, "high"):
            validate_forecast_request(payload, self.config)

    def test_integer_too_large_for_float_is_rejected_cleanly(self) -> None:
        payload = valid_payload()
        payload["bars"][0]["open"] = 10**400  # type: ignore[index]
        with self.assertRaisesRegex(RequestValidationError, "محدود"):
            validate_forecast_request(payload, self.config)

    def test_timestamps_require_timezone_and_strict_order(self) -> None:
        payload = valid_payload()
        payload["bars"][0]["timestamp"] = "2026-08-03T13:30:00"  # type: ignore[index]
        with self.assertRaisesRegex(RequestValidationError, "إزاحة زمنية"):
            validate_forecast_request(payload, self.config)

        payload = valid_payload()
        payload["bars"][1]["timestamp"] = payload["bars"][0]["timestamp"]  # type: ignore[index]
        with self.assertRaisesRegex(RequestValidationError, "متزايدة"):
            validate_forecast_request(payload, self.config)

    def test_future_timestamps_must_follow_exact_five_minute_cadence(self) -> None:
        payload = valid_payload()
        payload["future_timestamps"][0] = "2026-08-03T16:15:00Z"
        with self.assertRaisesRegex(RequestValidationError, "خمس دقائق"):
            validate_forecast_request(payload, self.config)

        payload = valid_payload()
        payload["future_timestamps"][1] = "2026-08-03T16:16:00Z"
        with self.assertRaisesRegex(RequestValidationError, "خمس دقائق"):
            validate_forecast_request(payload, self.config)

    def test_lookback_and_pred_len_limits_are_enforced(self) -> None:
        payload = valid_payload(lookback=31)
        with self.assertRaisesRegex(RequestValidationError, "عدد bars"):
            validate_forecast_request(payload, self.config)

        payload = valid_payload(pred_len=3)
        payload["future_timestamps"] = []
        with self.assertRaisesRegex(RequestValidationError, "future_timestamps"):
            validate_forecast_request(payload, self.config)

    def test_horizons_must_be_sorted_unique_and_within_pred_len(self) -> None:
        payload = valid_payload()
        payload["horizons"] = [2, 1]
        with self.assertRaisesRegex(RequestValidationError, "مرتبة"):
            validate_forecast_request(payload, self.config)

        payload = valid_payload()
        payload["horizons"] = [4]
        with self.assertRaisesRegex(RequestValidationError, "pred_len"):
            validate_forecast_request(payload, self.config)

    def test_json_round_trip_uses_only_supported_contract(self) -> None:
        payload = valid_payload()
        parsed = parse_json_body(json.dumps(payload).encode("utf-8"))
        request = validate_forecast_request(parsed, self.config)
        self.assertEqual(request.ticker, "TEST")


if __name__ == "__main__":
    unittest.main()
