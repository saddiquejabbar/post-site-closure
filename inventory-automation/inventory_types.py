from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    aliases: tuple[str, ...]
    active: bool = True


class Catalog:
    def __init__(self, products: Iterable[Product]) -> None:
        self.aliases: dict[str, list[Product]] = {}
        for product in products:
            if not product.active:
                continue
            for alias in {product.sku, product.name, *product.aliases}:
                key = normalize(alias)
                if key:
                    self.aliases.setdefault(key, []).append(product)
        if not self.aliases:
            raise ValueError("catalog has no active products")

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        source = raw["products"] if isinstance(raw, dict) else raw
        return cls(
            Product(
                str(item["sku"]).strip(),
                str(item["name"]).strip(),
                tuple(str(value).strip() for value in item.get("aliases", [])),
                bool(item.get("active", True)),
            )
            for item in source
        )

    def match(self, text: str) -> list[Product]:
        query = normalize(text)
        matches = self.aliases.get(query, [])
        if not matches:
            aliases = [alias for alias in self.aliases if alias in query or query in alias]
            if aliases:
                longest = max(map(len, aliases))
                matches = [
                    product
                    for alias in aliases
                    if len(alias) == longest
                    for product in self.aliases[alias]
                ]
        seen: set[str] = set()
        result: list[Product] = []
        for product in matches:
            if product.sku not in seen:
                seen.add(product.sku)
                result.append(product)
        return result


@dataclass
class ParsedItem:
    raw: str
    quantity: float | None
    sku: str | None = None
    name: str | None = None
    candidates: list[dict[str, str]] = field(default_factory=list)
    issue: str | None = None


