"""تكامل Render API — وعي بحالة الخدمة وآخر نشر، وإعادة تشغيل بإذن.

يُستخدم في: بريفنغ المستشار · أمر /status · أمر /restart.
best-effort: بدون RENDER_API_KEY/RENDER_SERVICE_ID يرجّع قيمًا فارغة.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .config import Config
from .textutil import esc

logger = logging.getLogger(__name__)

_BASE = "https://api.render.com/v1"


class RenderClient:
    def __init__(self, cfg: Config, timeout: float = 15.0):
        self.cfg = cfg
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.cfg.render_api_key and self.cfg.render_service_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.render_api_key}",
                "Accept": "application/json"}

    def _get(self, path: str) -> Optional[object]:
        if not self.available:
            return None
        try:
            resp = requests.get(f"{_BASE}{path}", headers=self._headers(),
                                timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Render رفض %s: %s", resp.status_code,
                               resp.text[:150])
                return None
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Render فشل: %s", exc)
            return None

    def service_status(self) -> dict:
        """حالة الخدمة (الاسم، suspended...). {} عند الفشل."""
        sid = self.cfg.render_service_id
        data = self._get(f"/services/{sid}")
        return data if isinstance(data, dict) else {}

    def latest_deploy(self) -> dict:
        """آخر نشر: {id, status, commit_id, commit_message, created}. {} عند الفشل."""
        sid = self.cfg.render_service_id
        data = self._get(f"/services/{sid}/deploys?limit=1")
        if not isinstance(data, list) or not data:
            return {}
        d = (data[0] or {}).get("deploy") or {}
        commit = d.get("commit") or {}
        return {
            "id": d.get("id", ""),
            "status": d.get("status", ""),
            "commit_id": (commit.get("id") or "")[:7],
            "commit_message": (commit.get("message") or "").splitlines()[0][:80]
            if commit.get("message") else "",
            "created": d.get("createdAt", ""),
            "finished": d.get("finishedAt", ""),
        }

    def restart(self) -> bool:
        """يعيد تشغيل الخدمة (بإذن المستخدم فقط)."""
        if not self.available:
            return False
        sid = self.cfg.render_service_id
        try:
            resp = requests.post(f"{_BASE}/services/{sid}/restart",
                                 headers=self._headers(), timeout=self.timeout)
            return resp.status_code in (200, 201, 202)
        except requests.RequestException as exc:
            logger.warning("Render restart فشل: %s", exc)
            return False

    def summary(self) -> str:
        """سطر/سطرين عن حالة الخدمة وآخر نشر (للبريفنغ و/status)."""
        if not self.available:
            return "Render: غير مربوط (لا API key)"
        svc = self.service_status()
        dep = self.latest_deploy()
        # اسم الخدمة ورسالة الـ commit نصّان خارجيان من Render API → يُهرَّبان
        name = esc(svc.get("name", self.cfg.render_service_id))
        suspended = svc.get("suspended", "")
        state = "موقوفة ⚠️" if suspended == "suspended" else "شغّالة ✅"
        out = f"Render «{name}»: {state}"
        if dep:
            out += (f" · آخر نشر {dep['commit_id']} ({dep['status']})"
                    f" {esc(dep['commit_message'])}")
        return out
