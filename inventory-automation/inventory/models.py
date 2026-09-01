from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class RequestStatus(StrEnum):
    RECEIVED = "received"
    NEEDS_REVIEW = "needs_review"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    WRITING = "writing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    DRY_RUN_COMPLETE = "dry_run_complete"


@dataclass(slots=True)
class ParsedLine:
    raw_label: str
    quantity: Decimal
    sku: str | None = None
    candidates: list[str] = field(default_factory=list)
    resolution: str = "unresolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_label": self.raw_label,
            "quantity": str(self.quantity),
            "sku": self.sku,
            "candidates": list(self.candidates),
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParsedLine":
        return cls(
            raw_label=str(value.get("raw_label", "")),
            quantity=Decimal(str(value.get("quantity", "0"))),
            sku=value.get("sku"),
            candidates=[str(item) for item in value.get("candidates", [])],
            resolution=str(value.get("resolution", "unresolved")),
        )


@dataclass(slots=True)
class ParsedCheckout:
    original_text: str
    checkout_by: str | None = None
    received_by: str | None = None
    quote: str | None = None
    name: str | None = None
    address: str | None = None
    purpose: str | None = None
    note: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    parser_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_text": self.original_text,
            "checkout_by": self.checkout_by,
            "received_by": self.received_by,
            "quote": self.quote,
            "name": self.name,
            "address": self.address,
            "purpose": self.purpose,
            "note": self.note,
            "lines": [line.to_dict() for line in self.lines],
            "parser_warnings": list(self.parser_warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParsedCheckout":
        return cls(
            original_text=str(value.get("original_text", "")),
            checkout_by=value.get("checkout_by"),
            received_by=value.get("received_by"),
            quote=value.get("quote"),
            name=value.get("name"),
            address=value.get("address"),
            purpose=value.get("purpose"),
            note=value.get("note"),
            lines=[ParsedLine.from_dict(item) for item in value.get("lines", [])],
            parser_warnings=[str(item) for item in value.get("parser_warnings", [])],
        )


@dataclass(slots=True, frozen=True)
class ValidationFlag:
    code: str
    message: str
    field: str | None = None
    line_index: int | None = None
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "line_index": self.line_index,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ValidationFlag":
        return cls(
            code=str(value["code"]),
            message=str(value["message"]),
            field=value.get("field"),
            line_index=value.get("line_index"),
            candidates=tuple(str(item) for item in value.get("candidates", [])),
        )


@dataclass(slots=True, frozen=True)
class CanonicalLine:
    sku: str
    quantity: Decimal

    def to_dict(self) -> dict[str, str]:
        return {"sku": self.sku, "quantity": str(self.quantity)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CanonicalLine":
        return cls(sku=str(value["sku"]), quantity=Decimal(str(value["quantity"])))


@dataclass(slots=True)
class ValidationResult:
    canonical_lines: list[CanonicalLine]
    flags: list[ValidationFlag]

    @property
    def ready(self) -> bool:
        return bool(self.canonical_lines) and not self.flags

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_lines": [line.to_dict() for line in self.canonical_lines],
            "flags": [flag.to_dict() for flag in self.flags],
            "ready": self.ready,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ValidationResult":
        return cls(
            canonical_lines=[CanonicalLine.from_dict(item) for item in value.get("canonical_lines", [])],
            flags=[ValidationFlag.from_dict(item) for item in value.get("flags", [])],
        )


@dataclass(slots=True, frozen=True)
class WorkbookCheckout:
    request_id: str
    timestamp: datetime
    checkout_by: str
    received_by: str
    quote: str
    name: str
    address: str
    purpose: str
    note: str
    lines: tuple[CanonicalLine, ...]


@dataclass(slots=True, frozen=True)
class WorkbookWriteResult:
    row: int
    before_sha256: str
    after_sha256: str
    backup_path: str
    duplicate: bool = False
