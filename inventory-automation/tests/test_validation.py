from decimal import Decimal

from inventory.models import ParsedCheckout, ParsedLine
from inventory.validation import validate_checkout


def complete_request(lines: list[ParsedLine]) -> ParsedCheckout:
    return ParsedCheckout(
        original_text="demo",
        checkout_by="Alex",
        received_by="Sam",
        name="Demo Home",
        address="10 Example Road",
        purpose="Install",
        lines=lines,
    )


def test_duplicate_skus_combine_and_returns_reduce_quantity() -> None:
    parsed = complete_request(
        [
            ParsedLine("switch", Decimal("3"), sku="SKU-A", resolution="exact_header"),
            ParsedLine("switch", Decimal("2"), sku="SKU-A", resolution="exact_header"),
            ParsedLine("returned switch", Decimal("-1"), sku="SKU-A", resolution="exact_header"),
        ]
    )
    result = validate_checkout(parsed, max_abs_quantity=1000)

    assert result.ready
    assert [(line.sku, line.quantity) for line in result.canonical_lines] == [
        ("SKU-A", Decimal("4"))
    ]


def test_missing_metadata_unknown_sku_and_quantity_limit_block() -> None:
    parsed = ParsedCheckout(
        original_text="demo",
        lines=[ParsedLine("mystery", Decimal("1001"), candidates=[], resolution="unresolved")],
    )
    result = validate_checkout(parsed, max_abs_quantity=1000)
    codes = {flag.code for flag in result.flags}

    assert "MISSING_FIELD" in codes
    assert "SKU_UNKNOWN" in codes
    assert "QUANTITY_LIMIT" in codes
    assert not result.ready


def test_too_many_items_blocks_request() -> None:
    parsed = complete_request(
        [
            ParsedLine(str(index), Decimal("1"), sku=f"SKU-{index}", resolution="exact_header")
            for index in range(3)
        ]
    )
    result = validate_checkout(parsed, max_abs_quantity=1000, max_items=2)
    assert any(flag.code == "TOO_MANY_ITEMS" for flag in result.flags)
