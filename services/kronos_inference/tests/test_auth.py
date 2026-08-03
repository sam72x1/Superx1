from __future__ import annotations

import unittest

from services.kronos_inference.auth import is_authorized


class DuplicateHeaders:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    def get_all(self, name: str) -> list[str]:
        return self.values if name.casefold() == "authorization" else []


class AuthorizationTests(unittest.TestCase):
    def test_auth_is_disabled_when_token_is_unset(self) -> None:
        self.assertTrue(is_authorized({}, None))

    def test_exact_bearer_token_is_accepted_case_insensitively_by_header_name(self) -> None:
        self.assertTrue(is_authorized({"authorization": "Bearer secret"}, "secret"))

    def test_wrong_scheme_or_token_is_rejected(self) -> None:
        self.assertFalse(is_authorized({"Authorization": "Basic secret"}, "secret"))
        self.assertFalse(is_authorized({"Authorization": "Bearer wrong"}, "secret"))
        self.assertFalse(is_authorized({}, "secret"))

    def test_duplicate_authorization_headers_are_rejected(self) -> None:
        headers = DuplicateHeaders(["Bearer secret", "Bearer secret"])
        self.assertFalse(is_authorized(headers, "secret"))


if __name__ == "__main__":
    unittest.main()
