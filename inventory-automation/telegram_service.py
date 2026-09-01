from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from inventory_store import RequestStore
from inventory_types import InventoryParser, ParsedRequest, Settings
from safe_xlsm import SafeXlsmWriter


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self.client.post(f"{self.base}/{method}", json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram rejected {method}")
        return body.get("result")

    async def send(
        self,
        chat_id: int,
        text: str,
        markup: dict[str, Any] | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if markup:
            payload["reply_markup"] = markup
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to}
        return await self.call("sendMessage", payload)

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        markup: dict[str, Any],
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:3900],
                "reply_markup": markup,
                "disable_web_page_preview": True,
            },
        )

    async def answer(
        self, callback_id: str, text: str, alert: bool = False
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text[:200],
                "show_alert": alert,
            },
        )


class InventoryService:
    CALLBACK = re.compile(
        r"^inv:(INV-[A-F0-9]{8}):(approve|reject|help|pick)(?::(\d+):(\d+))?$"
    )

    def __init__(
        self,
        settings: Settings,
        parser: InventoryParser,
        store: RequestStore,
        telegram: TelegramClient,
        writer: SafeXlsmWriter,
    ) -> None:
        self.settings = settings
        self.parser = parser
        self.store = store
        self.telegram = telegram
        self.writer = writer
        self.timezone = ZoneInfo(settings.timezone_name)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("callback_query"):
            await self._callback(update["callback_query"])
        elif update.get("message"):
            await self._message(update.get("update_id"), update["message"])

    async def _message(self, update_id: Any, message: dict[str, Any]) -> None:
        text = message.get("text")
        chat_id = message.get("chat", {}).get("id")
        if not isinstance(update_id, int) or not isinstance(chat_id, int):
            return
        if not isinstance(text, str):
            return
        if chat_id not in self.settings.allowed_chat_ids:
            return
        if message.get("from", {}).get("is_bot"):
            return
        if text.casefold().strip() == "/inventory_example":
            await self.telegram.send(
                chat_id,
                "Checkout 2x Atlas 75 white, 1x Zigbee hub | "
                "customer Tan | site Punggol | installer Hasan | job ZD-1042",
                reply_to=message.get("message_id"),
            )
            return
        if text.startswith("/"):
            return

        proposal = self.parser.parse(text)
        record, created = self.store.create(
            update_id,
            chat_id,
            int(message["message_id"]),
            message.get("from", {}).get("id"),
            text,
            proposal,
        )
        if not created:
            return
        sent = await self.telegram.send(
            chat_id,
            render_review(record, proposal),
            review_keyboard(record["public_id"], proposal, record["status"]),
            message.get("message_id"),
        )
        self.store.set_review_message(
            record["public_id"], int(sent["message_id"])
        )

    async def _callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        actor = callback.get("from", {}).get("id")
        match = self.CALLBACK.match(callback.get("data", ""))
        if not callback_id or not isinstance(actor, int) or not match:
            return
        if actor not in self.settings.approver_ids:
            await self.telegram.answer(
                callback_id,
                "Only an approved inventory controller can use this button.",
                True,
            )
            return

        public_id, action, item_raw, candidate_raw = match.groups()
        record = self.store.get(public_id)
        if not record:
            await self.telegram.answer(callback_id, "Request not found.", True)
            return
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        if chat_id != record["chat_id"]:
            await self.telegram.answer(callback_id, "Chat mismatch.", True)
            return
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return

        if action == "help":
            await self.telegram.answer(
                callback_id,
                "Send a new complete request: Checkout [items] | customer [name] "
                "| site [site] | installer [name] | job [ref]",
                True,
            )
            return
        if action == "reject":
            changed = self.store.reject(public_id, actor)
            await self.telegram.answer(
                callback_id,
                "Rejected." if changed else "Request is already closed.",
            )
            if changed:
                record = self.store.get(public_id) or record
                parsed = ParsedRequest.from_dict(record["parsed"])
                await self.telegram.edit(
                    chat_id,
                    message_id,
                    render_review(record, parsed),
                    {"inline_keyboard": []},
                )
            return
        if action == "pick":
            await self._pick(
                callback_id,
                record,
                int(item_raw or -1),
                int(candidate_raw or -1),
                chat_id,
                message_id,
            )
            return
        await self._approve(callback_id, record, actor, chat_id, message_id)

    async def _pick(
        self,
        callback_id: str,
        record: dict[str, Any],
        item_index: int,
        candidate_index: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        if record["status"] not in {"flagged", "pending_approval"}:
            await self.telegram.answer(callback_id, "Request is closed.", True)
            return
        parsed = ParsedRequest.from_dict(record["parsed"])
        try:
            item = parsed.items[item_index]
            candidate = item.candidates[candidate_index]
        except IndexError:
            await self.telegram.answer(
                callback_id, "Candidate is no longer valid.", True
            )
            return
        item.sku = candidate["sku"]
        item.name = candidate["name"]
        item.issue = None
        item.candidates = []
        self.store.save_proposal(record["public_id"], parsed)
        updated = self.store.get(record["public_id"]) or record
        await self.telegram.answer(
            callback_id, f"Selected {candidate['sku']}."
        )
        await self.telegram.edit(
            chat_id,
            message_id,
            render_review(updated, parsed),
            review_keyboard(record["public_id"], parsed, updated["status"]),
        )

    async def _approve(
        self,
        callback_id: str,
        record: dict[str, Any],
        actor: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        parsed = ParsedRequest.from_dict(record["parsed"])
        if not parsed.ready or record["status"] not in {
            "pending_approval",
            "failed",
        }:
            await self.telegram.answer(
                callback_id, "Resolve every flag before approval.", True
            )
            return
        if not self.settings.enable_writes:
            await self.telegram.answer(
                callback_id,
                "Validation passed, but ENABLE_WRITES=false. "
                "No workbook change was made.",
                True,
            )
            return
        if not self.store.claim(record["public_id"], actor):
            await self.telegram.answer(
                callback_id, "Request is already claimed or closed.", True
            )
            return

        await self.telegram.answer(
            callback_id, "Approved. Running controlled write."
        )
        try:
            receipt = await asyncio.to_thread(
                self.writer.append,
                record["public_id"],
                self._rows(record, parsed, actor),
            )
            self.store.committed(record["public_id"], receipt.to_dict())
        except Exception as error:
            self.store.failed(
                record["public_id"], f"{type(error).__name__}: {error}"
            )

        updated = self.store.get(record["public_id"]) or record
        parsed = ParsedRequest.from_dict(updated["parsed"])
        markup = (
            review_keyboard(updated["public_id"], parsed, updated["status"])
            if updated["status"] == "failed"
            else {"inline_keyboard": []}
        )
        await self.telegram.edit(
            chat_id, message_id, render_review(updated, parsed), markup
        )

    def _rows(
        self,
        record: dict[str, Any],
        parsed: ParsedRequest,
        actor: int,
    ) -> list[dict[str, Any]]:
        now = datetime.now(self.timezone)
        rows = []
        for index, item in enumerate(parsed.items, 1):
            quantity: int | float = item.quantity or 0
            if float(quantity).is_integer():
                quantity = int(quantity)
            rows.append(
                {
                    "timestamp": now.isoformat(timespec="seconds"),
                    "date": now.date().isoformat(),
                    "customer": parsed.customer,
                    "site": parsed.site,
                    "job_ref": parsed.job_ref,
                    "installer": parsed.installer,
                    "sku": item.sku,
                    "item_name": item.name,
                    "quantity": quantity,
                    "movement": "CHECKED OUT",
                    "row_id": f"{record['public_id']}:{index:02d}",
                    "approved_by": str(actor),
                    "source": "Telegram",
                    "source_chat_id": str(record["chat_id"]),
                    "source_message_id": str(record["message_id"]),
                    "raw_item": item.raw,
                }
            )
        return rows


def render_review(record: dict[str, Any], parsed: ParsedRequest) -> str:
    lines = [
        f"Inventory request {record['public_id']}",
        f"Status: {record['status'].replace('_', ' ').upper()}",
        "",
        f"Customer: {parsed.customer or 'MISSING'}",
        f"Site: {parsed.site or 'MISSING'}",
        f"Installer: {parsed.installer or 'MISSING'}",
        f"Job ref: {parsed.job_ref or 'MISSING'}",
        "",
        "Items:",
    ]
    if not parsed.items:
        lines.append("- MISSING")
    for index, item in enumerate(parsed.items, 1):
        if item.quantity is None:
            quantity = "?"
        elif item.quantity.is_integer():
            quantity = str(int(item.quantity))
        else:
            quantity = str(item.quantity)
        if item.sku:
            lines.append(
                f"{index}. {quantity} x {item.sku} — {item.name}"
            )
        else:
            lines.append(
                f"{index}. {quantity} x {item.raw} — FLAG: {item.issue}"
            )

    flags = []
    if parsed.missing_fields:
        flags.append("Missing: " + ", ".join(parsed.missing_fields))
    flags.extend(
        f"Item {index}: {item.issue.replace('_', ' ')}"
        for index, item in enumerate(parsed.items, 1)
        if item.issue
    )
    if flags:
        lines.extend(["", "Flags:", *[f"- {flag}" for flag in flags]])
    elif record["status"] == "pending_approval":
        lines.extend(["", "Validated. No workbook change yet."])
    elif record["status"] == "committed":
        receipt = record.get("receipt") or {}
        lines.extend(
            [
                "",
                f"Committed rows: {receipt.get('rows_appended', 0)}",
                f"Workbook SHA-256: {str(receipt.get('output_sha256', ''))[:16]}…",
                "Backup created; non-target workbook parts verified.",
            ]
        )
    elif record["status"] == "failed":
        lines.extend(
            [
                "",
                "WRITE NOT CONFIRMED — retry checks workbook row IDs.",
                f"Reason: {record.get('error', '')[:500]}",
            ]
        )
    elif record["status"] == "rejected":
        lines.extend(["", "Rejected. No workbook change made."])
    return "\n".join(lines)


def review_keyboard(
    public_id: str, parsed: ParsedRequest, status: str
) -> dict[str, Any]:
    rows = []
    for item_index, item in enumerate(parsed.items):
        for candidate_index, candidate in enumerate(item.candidates[:8]):
            rows.append(
                [
                    {
                        "text": (
                            f"Item {item_index + 1}: {candidate['name']} "
                            f"({candidate['sku']})"
                        )[:60],
                        "callback_data": (
                            f"inv:{public_id}:pick:{item_index}:{candidate_index}"
                        ),
                    }
                ]
            )
    if parsed.ready and status in {"pending_approval", "failed"}:
        label = (
            "Retry controlled write" if status == "failed" else "Approve & write"
        )
        rows.append(
            [
                {
                    "text": label,
                    "callback_data": f"inv:{public_id}:approve",
                },
                {
                    "text": "Reject",
                    "callback_data": f"inv:{public_id}:reject",
                },
            ]
        )
    elif status in {"flagged", "pending_approval"}:
        rows.append(
            [
                {
                    "text": "Correction format",
                    "callback_data": f"inv:{public_id}:help",
                },
                {
                    "text": "Reject",
                    "callback_data": f"inv:{public_id}:reject",
                },
            ]
        )
    return {"inline_keyboard": rows}
