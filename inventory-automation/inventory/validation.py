from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

from .models import CanonicalLine, ParsedCheckout, ValidationFlag, ValidationResult

_REQUIRED = (
    ("checkout_by", "Checkout By"),
    ("received_by", "Received By"),
    ("name", "Customer/Name"),
    ("address", "Address"),
    ("purpose", "Purpose"),
)


def validate_checkout(
    parsed: ParsedCheckout, *, max_abs_quantity: int, max_items: int = 50
) -> ValidationResult:
    flags: list[ValidationFlag] = []
    for field, label in _REQUIRED:
        value = getattr(parsed, field)
        if not value or not value.strip():
            flags.append(ValidationFlag("MISSING_FIELD", f"{label} is required", field=field))
        elif len(value.strip()) > 200:
            flags.append(ValidationFlag("FIELD_TOO_LONG", f"{label} exceeds 200 characters", field=field))
    if parsed.quote and len(parsed.quote) > 100:
        flags.append(ValidationFlag("FIELD_TOO_LONG", "Quote exceeds 100 characters", field="quote"))
    if parsed.note and len(parsed.note) > 500:
        flags.append(ValidationFlag("FIELD_TOO_LONG", "Note exceeds 500 characters", field="note"))
    if len(parsed.lines) > max_items:
        flags.append(ValidationFlag("TOO_MANY_ITEMS", f"Request has {len(parsed.lines)} item lines; maximum is {max_items}", field="items"))
    if not parsed.lines:
        flags.append(ValidationFlag("NO_ITEMS", "At least one item is required", field="items"))

    combined: OrderedDict[str, Decimal] = OrderedDict()
    for index, line in enumerate(parsed.lines):
        if line.quantity == 0:
            flags.append(ValidationFlag("ZERO_QUANTITY", f"{line.raw_label!r} has a zero quantity", line_index=index))
        elif abs(line.quantity) > Decimal(max_abs_quantity):
            flags.append(ValidationFlag("QUANTITY_LIMIT", f"{line.raw_label!r} exceeds the configured absolute quantity limit of {max_abs_quantity}", line_index=index))
        if not line.sku:
            flags.append(
                ValidationFlag(
                    "SKU_AMBIGUOUS" if line.candidates else "SKU_UNKNOWN",
                    f"Select an exact SKU for {line.raw_label!r}" if line.candidates else f"Unknown SKU or alias: {line.raw_label!r}",
                    field="items",
                    line_index=index,
                    candidates=tuple(line.candidates),
                )
            )
            continue
        combined[line.sku] = combined.get(line.sku, Decimal("0")) + line.quantity

    canonical: list[CanonicalLine] = []
    for sku, quantity in combined.items():
        if quantity == 0:
            flags.append(ValidationFlag("NET_ZERO_ITEM", f"{sku} nets to zero after duplicate/return combining", field="items"))
        else:
            canonical.append(CanonicalLine(sku, quantity))
    return ValidationResult(canonical, flags)
