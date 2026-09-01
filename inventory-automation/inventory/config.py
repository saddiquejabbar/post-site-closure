from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigurationError(ValueError):
    pass


def _csv_ints(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise ConfigurationError(f"{name} is required")
    try:
        return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must contain comma-separated numeric IDs") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip().casefold() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().casefold() in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc


@dataclass(slots=True, frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    allowed_chat_ids: frozenset[int]
    allowed_user_ids: frozenset[int]
    approver_user_ids: frozenset[int]
    workbook_path: Path
    sku_aliases_path: Path
    state_db_path: Path
    lock_file_path: Path
    staging_dir: Path
    backup_dir: Path
    timezone: ZoneInfo
    callback_ttl_minutes: int = 30
    max_abs_quantity: int = 1000
    max_items_per_request: int = 50
    source_stability_seconds: float = 1.0
    post_write_stability_seconds: float = 0.5
    require_vba_project: bool = True
    write_enabled: bool = False
    log_sheet_name: str = "Log"
    header_row: int = 3
    data_start_row: int = 4
    data_end_row: int = 9956
    sku_start_column: str = "J"
    sku_end_column: str = "HF"
    status_column: str = "HG"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        workbook = os.getenv("INVENTORY_WORKBOOK_PATH", "").strip()
        if not token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required")
        if len(secret) < 16:
            raise ConfigurationError("TELEGRAM_WEBHOOK_SECRET must be at least 16 characters")
        if not workbook:
            raise ConfigurationError("INVENTORY_WORKBOOK_PATH is required")
        allowed_users = _csv_ints("ALLOWED_USER_IDS")
        approvers = _csv_ints("APPROVER_USER_IDS")
        if not approvers.issubset(allowed_users):
            raise ConfigurationError("APPROVER_USER_IDS must be a subset of ALLOWED_USER_IDS")
        try:
            timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Singapore").strip())
        except Exception as exc:
            raise ConfigurationError("Invalid TIMEZONE") from exc
        result = cls(
            telegram_bot_token=token,
            telegram_webhook_secret=secret,
            allowed_chat_ids=_csv_ints("ALLOWED_CHAT_IDS"),
            allowed_user_ids=allowed_users,
            approver_user_ids=approvers,
            workbook_path=Path(workbook).expanduser().resolve(),
            sku_aliases_path=Path(os.getenv("SKU_ALIASES_PATH", "./config/sku_aliases.example.json")).expanduser().resolve(),
            state_db_path=Path(os.getenv("STATE_DB_PATH", "./state/inventory.sqlite3")).expanduser().resolve(),
            lock_file_path=Path(os.getenv("LOCK_FILE_PATH", "./state/inventory-writer.lock")).expanduser().resolve(),
            staging_dir=Path(os.getenv("STAGING_DIR", "./state/staging")).expanduser().resolve(),
            backup_dir=Path(os.getenv("BACKUP_DIR", "./state/backups")).expanduser().resolve(),
            timezone=timezone,
            callback_ttl_minutes=_int("CALLBACK_TTL_MINUTES", 30),
            max_abs_quantity=_int("MAX_ABS_QUANTITY", 1000),
            max_items_per_request=_int("MAX_ITEMS_PER_REQUEST", 50),
            source_stability_seconds=_float("SOURCE_STABILITY_SECONDS", 1.0),
            post_write_stability_seconds=_float("POST_WRITE_STABILITY_SECONDS", 0.5),
            require_vba_project=_bool("REQUIRE_VBA_PROJECT", True),
            write_enabled=_bool("WRITE_ENABLED", False),
            log_sheet_name=os.getenv("LOG_SHEET_NAME", "Log").strip(),
            header_row=_int("HEADER_ROW", 3),
            data_start_row=_int("DATA_START_ROW", 4),
            data_end_row=_int("DATA_END_ROW", 9956),
            sku_start_column=os.getenv("SKU_START_COLUMN", "J").strip().upper(),
            sku_end_column=os.getenv("SKU_END_COLUMN", "HF").strip().upper(),
            status_column=os.getenv("STATUS_COLUMN", "HG").strip().upper(),
        )
        if result.data_start_row <= result.header_row or result.data_end_row < result.data_start_row:
            raise ConfigurationError("Invalid workbook row bounds")
        if min(result.callback_ttl_minutes, result.max_abs_quantity, result.max_items_per_request) < 1:
            raise ConfigurationError("Limits must be positive")
        return result
