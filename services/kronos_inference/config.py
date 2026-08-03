"""إعدادات الخدمة المقروءة من متغيرات البيئة."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from os import environ as process_environ
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_MODEL_ID = "NeoQuasar/Kronos-small"
DEFAULT_MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
DEFAULT_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
DEFAULT_TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
DEFAULT_KRONOS_SOURCE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"


class ConfigError(ValueError):
    """يشير إلى إعداد بيئة مفقود أو غير صالح."""


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _read_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} يجب أن يكون عددًا صحيحًا") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{name} يجب أن يكون بين {minimum} و{maximum}")
    return parsed


def _read_text(
    values: Mapping[str, str],
    name: str,
    default: str,
    *,
    maximum_length: int = 256,
) -> str:
    value = values.get(name, default)
    if not value or value != value.strip():
        raise ConfigError(f"{name} لا يجوز أن يكون فارغًا أو محاطًا بمسافات")
    if len(value) > maximum_length or any(char in value for char in "\r\n\0"):
        raise ConfigError(f"{name} يحتوي قيمة غير صالحة")
    return value


@dataclass(frozen=True)
class ServiceConfig:
    """إعدادات تشغيل محدودة تمنع طلبًا واحدًا من استنزاف الخدمة."""

    host: str = "127.0.0.1"
    port: int = 8080
    api_token: str | None = None
    max_body_bytes: int = 1_048_576
    request_timeout_seconds: int = 15
    max_request_threads: int = 16
    min_lookback: int = 32
    max_context: int = 512
    max_pred_len: int = 120
    max_horizons: int = 32
    kronos_repo_path: str | None = None
    kronos_source_revision: str = DEFAULT_KRONOS_SOURCE_REVISION
    model_id: str = DEFAULT_MODEL_ID
    model_revision: str = DEFAULT_MODEL_REVISION
    tokenizer_id: str = DEFAULT_TOKENIZER_ID
    tokenizer_revision: str = DEFAULT_TOKENIZER_REVISION
    device: str | None = None
    market_timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        if self.api_token is not None and (
            not self.api_token
            or self.api_token != self.api_token.strip()
            or any(char.isspace() for char in self.api_token)
        ):
            raise ConfigError("KRONOS_API_TOKEN غير صالح")
        if not self.api_token and not _is_loopback_host(self.host):
            raise ConfigError("ضبط KRONOS_API_TOKEN إلزامي عند الربط بعنوان غير loopback")
        try:
            ZoneInfo(self.market_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError("KRONOS_MARKET_TIMEZONE غير صالح") from exc
        for name, revision in (
            ("KRONOS_SOURCE_REVISION", self.kronos_source_revision),
            ("KRONOS_MODEL_REVISION", self.model_revision),
            ("KRONOS_TOKENIZER_REVISION", self.tokenizer_revision),
        ):
            if len(revision) != 40 or any(
                character not in "0123456789abcdefABCDEF" for character in revision
            ):
                raise ConfigError(
                    f"{name} يجب أن يكون commit SHA ثابتًا من 40 خانة hex"
                )

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "ServiceConfig":
        source = process_environ if values is None else values
        token = source.get("KRONOS_API_TOKEN") or None
        if token is not None:
            if token != token.strip() or any(char.isspace() for char in token):
                raise ConfigError("KRONOS_API_TOKEN لا يجوز أن يحتوي مسافات")
            if len(token) > 4096:
                raise ConfigError("KRONOS_API_TOKEN أطول من الحد المسموح")

        min_lookback = _read_int(source, "KRONOS_MIN_LOOKBACK", 32, 2, 512)
        max_context = _read_int(source, "KRONOS_MAX_CONTEXT", 512, 2, 512)
        if min_lookback > max_context:
            raise ConfigError("KRONOS_MIN_LOOKBACK يجب ألا يتجاوز KRONOS_MAX_CONTEXT")

        repo_path = source.get("KRONOS_REPO_PATH") or None
        if repo_path is not None:
            if repo_path != repo_path.strip() or "\0" in repo_path:
                raise ConfigError("KRONOS_REPO_PATH غير صالح")

        device = source.get("KRONOS_DEVICE") or None
        if device is not None:
            device = _read_text(source, "KRONOS_DEVICE", "cpu", maximum_length=64)

        return cls(
            host=_read_text(source, "KRONOS_HOST", "127.0.0.1", maximum_length=255),
            port=_read_int(source, "KRONOS_PORT", 8080, 1, 65_535),
            api_token=token,
            max_body_bytes=_read_int(
                source,
                "KRONOS_MAX_BODY_BYTES",
                1_048_576,
                1_024,
                10_485_760,
            ),
            request_timeout_seconds=_read_int(
                source,
                "KRONOS_REQUEST_TIMEOUT_SECONDS",
                15,
                1,
                120,
            ),
            max_request_threads=_read_int(
                source,
                "KRONOS_MAX_REQUEST_THREADS",
                16,
                1,
                128,
            ),
            min_lookback=min_lookback,
            max_context=max_context,
            max_pred_len=_read_int(source, "KRONOS_MAX_PRED_LEN", 120, 1, 120),
            max_horizons=_read_int(source, "KRONOS_MAX_HORIZONS", 32, 1, 120),
            kronos_repo_path=repo_path,
            kronos_source_revision=_read_text(
                source,
                "KRONOS_SOURCE_REVISION",
                DEFAULT_KRONOS_SOURCE_REVISION,
                maximum_length=128,
            ),
            model_id=_read_text(source, "KRONOS_MODEL_ID", DEFAULT_MODEL_ID),
            model_revision=_read_text(
                source,
                "KRONOS_MODEL_REVISION",
                DEFAULT_MODEL_REVISION,
            ),
            tokenizer_id=_read_text(source, "KRONOS_TOKENIZER_ID", DEFAULT_TOKENIZER_ID),
            tokenizer_revision=_read_text(
                source,
                "KRONOS_TOKENIZER_REVISION",
                DEFAULT_TOKENIZER_REVISION,
            ),
            device=device,
            market_timezone=_read_text(
                source,
                "KRONOS_MARKET_TIMEZONE",
                "America/New_York",
                maximum_length=128,
            ),
        )
