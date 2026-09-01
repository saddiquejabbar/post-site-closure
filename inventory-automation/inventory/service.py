from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .catalog import CatalogError, SkuCatalog
from .config import Settings
from .models import RequestStatus, WorkbookCheckout
from .parser import NaturalCheckoutParser
from .store import CallbackError, InventoryStore, RequestRecord, StoreError
from .telegram import TelegramClient, TelegramError
from .validation import validate_checkout
from .workbook import WorkbookContract, WorkbookError, WorkbookWriter

logger = logging.getLogger(__name__)
_PURPOSES = ("Install", "Delivery", "Servicing", "Return", "Handover")
_TERMINAL = {
    RequestStatus.COMPLETED,
    RequestStatus.DRY_RUN_COMPLETE,
    RequestStatus.FAILED,
    RequestStatus.CANCELLED,
    RequestStatus.SUPERSEDED,
}


class InventoryService:
    def __init__(self, *, settings: Settings, store: InventoryStore, telegram: TelegramClient, writer: WorkbookWriter) -> None:
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.writer = writer

    @classmethod
    def build(cls, settings: Settings) -> "InventoryService":
        contract = WorkbookContract(
            sheet_name=settings.log_sheet_name,
            header_row=settings.header_row,
            data_start_row=settings.data_start_row,
            data_end_row=settings.data_end_row,
            sku_start_column=settings.sku_start_column,
            sku_end_column=settings.sku_end_column,
            status_column=settings.status_column,
        )
        writer = WorkbookWriter(
            workbook_path=settings.workbook_path,
            contract=contract,
            lock_path=settings.lock_file_path,
            staging_dir=settings.staging_dir,
            backup_dir=settings.backup_dir,
            require_vba_project=settings.require_vba_project,
            source_stability_seconds=settings.source_stability_seconds,
            post_write_stability_seconds=settings.post_write_stability_seconds,
        )
        return cls(
            settings=settings,
            store=InventoryStore(settings.state_db_path),
            telegram=TelegramClient(settings.telegram_bot_token),
            writer=writer,
        )

    async def close(self) -> None:
        await self.telegram.close()

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or not self.store.begin_update(update_id):
            return
        try:
            if isinstance(update.get("callback_query"), dict):
                await self._handle_callback(update["callback_query"])
            elif isinstance(update.get("message"), dict):
                await self._handle_message(update_id, update["message"])
            self.store.finish_update(update_id)
        except Exception as exc:
            logger.exception("Inventory update %s stopped with %s", update_id, type(exc).__name__)
            self.store.finish_update(update_id, error=self._safe_error(exc))
            await self._notify_processing_error(update, exc)

    async def _handle_message(self, update_id: int, message: dict[str, Any]) -> None:
        chat_id = self._int_at(message, "chat", "id")
        user_id = self._int_at(message, "from", "id")
        message_id = message.get("message_id")
        text = message.get("text")
        if chat_id is None or user_id is None or not isinstance(message_id, int):
            return
        if chat_id not in self.settings.allowed_chat_ids:
            return
        if user_id not in self.settings.allowed_user_ids:
            await self.telegram.send_message(
                chat_id=chat_id,
                text="Inventory request rejected: this Telegram user is not authorised.",
                reply_to_message_id=message_id,
            )
            return
        if not isinstance(text, str) or not text.strip():
            return

        reply_to = message.get("reply_to_message")
        if text.strip() == "APPROVED" and isinstance(reply_to, dict):
            replied_id = reply_to.get("message_id")
            if isinstance(replied_id, int):
                record = self.store.find_by_preview_message(chat_id=chat_id, preview_message_id=replied_id)
                if record is not None:
                    await self._approve_request(record, approver_id=user_id, source="reply")
                    return
        if text.startswith("/"):
            await self._handle_command(chat_id, message_id, text)
            return

        try:
            catalog = await asyncio.to_thread(self._load_catalog)
        except (WorkbookError, CatalogError, OSError) as exc:
            await self.telegram.send_message(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                text=(
                    "Inventory request blocked: the workbook contract or SKU headers could not be read. "
                    f"No write occurred.\nReason: {self._safe_error(exc)}"
                ),
            )
            return

        parsed = NaturalCheckoutParser(catalog).parse(text)
        if not parsed.checkout_by:
            parsed.checkout_by = self._display_name(message.get("from"))
        validation = validate_checkout(
            parsed,
            max_abs_quantity=self.settings.max_abs_quantity,
            max_items=self.settings.max_items_per_request,
        )
        status = RequestStatus.AWAITING_APPROVAL if validation.ready else RequestStatus.NEEDS_REVIEW
        record = self.store.create_request(
            request_id=self.store.new_request_id(),
            source_update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            source_message_id=message_id,
            original_text=text,
            parsed=parsed,
            validation=validation,
            status=status,
        )
        preview_id = await self.telegram.send_message(
            chat_id=chat_id,
            text=self._render_preview(record),
            reply_to_message_id=message_id,
            reply_markup=self._build_keyboard(record),
        )
        self.store.set_preview_message(record.request_id, preview_id)

    async def _handle_command(self, chat_id: int, message_id: int, text: str) -> None:
        command = text.split()[0].casefold()
        if command in {"/start", "/help", "/inventory_help"}:
            output = (
                "Send one complete checkout message:\n\n"
                "Checkout by: Alex\nReceived by: Sam\nCustomer: Demo Home\n"
                "Address: 10 Example Road\nQuote: Q-1001\nPurpose: Install\n"
                "Items: 2x SKU-SWITCH-1G, 1x SKU-HUB-ZB\n\n"
                "Unknown or ambiguous SKUs are blocked and shown as buttons. Only an authorised approver can write."
            )
        elif command == "/inventory_status":
            mode = "WRITE ENABLED" if self.settings.write_enabled else "DRY RUN — WRITES DISABLED"
            output = f"Inventory automation status: {mode}."
        else:
            return
        await self.telegram.send_message(chat_id=chat_id, reply_to_message_id=message_id, text=output)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        data = callback.get("data")
        user_id = self._int_at(callback, "from", "id")
        message = callback.get("message")
        chat_id = self._int_at(message, "chat", "id") if isinstance(message, dict) else None
        if not isinstance(callback_id, str) or not isinstance(data, str) or user_id is None:
            return
        if not data.startswith("inv:"):
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Unknown action", alert=True)
            return
        try:
            pending = self.store.get_callback(data[4:])
            record = self.store.get_request(pending.request_id)
        except (CallbackError, StoreError) as exc:
            await self.telegram.answer_callback(callback_query_id=callback_id, text=str(exc), alert=True)
            return
        if chat_id != record.chat_id or chat_id not in self.settings.allowed_chat_ids:
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Invalid chat", alert=True)
            return
        if user_id not in self.settings.allowed_user_ids:
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Not authorised", alert=True)
            return
        can_edit = user_id == record.user_id or user_id in self.settings.approver_user_ids
        if pending.action != "approve" and not can_edit:
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Requester or approver required", alert=True)
            return
        if pending.action == "approve" and user_id not in self.settings.approver_user_ids:
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Approver permission required", alert=True)
            return
        try:
            action = self.store.consume_callback(data[4:])
        except CallbackError as exc:
            await self.telegram.answer_callback(callback_query_id=callback_id, text=str(exc), alert=True)
            return

        if action.action == "select_sku":
            await self.telegram.answer_callback(callback_query_id=callback_id, text="SKU selected")
            await self._select_sku(record, user_id, action.payload)
        elif action.action == "set_purpose":
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Purpose selected")
            await self._set_purpose(record, user_id, action.payload)
        elif action.action == "replace":
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Request replaced")
            await self._replace(record, user_id)
        elif action.action == "cancel":
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Request cancelled")
            await self._cancel(record, user_id)
        elif action.action == "approve":
            await self.telegram.answer_callback(callback_query_id=callback_id, text="Approval accepted")
            await self._approve_request(record, approver_id=user_id, source="button")

    async def _select_sku(self, record: RequestRecord, user_id: int, payload: dict[str, Any]) -> None:
        line_index, sku = payload.get("line_index"), payload.get("sku")
        if not isinstance(line_index, int) or not isinstance(sku, str) or not 0 <= line_index < len(record.parsed.lines):
            raise StoreError("Invalid SKU selection")
        line = record.parsed.lines[line_index]
        if sku not in line.candidates:
            raise StoreError("SKU is not an approved candidate")
        catalog = await asyncio.to_thread(self._load_catalog)
        if sku not in catalog.headers:
            raise StoreError("SKU is no longer in the workbook")
        line.sku, line.candidates, line.resolution = sku, [], "human_selected"
        await self._save_and_refresh(record, f"telegram:{user_id}", "sku_selected", {"line_index": line_index, "sku": sku})

    async def _set_purpose(self, record: RequestRecord, user_id: int, payload: dict[str, Any]) -> None:
        purpose = payload.get("purpose")
        if purpose not in _PURPOSES:
            raise StoreError("Invalid purpose")
        record.parsed.purpose = str(purpose)
        await self._save_and_refresh(record, f"telegram:{user_id}", "purpose_selected", {"purpose": purpose})

    async def _save_and_refresh(self, record: RequestRecord, actor: str, action: str, payload: dict[str, Any]) -> None:
        validation = validate_checkout(
            record.parsed,
            max_abs_quantity=self.settings.max_abs_quantity,
            max_items=self.settings.max_items_per_request,
        )
        status = RequestStatus.AWAITING_APPROVAL if validation.ready else RequestStatus.NEEDS_REVIEW
        updated = self.store.replace_analysis(
            request_id=record.request_id,
            parsed=record.parsed,
            validation=validation,
            status=status,
            actor=actor,
            action=action,
            audit_payload={**payload, "status": status.value},
        )
        await self._refresh_preview(updated)

    async def _replace(self, record: RequestRecord, user_id: int) -> None:
        changed = self.store.transition(
            request_id=record.request_id,
            expected=(RequestStatus.NEEDS_REVIEW, RequestStatus.AWAITING_APPROVAL),
            new_status=RequestStatus.SUPERSEDED,
            actor=f"telegram:{user_id}",
            action="request_superseded",
        )
        if changed:
            await self._terminal(record, "Request replaced. No write occurred. Send one corrected full checkout message.")

    async def _cancel(self, record: RequestRecord, user_id: int) -> None:
        changed = self.store.transition(
            request_id=record.request_id,
            expected=(RequestStatus.NEEDS_REVIEW, RequestStatus.AWAITING_APPROVAL),
            new_status=RequestStatus.CANCELLED,
            actor=f"telegram:{user_id}",
            action="request_cancelled",
        )
        if changed:
            await self._terminal(record, f"Inventory request {record.request_id} cancelled. No write occurred.")

    async def _approve_request(self, record: RequestRecord, *, approver_id: int, source: str) -> None:
        if approver_id not in self.settings.approver_user_ids:
            await self.telegram.send_message(chat_id=record.chat_id, reply_to_message_id=record.preview_message_id, text="Approval rejected: approver permission required.")
            return
        current = self.store.get_request(record.request_id)
        if current.status in _TERMINAL or current.status == RequestStatus.WRITING:
            await self.telegram.send_message(chat_id=current.chat_id, reply_to_message_id=current.preview_message_id, text=f"Request is already {current.status.value}.")
            return
        if current.status != RequestStatus.AWAITING_APPROVAL or not current.validation.ready:
            await self.telegram.send_message(chat_id=current.chat_id, reply_to_message_id=current.preview_message_id, text="Approval blocked: resolve every validation flag first.")
            return
        if not self.store.transition(
            request_id=current.request_id,
            expected=(RequestStatus.AWAITING_APPROVAL,),
            new_status=RequestStatus.APPROVED,
            actor=f"telegram:{approver_id}",
            action="request_approved",
            approved_by=approver_id,
            audit_payload={"source": source},
        ):
            return
        if not self.store.transition(
            request_id=current.request_id,
            expected=(RequestStatus.APPROVED,),
            new_status=RequestStatus.WRITING,
            actor="inventory-writer",
            action="write_claimed",
        ):
            return
        await self._terminal(
            current,
            f"Request {current.request_id} approved. " + ("Validation-only dry run in progress." if not self.settings.write_enabled else "Controlled workbook write in progress."),
        )
        if not self.settings.write_enabled:
            self.store.transition(
                request_id=current.request_id,
                expected=(RequestStatus.WRITING,),
                new_status=RequestStatus.DRY_RUN_COMPLETE,
                actor="inventory-writer",
                action="dry_run_completed",
                audit_payload={"workbook_changed": False},
            )
            final = self.store.get_request(current.request_id)
            await self._terminal(final, self._render_completion(final, dry_run=True))
            return

        checkout = WorkbookCheckout(
            request_id=current.request_id,
            timestamp=datetime.now(self.settings.timezone),
            checkout_by=current.parsed.checkout_by or "",
            received_by=current.parsed.received_by or "",
            quote=current.parsed.quote or "",
            name=current.parsed.name or "",
            address=current.parsed.address or "",
            purpose=current.parsed.purpose or "",
            note=current.parsed.note or "",
            lines=tuple(current.validation.canonical_lines),
        )
        try:
            result = await asyncio.to_thread(self.writer.apply, checkout)
        except (WorkbookError, OSError) as exc:
            safe = self._safe_error(exc)
            self.store.transition(
                request_id=current.request_id,
                expected=(RequestStatus.WRITING,),
                new_status=RequestStatus.FAILED,
                actor="inventory-writer",
                action="write_failed",
                error=safe,
                audit_payload={"error_type": type(exc).__name__},
            )
            await self._terminal(self.store.get_request(current.request_id), f"Inventory write failed safely. No unverified result was accepted.\nReason: {safe}")
            return
        self.store.transition(
            request_id=current.request_id,
            expected=(RequestStatus.WRITING,),
            new_status=RequestStatus.COMPLETED,
            actor="inventory-writer",
            action="duplicate_confirmed" if result.duplicate else "write_completed",
            workbook_row=result.row,
            before_sha256=result.before_sha256,
            after_sha256=result.after_sha256,
            backup_path=result.backup_path or None,
            audit_payload={"row": result.row, "duplicate": result.duplicate, "before_sha256": result.before_sha256, "after_sha256": result.after_sha256},
        )
        final = self.store.get_request(current.request_id)
        await self._terminal(final, self._render_completion(final, dry_run=False))

    async def _refresh_preview(self, record: RequestRecord) -> None:
        self.store.invalidate_callbacks(record.request_id)
        keyboard, text = self._build_keyboard(record), self._render_preview(record)
        if record.preview_message_id is not None:
            try:
                await self.telegram.edit_message(chat_id=record.chat_id, message_id=record.preview_message_id, text=text, reply_markup=keyboard)
                return
            except TelegramError:
                pass
        message_id = await self.telegram.send_message(chat_id=record.chat_id, text=text, reply_to_message_id=record.source_message_id, reply_markup=keyboard)
        self.store.set_preview_message(record.request_id, message_id)

    async def _terminal(self, record: RequestRecord, text: str) -> None:
        if record.preview_message_id is not None:
            try:
                await self.telegram.edit_message(chat_id=record.chat_id, message_id=record.preview_message_id, text=text, reply_markup={"inline_keyboard": []})
                return
            except TelegramError:
                pass
        await self.telegram.send_message(chat_id=record.chat_id, text=text, reply_to_message_id=record.source_message_id)

    def _build_keyboard(self, record: RequestRecord) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        if record.status == RequestStatus.NEEDS_REVIEW:
            purpose_added = False
            for flag in record.validation.flags:
                if flag.code == "SKU_AMBIGUOUS" and flag.line_index is not None:
                    for sku in flag.candidates:
                        token = self.store.create_callback(request_id=record.request_id, action="select_sku", payload={"line_index": flag.line_index, "sku": sku}, ttl_minutes=self.settings.callback_ttl_minutes)
                        rows.append([{"text": f"Item {flag.line_index + 1}: {self._short(sku, 42)}", "callback_data": f"inv:{token}"}])
                elif flag.code == "MISSING_FIELD" and flag.field == "purpose" and not purpose_added:
                    purpose_added = True
                    row: list[dict[str, str]] = []
                    for purpose in _PURPOSES:
                        token = self.store.create_callback(request_id=record.request_id, action="set_purpose", payload={"purpose": purpose}, ttl_minutes=self.settings.callback_ttl_minutes)
                        row.append({"text": purpose, "callback_data": f"inv:{token}"})
                        if len(row) == 2:
                            rows.append(row)
                            row = []
                    if row:
                        rows.append(row)
        elif record.status == RequestStatus.AWAITING_APPROVAL:
            token = self.store.create_callback(request_id=record.request_id, action="approve", payload={}, ttl_minutes=self.settings.callback_ttl_minutes)
            rows.append([{"text": "Approve & write" if self.settings.write_enabled else "Approve dry run", "callback_data": f"inv:{token}"}])
        if record.status in {RequestStatus.NEEDS_REVIEW, RequestStatus.AWAITING_APPROVAL}:
            replace = self.store.create_callback(request_id=record.request_id, action="replace", payload={}, ttl_minutes=self.settings.callback_ttl_minutes)
            cancel = self.store.create_callback(request_id=record.request_id, action="cancel", payload={}, ttl_minutes=self.settings.callback_ttl_minutes)
            rows.append([{"text": "Replace", "callback_data": f"inv:{replace}"}, {"text": "Cancel", "callback_data": f"inv:{cancel}"}])
        return {"inline_keyboard": rows}

    def _render_preview(self, record: RequestRecord) -> str:
        parsed = record.parsed
        heading = "APPROVAL REQUIRED" if record.validation.ready else "REVIEW REQUIRED — WRITE BLOCKED"
        output = [
            f"Inventory checkout — {heading}", f"Request: {record.request_id}", "",
            f"Checkout By: {parsed.checkout_by or 'MISSING'}", f"Received By: {parsed.received_by or 'MISSING'}",
            f"Customer: {parsed.name or 'MISSING'}", f"Address: {parsed.address or 'MISSING'}",
            f"Quote: {parsed.quote or '—'}", f"Purpose: {parsed.purpose or 'MISSING'}", "", "Items:",
        ]
        for index, item in enumerate(parsed.lines, start=1):
            output.append(f"{index}. {self._quantity(item.quantity)} {item.sku or item.raw_label}{'' if item.sku else ' [UNRESOLVED]'}")
        if not parsed.lines:
            output.append("MISSING")
        if record.validation.flags:
            output.extend(["", "Flags:"])
            output.extend(f"{index}. {flag.message}" for index, flag in enumerate(record.validation.flags, start=1))
        else:
            output.extend(["", "Validation passed. Duplicate SKU lines were combined.", "An approver must press the button or reply exactly APPROVED." if self.settings.write_enabled else "Writes are disabled: approval completes a dry run only."])
        output.extend(["", "No workbook write has occurred."])
        return self._short("\n".join(output), 4000)

    def _render_completion(self, record: RequestRecord, *, dry_run: bool) -> str:
        items = [f"• {self._quantity(line.quantity)} {line.sku}" for line in record.validation.canonical_lines]
        if dry_run:
            return "\n".join(["Inventory checkout — DRY RUN COMPLETE", f"Request: {record.request_id}", "Workbook changed: NO", "", *items, "", "Enable writes only after testing a workbook copy in desktop Excel."])
        duplicate = " (existing idempotent request)" if record.before_sha256 == record.after_sha256 else ""
        return "\n".join(["Inventory checkout — SAVED AND VERIFIED", f"Request: {record.request_id}", f"Workbook row: {record.workbook_row}{duplicate}", f"Before: {(record.before_sha256 or '')[:12]}", f"After: {(record.after_sha256 or '')[:12]}", "", *items, "", "The XLSM reopened and the row was read back; all non-target members, including VBA, were hash-checked."])

    def _load_catalog(self) -> SkuCatalog:
        return SkuCatalog.from_json_file(self.writer.inspect().sku_headers, self.settings.sku_aliases_path)

    async def _notify_processing_error(self, update: dict[str, Any], exc: Exception) -> None:
        message = update.get("message")
        callback = update.get("callback_query")
        if isinstance(message, dict):
            chat_id, reply_id = self._int_at(message, "chat", "id"), message.get("message_id")
        elif isinstance(callback, dict) and isinstance(callback.get("message"), dict):
            chat_id, reply_id = self._int_at(callback["message"], "chat", "id"), callback["message"].get("message_id")
        else:
            return
        if chat_id in self.settings.allowed_chat_ids:
            try:
                await self.telegram.send_message(chat_id=chat_id, reply_to_message_id=reply_id if isinstance(reply_id, int) else None, text=f"Inventory workflow stopped safely. No unverified write was accepted.\nReason: {self._safe_error(exc)}")
            except TelegramError:
                pass

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc).replace(str(self.settings.workbook_path), self.settings.workbook_path.name).replace(str(Path.home()), "~")
        return self._short(f"{type(exc).__name__}: {' '.join(message.split())}", 300)

    @staticmethod
    def _display_name(user: Any) -> str:
        if not isinstance(user, dict):
            return "Telegram User"
        name = " ".join(str(user.get(key, "")).strip() for key in ("first_name", "last_name") if str(user.get(key, "")).strip())
        return name or (f"@{user['username']}" if user.get("username") else "Telegram User")

    @staticmethod
    def _int_at(value: Any, *path: str) -> int | None:
        current = value
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current if isinstance(current, int) else None

    @staticmethod
    def _quantity(value: Decimal) -> str:
        rendered = format(abs(value), "f").rstrip("0").rstrip(".") or "0"
        return f"RETURN {rendered}x" if value < 0 else f"{rendered}x"

    @staticmethod
    def _short(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"
