from decimal import Decimal

import pytest

from inventory.models import CanonicalLine, ParsedCheckout, RequestStatus, ValidationResult
from inventory.store import CallbackError, InventoryStore


def request_data() -> tuple[ParsedCheckout, ValidationResult]:
    parsed = ParsedCheckout(
        original_text="demo",
        checkout_by="Alex",
        received_by="Sam",
        name="Demo",
        address="Example",
        purpose="Install",
    )
    validation = ValidationResult(
        canonical_lines=[CanonicalLine("SKU-A", Decimal("1"))], flags=[]
    )
    return parsed, validation


def test_update_and_request_idempotency(tmp_path) -> None:
    store = InventoryStore(tmp_path / "state.sqlite3")
    assert store.begin_update(10)
    assert not store.begin_update(10)

    parsed, validation = request_data()
    record = store.create_request(
        request_id="inv_test",
        source_update_id=10,
        chat_id=-100,
        user_id=1,
        source_message_id=20,
        original_text="demo",
        parsed=parsed,
        validation=validation,
        status=RequestStatus.AWAITING_APPROVAL,
    )
    assert record.status == RequestStatus.AWAITING_APPROVAL
    store.set_preview_message("inv_test", 30)
    assert store.find_by_preview_message(chat_id=-100, preview_message_id=30).request_id == "inv_test"


def test_callbacks_are_single_use(tmp_path) -> None:
    store = InventoryStore(tmp_path / "state.sqlite3")
    store.begin_update(1)
    parsed, validation = request_data()
    store.create_request(
        request_id="inv_test",
        source_update_id=1,
        chat_id=-100,
        user_id=1,
        source_message_id=20,
        original_text="demo",
        parsed=parsed,
        validation=validation,
        status=RequestStatus.AWAITING_APPROVAL,
    )
    token = store.create_callback(
        request_id="inv_test", action="approve", payload={}, ttl_minutes=30
    )

    assert store.get_callback(token).action == "approve"
    assert store.consume_callback(token).request_id == "inv_test"
    with pytest.raises(CallbackError):
        store.consume_callback(token)


def test_status_transition_is_compare_and_set(tmp_path) -> None:
    store = InventoryStore(tmp_path / "state.sqlite3")
    store.begin_update(1)
    parsed, validation = request_data()
    store.create_request(
        request_id="inv_test",
        source_update_id=1,
        chat_id=-100,
        user_id=1,
        source_message_id=20,
        original_text="demo",
        parsed=parsed,
        validation=validation,
        status=RequestStatus.AWAITING_APPROVAL,
    )

    assert store.transition(
        request_id="inv_test",
        expected=(RequestStatus.AWAITING_APPROVAL,),
        new_status=RequestStatus.APPROVED,
        actor="telegram:1",
        action="approved",
    )
    assert not store.transition(
        request_id="inv_test",
        expected=(RequestStatus.AWAITING_APPROVAL,),
        new_status=RequestStatus.APPROVED,
        actor="telegram:1",
        action="approved_twice",
    )
