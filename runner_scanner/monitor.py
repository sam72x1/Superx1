"""مراقبة صحّة البوت — صامت عند الصحة، 🚨 فقط عند عطل حقيقي (القسم 1).

أعطال حقيقية: مفتاح API، تيليجرام، القرص، أو توقّف المسح (لا دورة ناجحة
منذ مدة). نرسل تنبيه 🚨 عالٍ مرة واحدة لكل عطل (لا إزعاج متكرّر).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .textutil import esc

logger = logging.getLogger(__name__)


class HealthMonitor:
    """يتتبّع آخر دورة مسح ناجحة ويصدر تنبيهات أعطال مزيلة للتكرار."""

    def __init__(self, notify: Callable[[str], bool],
                 stall_seconds: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self._notify = notify
        self.stall_seconds = stall_seconds
        self._clock = clock
        self._last_ok = clock()
        self._active_faults: set[str] = set()

    def heartbeat(self) -> None:
        """تُستدعى بعد كل دورة مسح ناجحة."""
        self._last_ok = self._clock()
        # تعافى المسح → نظّف عطل التوقّف
        self._clear_fault("scan_stall")

    def raise_fault(self, key: str, message: str) -> None:
        """يصدر تنبيه عطل مرة واحدة (حتى يُحل ثم يتكرّر)."""
        if key in self._active_faults:
            return
        self._active_faults.add(key)
        logger.error("🚨 عطل [%s]: %s", key, message)
        # §5: message نصّ خارجي (جسم استجابة المزوّد الخام — قد يكون HTML وقت
        # عطل 4xx/5xx) → يُهرَّب، وإلا < واحد يُسقط بـ400 الرسالةَ الوحيدة
        # المصمَّمة لإخبارك أن البوت عمي، وهي مزيلة التكرار فلا تُعاد (BUG-05).
        self._notify(
            f"🚨 <b>عطل في الماسح</b>\n[{esc(key)}] {esc(message)}")

    def _clear_fault(self, key: str) -> None:
        if key in self._active_faults:
            self._active_faults.discard(key)
            logger.info("تعافى العطل [%s]", key)

    def clear_fault(self, key: str) -> None:
        self._clear_fault(key)

    def active_faults(self) -> list[str]:
        """أسماء الأعطال النشطة حاليًا (للبريفنغ)."""
        return sorted(self._active_faults)

    def check_stall(self) -> None:
        """يفحص توقّف المسح (يُستدعى دوريًا)."""
        if self._clock() - self._last_ok > self.stall_seconds:
            self.raise_fault(
                "scan_stall",
                f"لا دورة مسح ناجحة منذ {self.stall_seconds:.0f}ث")
