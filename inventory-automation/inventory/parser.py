from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .catalog import SkuCatalog
from .models import ParsedCheckout, ParsedLine


_FIELD_TOKEN_RE = re.compile(
    r"(?i)(?<!\w)(checkout\s+by|checked\s+out\s+by|received\s+by|receiver|"
    r"customer|client|name|address|site|quote|invoice|purpose|job|reason|"
    r"items?|products?|notes?)\s*(?::|=|-)?\s*"
)
_ITEM_RE = re.compile(
    r"(?ix)^\s*(?P<return>return(?:ed)?\s+)?(?P<qty>[+-]?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>x|×|pcs?|pieces?|units?|m|metres?|meters?)?\s*(?:of\s+)?(?P<label>.+?)\s*$"
)
_TRAILING_RE = re.compile(
    r"(?ix)^\s*(?P<return>return(?:ed)?\s+)?(?P<label>.+?)\s+(?:x|×)\s*"
    r"(?P<qty>[+-]?\d+(?:\.\d+)?)\s*$"
)
_SPLIT_RE = re.compile(
    r"\s*(?:(?:,|;|\n)|\band\b(?=\s+(?:return(?:ed)?\s+)?[+-]?\d))\s*",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip(";,. "))


def _field(token: str) -> str:
    normalized = re.sub(r"\s+", " ", token.casefold().strip())
    if normalized in {"checkout by", "checked out by"}:
        return "checkout_by"
    if normalized in {"received by", "receiver"}:
        return "received_by"
    if normalized in {"customer", "client", "name"}:
        return "name"
    if normalized in {"address", "site"}:
        return "address"
    if normalized in {"quote", "invoice"}:
        return "quote"
    if normalized in {"purpose", "job", "reason"}:
        return "purpose"
    if normalized in {"item", "items", "product", "products"}:
        return "items"
    return "note"


class NaturalCheckoutParser:
    def __init__(self, catalog: SkuCatalog) -> None:
        self.catalog = catalog

    def parse(self, text: str) -> ParsedCheckout:
        text = text.strip()
        parsed = ParsedCheckout(original_text=text)
        if not text:
            parsed.parser_warnings.append("Message is empty")
            return parsed
        fields = self._extract_fields(text)
        for name in ("checkout_by", "received_by", "name", "address", "quote", "purpose", "note"):
            if fields.get(name):
                setattr(parsed, name, _clean(fields[name]))
        self._fallback_metadata(parsed, text)
        item_text = fields.get("items") or self._fallback_items(text)
        if not item_text:
            parsed.parser_warnings.append("No item segment could be identified")
            return parsed
        parsed.lines = self._parse_items(item_text, parsed.parser_warnings)
        if parsed.purpose and re.fullmatch(r"(?i)return(?:ed)?", parsed.purpose.strip()):
            for line in parsed.lines:
                if line.quantity > 0:
                    line.quantity = -line.quantity
        return parsed

    def _extract_fields(self, text: str) -> dict[str, str]:
        matches = list(_FIELD_TOKEN_RE.finditer(text))
        fields: dict[str, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = _clean(text[match.end():end])
            if not value:
                continue
            key = _field(match.group(1))
            fields[key] = f"{fields[key]}; {value}" if key in fields else value
        return fields

    def _fallback_metadata(self, parsed: ParsedCheckout, text: str) -> None:
        if not parsed.quote:
            match = re.search(r"(?i)\b(?:quote|invoice)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]*)", text)
            if match:
                parsed.quote = _clean(match.group(1))
        if not parsed.checkout_by:
            match = re.search(r"(?i)(?<!received\s)\bby\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?=[.;,]|$)", text)
            if match:
                parsed.checkout_by = _clean(match.group(1))
        if not parsed.name or not parsed.address:
            match = re.search(
                r"(?is)\bfor\s+(.+?)\s+\bat\s+(.+?)(?=(?:[.;,]\s*(?:quote|invoice|checkout\s+by|received\s+by|purpose|job)\b)|$)",
                text,
            )
            if match:
                parsed.name = parsed.name or _clean(match.group(1))
                parsed.address = parsed.address or _clean(match.group(2))
        if not parsed.purpose:
            for pattern, purpose in (
                (r"\bservic(?:e|ing)\b", "Servicing"),
                (r"\bdeliver(?:y|ing)?\b", "Delivery"),
                (r"\breturn(?:ed|ing)?\b", "Return"),
                (r"\binstall(?:ation|ing)?\b", "Install"),
                (r"\bhandover\b", "Handover"),
            ):
                if re.search(pattern, text, re.IGNORECASE):
                    parsed.purpose = purpose
                    break

    def _fallback_items(self, text: str) -> str | None:
        match = re.match(
            r"(?is)^\s*(?P<verb>checkout|take|return)\s+(?P<items>.+?)\s+"
            r"(?=(?:for|customer|client|address|site|quote|invoice|checkout\s+by|received\s+by|purpose|job)\b)",
            text,
        )
        if not match:
            return None
        items = _clean(match.group("items"))
        return f"return {items}" if match.group("verb").casefold() == "return" else items

    def _parse_items(self, item_text: str, warnings: list[str]) -> list[ParsedLine]:
        lines: list[ParsedLine] = []
        for chunk in (part for part in _SPLIT_RE.split(item_text) if part.strip()):
            cleaned = _clean(chunk)
            match = _ITEM_RE.match(cleaned) or _TRAILING_RE.match(cleaned)
            if not match:
                warnings.append(f"Could not parse item: {cleaned}")
                continue
            try:
                quantity = Decimal(match.group("qty"))
            except (InvalidOperation, TypeError):
                warnings.append(f"Invalid quantity in item: {cleaned}")
                continue
            if match.group("return") and quantity > 0:
                quantity = -quantity
            label = _clean(match.group("label"))
            resolution = self.catalog.resolve(label)
            lines.append(
                ParsedLine(
                    raw_label=label,
                    quantity=quantity,
                    sku=resolution.sku,
                    candidates=list(resolution.candidates),
                    resolution=resolution.method,
                )
            )
        return lines