@dataclass
class ParsedRequest:
    customer: str | None
    site: str | None
    installer: str | None
    job_ref: str | None
    items: list[ParsedItem]
    missing_fields: list[str]
    parser_version: str = "deterministic-v1"

    @property
    def ready(self) -> bool:
        return bool(self.items) and not self.missing_fields and all(
            item.quantity and item.sku and not item.issue for item in self.items
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer": self.customer,
            "site": self.site,
            "installer": self.installer,
            "job_ref": self.job_ref,
            "items": [asdict(item) for item in self.items],
            "missing_fields": self.missing_fields,
            "parser_version": self.parser_version,
            "ready": self.ready,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParsedRequest":
        return cls(
            value.get("customer"),
            value.get("site"),
            value.get("installer"),
            value.get("job_ref"),
            [ParsedItem(**item) for item in value.get("items", [])],
            list(value.get("missing_fields", [])),
            value.get("parser_version", "deterministic-v1"),
        )


class InventoryParser:
    """Deterministic parser: unresolved input is flagged, never invented."""

    PREFIX = re.compile(r"^\s*(?:checkout|check\s*out|issue|take)\s+", re.I)

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def parse(self, text: str) -> ParsedRequest:
        text = " ".join(text.replace("\n", " ").split())
        fields, item_text = self._fields(text)
        items = self._items(item_text)
        missing = [
            name
            for name in ("customer", "site", "installer", "job_ref")
            if not fields.get(name)
        ]
        return ParsedRequest(
            fields.get("customer"),
            fields.get("site"),
            fields.get("installer"),
            fields.get("job_ref"),
            items,
            missing,
        )

    def _fields(self, text: str) -> tuple[dict[str, str | None], str]:
        fields: dict[str, str | None] = {
            "customer": None,
            "site": None,
            "installer": None,
            "job_ref": None,
        }
        parts = [part.strip() for part in text.split("|") if part.strip()]
        if len(parts) > 1:
            items = self.PREFIX.sub("", parts[0]).strip()
            pattern = r"^(customer|client|site|installer|staff|job|job\s*ref|ref|reference)\s*[:=]?\s*(.+)$"
            for part in parts[1:]:
                match = re.match(pattern, part, re.I)
                if not match:
                    continue
                key = match.group(1).casefold().replace(" ", "")
                value = _clean(match.group(2))
                if key in {"customer", "client"}:
                    fields["customer"] = value
                elif key == "site":
                    fields["site"] = value
                elif key in {"installer", "staff"}:
                    fields["installer"] = value
                else:
                    fields["job_ref"] = value
            return fields, items

        fields["installer"] = _find(
            text, r"\b(?:installer|staff)\s*[:=]?\s*([^.;|]+)"
        )
        fields["job_ref"] = _find(
            text,
            r"\b(?:job(?:\s*ref)?|ref(?:erence)?)\s*[:=#]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)",
        )
        main = re.split(
            r"[.;]\s*(?=(?:installer|staff|job(?:\s*ref)?|ref(?:erence)?)\b)",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
        main = self.PREFIX.sub("", main).strip(" .;")
        match = re.match(
            r"^(?P<items>.+?)\s+for\s+(?P<customer>.+?)\s+at\s+(?P<site>.+?)$",
            main,
            re.I,
        )
        if match:
            fields["customer"] = _clean(match.group("customer"))
            fields["site"] = _clean(match.group("site"))
            return fields, match.group("items")
        match = re.match(
            r"^(?P<items>.+?)\s+for\s+(?P<customer>.+?)$", main, re.I
        )
        if match:
            fields["customer"] = _clean(match.group("customer"))
            return fields, match.group("items")
        return fields, main

    def _items(self, text: str) -> list[ParsedItem]:
        parts = [
            part.strip(" .")
            for part in re.split(r"\s*(?:,|;|\band\b|\+)\s*", text, flags=re.I)
            if part.strip(" .")
        ]
        result: list[ParsedItem] = []
        for raw in parts:
            match = re.match(
                r"^\s*(\d+(?:\.\d+)?)\s*(?:x|×)?\s*(.+?)\s*$", raw, re.I
            )
            if not match or float(match.group(1)) <= 0:
                result.append(ParsedItem(raw, None, issue="missing_quantity"))
                continue
            quantity, description = float(match.group(1)), match.group(2)
            candidates = self.catalog.match(description)
            if len(candidates) == 1:
                product = candidates[0]
                result.append(ParsedItem(raw, quantity, product.sku, product.name))
            elif candidates:
                result.append(
                    ParsedItem(
                        raw,
                        quantity,
                        candidates=[
                            {"sku": product.sku, "name": product.name}
                            for product in candidates
                        ],
                        issue="ambiguous_sku",
                    )
                )
            else:
                result.append(ParsedItem(raw, quantity, issue="unknown_item"))
        return result


@dataclass(frozen=True)
class Settings:
    bot_token: str
    webhook_secret: str
    approver_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    catalog_path: Path
    workbook_path: Path
    mapping_path: Path
    backup_dir: Path
    db_path: Path
    timezone_name: str
    enable_writes: bool
    admin_token: str
    lock_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            required("TELEGRAM_BOT_TOKEN"),
            required("TELEGRAM_WEBHOOK_SECRET"),
            frozenset(ints(required("APPROVER_TELEGRAM_IDS"))),
            frozenset(ints(required("ALLOWED_TELEGRAM_CHAT_IDS"))),
            Path(os.getenv("CATALOG_PATH", "examples/catalog.example.json")),
            Path(required("WORKBOOK_PATH")),
            Path(
                os.getenv(
                    "WORKBOOK_MAPPING_PATH", "examples/workbook_mapping.example.json"
                )
            ),
            Path(required("BACKUP_DIR")),
            Path(os.getenv("STATE_DB_PATH", "data/inventory.sqlite3")),
            os.getenv("TIMEZONE", "Asia/Singapore"),
            os.getenv("ENABLE_WRITES", "false").casefold()
            in {"1", "true", "yes", "on"},
            os.getenv("ADMIN_API_TOKEN", ""),
            float(os.getenv("LOCK_TIMEOUT_SECONDS", "5")),
        )

    def webhook_secret_matches(self, value: str) -> bool:
        return bool(value) and hmac.compare_digest(self.webhook_secret, value)

    @property
    def admin_api_token(self) -> str:
        return self.admin_token


def normalize(value: str) -> str:
    cleaned = re.sub(
        r"[^a-z0-9]+", " ", value.casefold().replace("×", "x")
    )
    return " ".join(cleaned.split())


def _find(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.I)
    return _clean(match.group(1)) if match else None


def _clean(value: str) -> str:
    return value.strip().strip(".,;|")[:200]


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def ints(value: str) -> list[int]:
    result = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not result:
        raise RuntimeError("at least one numeric ID is required")
    return result
