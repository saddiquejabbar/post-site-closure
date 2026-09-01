from __future__ import annotations

from inventory_store import RequestStore
from inventory_types import (
    Catalog,
    InventoryParser,
    ParsedItem,
    ParsedRequest,
    Product,
    Settings,
)
from safe_xlsm import Mapping, SafeXlsmWriter
from telegram_service import InventoryService, TelegramClient, render_review


def build_service(settings: Settings) -> InventoryService:
    parser = InventoryParser(Catalog.load(settings.catalog_path))
    store = RequestStore(settings.db_path)
    telegram = TelegramClient(settings.bot_token)
    writer = SafeXlsmWriter(
        settings.workbook_path,
        Mapping.load(settings.mapping_path),
        settings.backup_dir,
        settings.lock_timeout,
    )
    return InventoryService(settings, parser, store, telegram, writer)


__all__ = [
    "Catalog",
    "InventoryParser",
    "InventoryService",
    "ParsedItem",
    "ParsedRequest",
    "Product",
    "RequestStore",
    "Settings",
    "TelegramClient",
    "build_service",
    "render_review",
]
