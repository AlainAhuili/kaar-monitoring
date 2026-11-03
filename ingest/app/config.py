from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"missing environment variable {name}")
    return val


@dataclass(frozen=True)
class Config:
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str
    mqtt_prefix: str
    database_url: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    telegram_bot_token: str
    batch_size: int
    flush_interval_s: float
    rules_reload_s: float
    offline_check_s: float


def load() -> Config:
    return Config(
        mqtt_host=_env("MQTT_HOST", "mosquitto"),
        mqtt_port=int(_env("MQTT_PORT", "1883")),
        mqtt_user=_env("MQTT_USER", "ingest"),
        mqtt_password=_env("MQTT_PASSWORD"),
        mqtt_prefix=_env("MQTT_PREFIX", "kaar"),
        database_url=_env("DATABASE_URL"),
        smtp_host=_env("SMTP_HOST", ""),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER", ""),
        smtp_password=_env("SMTP_PASSWORD", ""),
        smtp_from=_env("SMTP_FROM", "alerts@example.com"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", ""),
        batch_size=int(_env("BATCH_SIZE", "500")),
        flush_interval_s=float(_env("FLUSH_INTERVAL_S", "1")),
        rules_reload_s=float(_env("RULES_RELOAD_S", "60")),
        offline_check_s=float(_env("OFFLINE_CHECK_S", "60")),
    )
