from decimal import Decimal

from inventory.catalog import SkuCatalog
from inventory.parser import NaturalCheckoutParser
from inventory.validation import validate_checkout


def catalog() -> SkuCatalog:
    return SkuCatalog(
        ["SKU-SWITCH-1G", "SKU-HUB-ZB", "SKU-LED-CCT", "SKU-IR-RF"],
        {
            "hub": "SKU-HUB-ZB",
            "led strip": "SKU-LED-CCT",
            "controller": ["SKU-HUB-ZB", "SKU-IR-RF"],
        },
    )


def test_structured_message_parses_and_resolves_aliases() -> None:
    parsed = NaturalCheckoutParser(catalog()).parse(
        """Checkout by: Alex Tan
Received by: Sam Lee
Customer: Demo Home
Address: 10 Example Road
Quote: Q-1001
Purpose: Install
Items: 2x SKU-SWITCH-1G, 1x hub, 3.5m led strip
Notes: staged demo"""
    )

    assert parsed.checkout_by == "Alex Tan"
    assert parsed.received_by == "Sam Lee"
    assert parsed.name == "Demo Home"
    assert parsed.address == "10 Example Road"
    assert parsed.quote == "Q-1001"
    assert parsed.purpose == "Install"
    assert parsed.note == "staged demo"
    assert [(line.sku, line.quantity) for line in parsed.lines] == [
        ("SKU-SWITCH-1G", Decimal("2")),
        ("SKU-HUB-ZB", Decimal("1")),
        ("SKU-LED-CCT", Decimal("3.5")),
    ]
    assert validate_checkout(parsed, max_abs_quantity=1000).ready


def test_natural_sentence_fallback_and_return_sign() -> None:
    parsed = NaturalCheckoutParser(catalog()).parse(
        "Return 2x SKU-SWITCH-1G and 1x hub for Demo Home at 10 Example Road; "
        "received by Sam; checkout by Alex; purpose return"
    )

    assert parsed.name == "Demo Home"
    assert parsed.address == "10 Example Road"
    assert parsed.checkout_by == "Alex"
    assert parsed.received_by == "Sam"
    assert [(line.sku, line.quantity) for line in parsed.lines] == [
        ("SKU-SWITCH-1G", Decimal("-2")),
        ("SKU-HUB-ZB", Decimal("-1")),
    ]


def test_ambiguous_alias_is_flagged_not_guessed() -> None:
    parsed = NaturalCheckoutParser(catalog()).parse(
        """Checkout by: Alex
Received by: Sam
Customer: Demo
Address: Example
Purpose: Install
Items: 1x controller"""
    )
    result = validate_checkout(parsed, max_abs_quantity=1000)

    assert not result.ready
    flag = next(flag for flag in result.flags if flag.code == "SKU_AMBIGUOUS")
    assert flag.line_index == 0
    assert flag.candidates == ("SKU-HUB-ZB", "SKU-IR-RF")


def test_item_name_containing_and_is_not_split_without_next_quantity() -> None:
    local = SkuCatalog(["IR and RF controller"])
    parsed = NaturalCheckoutParser(local).parse(
        """Checkout by: Alex
Received by: Sam
Customer: Demo
Address: Example
Purpose: Install
Items: 1x IR and RF controller"""
    )
    assert len(parsed.lines) == 1
    assert parsed.lines[0].sku == "IR and RF controller"
