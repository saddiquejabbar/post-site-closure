from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inventory_flow import Catalog, InventoryParser, RequestStore


def parser(tmp_path: Path) -> InventoryParser:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "products": [
                    {"sku": "SW-W", "name": "Atlas 75 White", "aliases": ["atlas 75 white", "atlas 75"]},
                    {"sku": "SW-B", "name": "Atlas 75 Black", "aliases": ["atlas 75 black", "atlas 75"]},
                    {"sku": "HUB-1", "name": "Zigbee Hub", "aliases": ["zigbee hub", "gateway"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    return InventoryParser(Catalog.load(path))


def test_clean_natural_request(tmp_path: Path) -> None:
    proposal = parser(tmp_path).parse(
        "Checkout 2x Atlas 75 white, 1x Zigbee hub | customer Tan | "
        "site Punggol | installer Hasan | job ZD-1042"
    )
    assert proposal.ready
    assert proposal.customer == "Tan"
    assert proposal.job_ref == "ZD-1042"
    assert [item.sku for item in proposal.items] == ["SW-W", "HUB-1"]


def test_sentence_request(tmp_path: Path) -> None:
    proposal = parser(tmp_path).parse(
        "Checkout 2x Atlas 75 white and 1x Zigbee hub for Tan at Punggol. "
        "Installer Hasan. Job ZD-1042."
    )
    assert proposal.ready
    assert proposal.site == "Punggol"
    assert proposal.installer == "Hasan"


def test_ambiguous_and_unknown_items_fail_closed(tmp_path: Path) -> None:
    ambiguous = parser(tmp_path).parse(
        "Checkout 2x Atlas 75 | customer Tan | site Punggol | installer Hasan | job ZD-1042"
    )
    assert not ambiguous.ready
    assert ambiguous.items[0].issue == "ambiguous_sku"
    assert {item["sku"] for item in ambiguous.items[0].candidates} == {"SW-W", "SW-B"}

    unknown = parser(tmp_path).parse(
        "Checkout 1x mystery controller | customer Tan | site Punggol | installer Hasan"
    )
    assert not unknown.ready
    assert "job_ref" in unknown.missing_fields
    assert unknown.items[0].issue == "unknown_item"


def test_store_deduplicates_telegram_updates(tmp_path: Path) -> None:
    proposal = parser(tmp_path).parse(
        "Checkout 1x Zigbee hub | customer Tan | site Punggol | installer Hasan | job ZD-1042"
    )
    store = RequestStore(tmp_path / "state.sqlite3")
    first, created_first = store.create(99, -100, 5, 7, "request", proposal)
    second, created_second = store.create(99, -100, 5, 7, "request", proposal)
    assert created_first is True
    assert created_second is False
    assert second["public_id"] == first["public_id"]
