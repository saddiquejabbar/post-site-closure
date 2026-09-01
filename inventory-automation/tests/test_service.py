from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from inventory.config import Settings
from inventory.models import RequestStatus, WorkbookWriteResult
from inventory.service import InventoryService
from inventory.store import InventoryStore
from inventory.workbook import WorkbookInspection


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.next_message_id = 100

    async def close(self) -> None:
        return None

    async def send_message(self, **kwargs: Any) -> int:
        message_id = self.next_message_id
        self.next_message_id += 1
        self.sent.append({**kwargs, "message_id": message_id})
        return message_id

    async def edit_message(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)

    async def answer_callback(self, **kwargs: Any) -> None:
        self.answers.append(kwargs)


class FakeWriter:
    def __init__(self, *, result: WorkbookWriteResult | None = None) -> None:
        self.result = result
        self.applied = []

    def inspect(self) -> WorkbookInspection:
        return WorkbookInspection(
            sheet_path="xl/worksheets/sheet1.xml",
            sku_headers=("SKU-SWITCH-1G", "SKU-HUB-ZB", "SKU-IR-RF"),
            first_empty_row=5,
            date_1904=False,
            status_formula_template_row=4,
            vba_members=("xl/vbaProject.bin",),
        )

    def apply(self, checkout):
        self.applied.append(checkout)
        if self.result is None:
            raise AssertionError("Writer must not be called in dry-run mode")
        return self.result


def settings(tmp_path: Path, *, write_enabled: bool) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_webhook_secret="0123456789abcdef",
        allowed_chat_ids=frozenset({-100}),
        allowed_user_ids=frozenset({1, 2}),
        approver_user_ids=frozenset({1}),
        workbook_path=tmp_path / "Inventory2.xlsm",
        sku_aliases_path=tmp_path / "aliases.json",
        state_db_path=tmp_path / "state.sqlite3",
        lock_file_path=tmp_path / "writer.lock",
        staging_dir=tmp_path / "staging",
        backup_dir=tmp_path / "backups",
        timezone=ZoneInfo("Asia/Singapore"),
        write_enabled=write_enabled,
        source_stability_seconds=0,
        post_write_stability_seconds=0,
    )


def structured_message(items: str = "2x SKU-SWITCH-1G, 1x SKU-HUB-ZB") -> str:
    return (
        "Checkout by: Alex\n"
        "Received by: Sam\n"
        "Customer: Demo Home\n"
        "Address: 10 Example Road\n"
        "Quote: Q-1001\n"
        "Purpose: Install\n"
        f"Items: {items}"
    )


def request_id_from_preview(text: str) -> str:
    return next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Request: "))


def approval_callback(fake: FakeTelegram) -> str:
    markup = fake.sent[0]["reply_markup"]
    for row in markup["inline_keyboard"]:
        for button in row:
            if button["text"].startswith("Approve"):
                return button["callback_data"]
    raise AssertionError("Approval button missing")


@pytest.mark.asyncio
async def test_dry_run_approval_and_unauthorised_button_does_not_consume(tmp_path: Path) -> None:
    fake = FakeTelegram()
    writer = FakeWriter()
    service = InventoryService(
        settings=settings(tmp_path, write_enabled=False),
        store=InventoryStore(tmp_path / "state.sqlite3"),
        telegram=fake,
        writer=writer,
    )
    await service.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": -100},
                "from": {"id": 2, "first_name": "Requester"},
                "text": structured_message(),
            },
        }
    )
    callback_data = approval_callback(fake)
    request_id = request_id_from_preview(fake.sent[0]["text"])

    await service.handle_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-denied",
                "from": {"id": 3},
                "data": callback_data,
                "message": {"message_id": 100, "chat": {"id": -100}},
            },
        }
    )
    assert fake.answers[-1]["alert"] is True

    await service.handle_update(
        {
            "update_id": 3,
            "callback_query": {
                "id": "cb-approved",
                "from": {"id": 1},
                "data": callback_data,
                "message": {"message_id": 100, "chat": {"id": -100}},
            },
        }
    )
    record = service.store.get_request(request_id)
    assert record.status == RequestStatus.DRY_RUN_COMPLETE
    assert writer.applied == []
    assert "Workbook changed: NO" in fake.edited[-1]["text"]


@pytest.mark.asyncio
async def test_approved_write_uses_combined_canonical_quantities(tmp_path: Path) -> None:
    fake = FakeTelegram()
    writer = FakeWriter(
        result=WorkbookWriteResult(
            row=6346,
            before_sha256="a" * 64,
            after_sha256="b" * 64,
            backup_path="/safe/backups/example.xlsm",
        )
    )
    service = InventoryService(
        settings=settings(tmp_path, write_enabled=True),
        store=InventoryStore(tmp_path / "state.sqlite3"),
        telegram=fake,
        writer=writer,
    )
    await service.handle_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "chat": {"id": -100},
                "from": {"id": 1, "first_name": "Approver"},
                "text": structured_message("2x SKU-SWITCH-1G, 1x SKU-SWITCH-1G"),
            },
        }
    )
    request_id = request_id_from_preview(fake.sent[0]["text"])
    await service.handle_update(
        {
            "update_id": 11,
            "callback_query": {
                "id": "cb-write",
                "from": {"id": 1},
                "data": approval_callback(fake),
                "message": {"message_id": 100, "chat": {"id": -100}},
            },
        }
    )

    record = service.store.get_request(request_id)
    assert record.status == RequestStatus.COMPLETED
    assert record.workbook_row == 6346
    assert len(writer.applied) == 1
    assert str(writer.applied[0].lines[0].quantity) == "3"
    assert "SAVED AND VERIFIED" in fake.edited[-1]["text"]
