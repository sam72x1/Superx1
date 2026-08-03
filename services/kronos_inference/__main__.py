"""نقطة تشغيل خدمة استدلال Kronos."""

from __future__ import annotations

import logging

from .config import ConfigError, ServiceConfig
from .engine import KronosEngine
from .http_server import serve
from .service import ForecastApplication


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = ServiceConfig.from_env()
    except ConfigError as exc:
        raise SystemExit(f"إعدادات خدمة Kronos غير صالحة: {exc}") from exc

    engine = KronosEngine(config)
    application = ForecastApplication(config, engine)
    serve(config, application)


if __name__ == "__main__":
    main()
