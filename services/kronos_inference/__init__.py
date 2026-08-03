"""خدمة استدلال Kronos المعزولة عن عامل الماسح الرئيسي."""

from .config import ServiceConfig
from .service import ForecastApplication

__all__ = ["ForecastApplication", "ServiceConfig"]
