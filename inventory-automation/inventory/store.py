from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import ParsedCheckout, RequestStatus, ValidationResult


class StoreError(RuntimeError):
    pass


class RequestNotFound(StoreError):
    pass


class CallbackError(StoreError):
    pass


@dataclass(slots=True, frozen=True)
class RequestRecord:
    request_id: str
    source_update_id: int
    chat_id: int
    user_id: int
    source_message_id: int
    preview_message_id: int | None
    original_text: str
    parsed: ParsedCheckout
    validation: ValidationResult
    status: RequestStatus
    approved_by: int | None
    workbook_row: int | None
    before_sha256: str | None
    after_sha256: str | None
    backup_path: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True, frozen=True)
class CallbackRecord:
    token: str
    request_id: str
    action: str
    payload: dict[str, Any]
    expires_at: str


class InventoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_updates(
                    update_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('processing','done','failed')),
                    error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY,
                    source_update_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    preview_message_id INTEGER,
                    original_text TEXT NOT NULL,
                    parsed_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_by INTEGER,
                    workbook_row INTEGER,
                    before_sha256 TEXT,
                    after_sha256 TEXT,
                    backup_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_requests_preview ON requests(chat_id,preview_message_id);
                CREATE TABLE IF NOT EXISTS callback_actions(
                    token TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_request_id() -> str:
        return f"inv_{secrets.token_hex(8)}"

    def begin_update(self, update_id: int) -> bool:
        with self._transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM inbound_updates WHERE update_id=?", (update_id,)).fetchone():
                return False
            connection.execute(
                "INSERT INTO inbound_updates(update_id,state,updated_at) VALUES(?,'processing',?)",
                (update_id, self._now()),
            )
        return True

    def finish_update(self, update_id: int, *, error: str | None = None) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE inbound_updates SET state=?,error=?,updated_at=? WHERE update_id=?",
                ("failed" if error else "done", error, self._now(), update_id),
            )

    def create_request(
        self,
        *,
        request_id: str,
        source_update_id: int,
        chat_id: int,
        user_id: int,
        source_message_id: int,
        original_text: str,
        parsed: ParsedCheckout,
        validation: ValidationResult,
        status: RequestStatus,
    ) -> RequestRecord:
        now = self._now()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO requests(
                request_id,source_update_id,chat_id,user_id,source_message_id,original_text,
                parsed_json,validation_json,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id, source_update_id, chat_id, user_id, source_message_id, original_text,
                    json.dumps(parsed.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(validation.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    status.value, now, now,
                ),
            )
            self._append_audit_locked(
                connection, request_id=request_id, actor=f"telegram:{user_id}", action="request_created",
                payload={"status": status.value, "source_update_id": source_update_id},
            )
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> RequestRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise RequestNotFound(request_id)
        return self._request_from_row(row)

    def find_by_preview_message(self, *, chat_id: int, preview_message_id: int) -> RequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE chat_id=? AND preview_message_id=? ORDER BY created_at DESC LIMIT 1",
                (chat_id, preview_message_id),
            ).fetchone()
        return self._request_from_row(row) if row else None

    def set_preview_message(self, request_id: str, preview_message_id: int) -> None:
        with self._transaction(immediate=True) as connection:
            count = connection.execute(
                "UPDATE requests SET preview_message_id=?,updated_at=? WHERE request_id=?",
                (preview_message_id, self._now(), request_id),
            ).rowcount
            if count != 1:
                raise RequestNotFound(request_id)

    def replace_analysis(
        self,
        *,
        request_id: str,
        parsed: ParsedCheckout,
        validation: ValidationResult,
        status: RequestStatus,
        actor: str,
        action: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> RequestRecord:
        with self._transaction(immediate=True) as connection:
            row = connection.execute("SELECT status FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise RequestNotFound(request_id)
            if RequestStatus(row["status"]) not in {RequestStatus.NEEDS_REVIEW, RequestStatus.AWAITING_APPROVAL}:
                raise StoreError("Request is no longer editable")
            connection.execute(
                """UPDATE requests SET parsed_json=?,validation_json=?,status=?,error=NULL,updated_at=?
                WHERE request_id=?""",
                (
                    json.dumps(parsed.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(validation.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    status.value, self._now(), request_id,
                ),
            )
            self._invalidate_callbacks_locked(connection, request_id)
            self._append_audit_locked(connection, request_id=request_id, actor=actor, action=action, payload=audit_payload or {"status": status.value})
        return self.get_request(request_id)

    def transition(
        self,
        *,
        request_id: str,
        expected: Sequence[RequestStatus],
        new_status: RequestStatus,
        actor: str,
        action: str,
        approved_by: int | None = None,
        workbook_row: int | None = None,
        before_sha256: str | None = None,
        after_sha256: str | None = None,
        backup_path: str | None = None,
        error: str | None = None,
        audit_payload: dict[str, Any] | None = None,
    ) -> bool:
        expected_values = tuple(item.value for item in expected)
        if not expected_values:
            raise ValueError("Expected statuses required")
        placeholders = ",".join("?" for _ in expected_values)
        with self._transaction(immediate=True) as connection:
            count = connection.execute(
                f"""UPDATE requests SET status=?,approved_by=COALESCE(?,approved_by),
                workbook_row=COALESCE(?,workbook_row),before_sha256=COALESCE(?,before_sha256),
                after_sha256=COALESCE(?,after_sha256),backup_path=COALESCE(?,backup_path),
                error=?,updated_at=? WHERE request_id=? AND status IN ({placeholders})""",
                (
                    new_status.value, approved_by, workbook_row, before_sha256, after_sha256,
                    backup_path, error, self._now(), request_id, *expected_values,
                ),
            ).rowcount
            if count != 1:
                return False
            if new_status not in {RequestStatus.NEEDS_REVIEW, RequestStatus.AWAITING_APPROVAL}:
                self._invalidate_callbacks_locked(connection, request_id)
            self._append_audit_locked(connection, request_id=request_id, actor=actor, action=action, payload=audit_payload or {"status": new_status.value})
        return True

    def create_callback(self, *, request_id: str, action: str, payload: dict[str, Any], ttl_minutes: int) -> str:
        token = secrets.token_urlsafe(12)
        now = datetime.now(timezone.utc)
        with self._transaction(immediate=True) as connection:
            if not connection.execute("SELECT 1 FROM requests WHERE request_id=?", (request_id,)).fetchone():
                raise RequestNotFound(request_id)
            connection.execute(
                "INSERT INTO callback_actions(token,request_id,action,payload_json,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    token, request_id, action, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    (now + timedelta(minutes=ttl_minutes)).isoformat(), now.isoformat(),
                ),
            )
        return token

    def get_callback(self, token: str) -> CallbackRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM callback_actions WHERE token=?", (token,)).fetchone()
        return self._validate_callback_row(row)

    def consume_callback(self, token: str) -> CallbackRecord:
        now = datetime.now(timezone.utc)
        with self._transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM callback_actions WHERE token=?", (token,)).fetchone()
            record = self._validate_callback_row(row, now=now)
            count = connection.execute(
                "UPDATE callback_actions SET used_at=? WHERE token=? AND used_at IS NULL",
                (now.isoformat(), token),
            ).rowcount
            if count != 1:
                raise CallbackError("This action was already used")
            return record

    def invalidate_callbacks(self, request_id: str) -> None:
        with self._transaction(immediate=True) as connection:
            self._invalidate_callbacks_locked(connection, request_id)

    @staticmethod
    def _validate_callback_row(row: sqlite3.Row | None, *, now: datetime | None = None) -> CallbackRecord:
        if row is None:
            raise CallbackError("Unknown action")
        if row["used_at"] is not None:
            raise CallbackError("This action was already used")
        now = now or datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError as exc:
            raise CallbackError("Invalid action expiry") from exc
        if expires <= now:
            raise CallbackError("This action expired")
        return CallbackRecord(row["token"], row["request_id"], row["action"], json.loads(row["payload_json"]), row["expires_at"])

    @staticmethod
    def _invalidate_callbacks_locked(connection: sqlite3.Connection, request_id: str) -> None:
        connection.execute(
            "UPDATE callback_actions SET used_at=? WHERE request_id=? AND used_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), request_id),
        )

    def _append_audit_locked(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str | None,
        actor: str,
        action: str,
        payload: dict[str, Any],
    ) -> str:
        previous = connection.execute("SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        timestamp = self._now()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical = json.dumps(
            {"timestamp": timestamp, "request_id": request_id, "actor": actor, "action": action, "payload": json.loads(payload_json), "previous_hash": previous_hash},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        event_hash = hashlib.sha256(canonical).hexdigest()
        connection.execute(
            "INSERT INTO audit_events(timestamp,request_id,actor,action,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?)",
            (timestamp, request_id, actor, action, payload_json, previous_hash, event_hash),
        )
        return event_hash

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            request_id=row["request_id"], source_update_id=row["source_update_id"], chat_id=row["chat_id"],
            user_id=row["user_id"], source_message_id=row["source_message_id"], preview_message_id=row["preview_message_id"],
            original_text=row["original_text"], parsed=ParsedCheckout.from_dict(json.loads(row["parsed_json"])),
            validation=ValidationResult.from_dict(json.loads(row["validation_json"])), status=RequestStatus(row["status"]),
            approved_by=row["approved_by"], workbook_row=row["workbook_row"], before_sha256=row["before_sha256"],
            after_sha256=row["after_sha256"], backup_path=row["backup_path"], error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
