"""تحقق Bearer مستقل وقابل للاختبار."""

from __future__ import annotations

from hmac import compare_digest
from typing import Mapping, Protocol


class HeadersWithDuplicates(Protocol):
    def get_all(self, name: str) -> list[str] | None: ...


def _authorization_values(headers: Mapping[str, str] | HeadersWithDuplicates) -> list[str]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return list(get_all("Authorization") or [])

    return [
        value
        for name, value in headers.items()
        if name.casefold() == "authorization"
    ]


def is_authorized(
    headers: Mapping[str, str] | HeadersWithDuplicates,
    expected_token: str | None,
) -> bool:
    """يفرض Bearer فقط عندما يُضبط السر في البيئة."""

    if expected_token is None:
        return True
    values = _authorization_values(headers)
    if len(values) != 1:
        return False
    expected = f"Bearer {expected_token}"
    supplied = values[0]
    return isinstance(supplied, str) and compare_digest(supplied, expected)
