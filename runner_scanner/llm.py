"""عميل Claude رفيع (Anthropic Messages API) — يخدم المحلّل والمستشار والمساعد.

best-effort: بدون مفتاح أو عند أي فشل يرجّع None (لا يُسقط البوت).
يدعم مخرجًا منظّمًا عبر «أداة» (tool use) ومخرجًا نصّيًا حرًّا.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class ClaudeClient:
    """غلاف بسيط لـ Anthropic Messages API."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def structured(self, model: str, system: str, prompt: str, tool: dict,
                   max_tokens: int = 700) -> Optional[dict]:
        """يرجّع input الأداة (dict منظّم) أو None. tool = تعريف أداة كامل."""
        if not self.available:
            return None
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = requests.post(_API_URL, headers=self._headers(), json=body,
                                 timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Claude (structured) رفض %s: %s",
                               resp.status_code, resp.text[:200])
                return None
            for block in resp.json().get("content", []):
                if block.get("type") == "tool_use":
                    return block.get("input") or {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Claude (structured) فشل: %s", exc)
        return None

    def chat(self, model: str, system: str, prompt: str,
             max_tokens: int = 900) -> Optional[str]:
        """يرجّع نصًّا حرًّا (للبريفنغ والمساعد) أو None."""
        if not self.available:
            return None
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = requests.post(_API_URL, headers=self._headers(), json=body,
                                 timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Claude (chat) رفض %s: %s", resp.status_code,
                               resp.text[:200])
                return None
            parts = [b.get("text", "") for b in resp.json().get("content", [])
                     if b.get("type") == "text"]
            return "".join(parts).strip() or None
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Claude (chat) فشل: %s", exc)
            return None
