from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from inventory_types import ParsedRequest


class RequestStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                  public_id TEXT PRIMARY KEY,
                  update_id INTEGER UNIQUE NOT NULL,
                  chat_id INTEGER NOT NULL,
                  message_id INTEGER NOT NULL,
                  requester_id INTEGER,
                  raw_text TEXT NOT NULL,
                  proposal_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  review_message_id INTEGER,
                  approved_by INTEGER,
                  receipt_json TEXT,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
                """
            )
        self.recover_stale()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def create(
        self,
        update_id: int,
        chat_id: int,
        message_id: int,
        requester_id: int | None,
        raw_text: str,
        proposal: ParsedRequest,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        status = "pending_approval" if proposal.ready else "flagged"
        with self.connect() as db:
            old = db.execute(
                "SELECT * FROM requests WHERE update_id=?", (update_id,)
            ).fetchone()
            if old:
                return row_dict(old), False
            for _ in range(10):
                public_id = "INV-" + secrets.token_hex(4).upper()
                try:
                    db.execute(
                        """
                        INSERT INTO requests(
                          public_id,update_id,chat_id,message_id,requester_id,
                          raw_text,proposal_json,status,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            public_id,
                            update_id,
                            chat_id,
                            message_id,
                            requester_id,
                            raw_text,
                            json.dumps(proposal.to_dict()),
                            status,
                            now,
                            now,
                        ),
                    )
                    row = db.execute(
                        "SELECT * FROM requests WHERE public_id=?", (public_id,)
                    ).fetchone()
                    return row_dict(row), True
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("could not allocate request ID")

    def get(self, public_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE public_id=?", (public_id,)
            ).fetchone()
            return row_dict(row) if row else None

    def set_review_message(self, public_id: str, message_id: int) -> None:
        self.update(public_id, review_message_id=message_id)

    def save_proposal(self, public_id: str, proposal: ParsedRequest) -> None:
        self.update(
            public_id,
            proposal_json=json.dumps(proposal.to_dict()),
            status="pending_approval" if proposal.ready else "flagged",
            error=None,
        )

    def claim(self, public_id: str, approver: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE requests
                SET status='writing',approved_by=?,updated_at=?,error=NULL
                WHERE public_id=? AND status IN ('pending_approval','failed')
                """,
                (approver, utc_now(), public_id),
            )
            return cursor.rowcount == 1

    def committed(self, public_id: str, receipt: dict[str, Any]) -> None:
        self.update(
            public_id,
            status="committed",
            receipt_json=json.dumps(receipt),
            error=None,
        )

    def failed(self, public_id: str, error: str) -> None:
        self.update(public_id, status="failed", error=error[:1600])

    def reject(self, public_id: str, approver: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE requests
                SET status='rejected',approved_by=?,updated_at=?
                WHERE public_id=? AND status IN ('flagged','pending_approval','failed')
                """,
                (approver, utc_now(), public_id),
            )
            return cursor.rowcount == 1

    def recover_stale(self, minutes: int = 15) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).isoformat()
        with self.connect() as db:
            db.execute(
                """
                UPDATE requests
                SET status='failed',
                    error='Recovered stale write; retry checks workbook row IDs.',
                    updated_at=?
                WHERE status='writing' AND updated_at<?
                """,
                (utc_now(), cutoff),
            )

    def update(self, public_id: str, **values: Any) -> None:
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE requests SET {assignments} WHERE public_id=?",
                [*values.values(), public_id],
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    parsed = json.loads(value.pop("proposal_json"))
    value["proposal"] = parsed
    value["parsed"] = parsed
    value["created_at_utc"] = value["created_at"]
    value["updated_at_utc"] = value["updated_at"]
    value["receipt"] = (
        json.loads(value["receipt_json"]) if value.get("receipt_json") else None
    )
    return value
