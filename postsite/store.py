from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import DmrDraft, Site


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self.connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dmr_date TEXT NOT NULL,
                    scheduled_time TEXT NOT NULL DEFAULT '',
                    staff_numbers TEXT NOT NULL,
                    deal_name TEXT NOT NULL,
                    location TEXT,
                    activity_raw TEXT,
                    visit_type TEXT,
                    flags TEXT,
                    work_items TEXT,
                    proposal_url TEXT,
                    payment_required INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    prompt_message_id INTEGER,
                    stage TEXT NOT NULL DEFAULT 'primary',
                    responses TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(dmr_date, deal_name, location, activity_raw)
                )
                """
            )
            existing_cols = {row["name"] for row in con.execute("PRAGMA table_info(sites)")}
            if "scheduled_time" not in existing_cols:
                con.execute("ALTER TABLE sites ADD COLUMN scheduled_time TEXT NOT NULL DEFAULT ''")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dmr_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    requested_by INTEGER,
                    dmr_date TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    sites TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    schedule_job_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            draft_cols = {row["name"] for row in con.execute("PRAGMA table_info(dmr_drafts)")}
            if "schedule_job_ids" not in draft_cols:
                con.execute(
                    "ALTER TABLE dmr_drafts "
                    "ADD COLUMN schedule_job_ids TEXT NOT NULL DEFAULT '[]'"
                )

    @staticmethod
    def _upsert_site(con: sqlite3.Connection, site: Site) -> None:
        con.execute(
            """
            INSERT INTO sites (
                dmr_date, scheduled_time, staff_numbers, deal_name, location, activity_raw,
                visit_type, flags, work_items, proposal_url, payment_required,
                status, stage, responses
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dmr_date, deal_name, location, activity_raw) DO UPDATE SET
                scheduled_time=excluded.scheduled_time,
                staff_numbers=excluded.staff_numbers,
                visit_type=excluded.visit_type,
                flags=excluded.flags,
                work_items=excluded.work_items,
                proposal_url=excluded.proposal_url,
                payment_required=excluded.payment_required
            """,
            (
                site.dmr_date,
                site.scheduled_time,
                json.dumps(site.staff_numbers),
                site.deal_name,
                site.location,
                site.activity_raw,
                site.visit_type,
                json.dumps(site.flags),
                json.dumps(site.work_items),
                site.proposal_url,
                int(site.payment_required),
                site.status,
                site.stage,
                json.dumps(site.responses),
            ),
        )

    def upsert_sites(self, sites: list[Site]) -> list[Site]:
        out = []
        with self.connect() as con:
            for site in sites:
                self._upsert_site(con, site)
            con.commit()
        for s in sites:
            row = self.find_unique(s.dmr_date, s.deal_name, s.location, s.activity_raw)
            if row:
                out.append(row)
        return out

    def create_draft(
        self,
        chat_id: int | str,
        requested_by: int | None,
        source_text: str,
        sites: list[Site],
    ) -> DmrDraft:
        if not sites:
            raise ValueError("A DMR draft must contain at least one site")
        payload = json.dumps([site.to_dict() for site in sites])
        with self.connect() as con:
            con.execute(
                "UPDATE dmr_drafts SET status='replaced' "
                "WHERE chat_id=? AND status IN ('pending', 'revising')",
                (str(chat_id),),
            )
            cursor = con.execute(
                """
                INSERT INTO dmr_drafts (
                    chat_id, requested_by, dmr_date, source_text, sites, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (str(chat_id), requested_by, sites[0].dmr_date, source_text, payload),
            )
            draft_id = int(cursor.lastrowid)
            con.commit()
        draft = self.get_draft(draft_id)
        if not draft:
            raise RuntimeError("Could not reload the saved DMR draft")
        return draft

    def get_draft(self, draft_id: int) -> DmrDraft | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM dmr_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._draft_row(row) if row else None

    def update_draft_status(self, draft_id: int, status: str) -> DmrDraft | None:
        allowed = {
            "pending",
            "revising",
            "scheduling",
            "approved",
            "cancelled",
            "replaced",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported draft status: {status}")
        with self.connect() as con:
            changed = con.execute(
                "UPDATE dmr_drafts SET status=? WHERE id=? AND status='pending'",
                (status, draft_id),
            ).rowcount
            con.commit()
        return self.get_draft(draft_id) if changed else None

    def claim_draft_for_scheduling(
        self,
        draft_id: int,
        chat_id: int | str,
        requested_by: int,
    ) -> DmrDraft | None:
        """Claim one authorized pending draft so only one approval can schedule it."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT * FROM dmr_drafts
                WHERE id=? AND chat_id=? AND requested_by=? AND status='pending'
                """,
                (draft_id, str(chat_id), requested_by),
            ).fetchone()
            if not row:
                return None
            changed = con.execute(
                "UPDATE dmr_drafts SET status='scheduling' "
                "WHERE id=? AND status='pending'",
                (draft_id,),
            ).rowcount
            if changed != 1:
                con.rollback()
                return None
            con.commit()
        return self.get_draft(draft_id)

    def finalize_openclaw_schedule(
        self,
        draft_id: int,
        job_ids: list[str],
    ) -> DmrDraft | None:
        with self.connect() as con:
            changed = con.execute(
                """
                UPDATE dmr_drafts
                SET status='approved', schedule_job_ids=?
                WHERE id=? AND status='scheduling'
                """,
                (json.dumps(job_ids), draft_id),
            ).rowcount
            con.commit()
        return self.get_draft(draft_id) if changed else None

    def rollback_openclaw_schedule(self, draft_id: int) -> DmrDraft | None:
        with self.connect() as con:
            changed = con.execute(
                "UPDATE dmr_drafts SET status='pending', schedule_job_ids='[]' "
                "WHERE id=? AND status='scheduling'",
                (draft_id,),
            ).rowcount
            con.commit()
        return self.get_draft(draft_id) if changed else None

    def approve_draft(self, draft_id: int) -> tuple[DmrDraft, list[Site]] | None:
        """Atomically approve a draft and add its sites to the live schedule."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM dmr_drafts WHERE id=? AND status='pending'",
                (draft_id,),
            ).fetchone()
            if not row:
                return None
            draft = self._draft_row(row)
            for site in draft.sites:
                self._upsert_site(con, site)
            changed = con.execute(
                "UPDATE dmr_drafts SET status='approved' "
                "WHERE id=? AND status='pending'",
                (draft_id,),
            ).rowcount
            if changed != 1:
                con.rollback()
                return None
            con.commit()

        approved = self.get_draft(draft_id)
        if not approved:
            raise RuntimeError("Could not reload the approved DMR draft")
        live_sites = []
        for site in approved.sites:
            stored = self.find_unique(
                site.dmr_date, site.deal_name, site.location, site.activity_raw
            )
            if stored:
                live_sites.append(stored)
        return approved, live_sites

    def find_unique(self, dmr_date: str, deal_name: str, location: str, activity_raw: str) -> Site | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM sites WHERE dmr_date=? AND deal_name=? AND location=? AND activity_raw=?",
                (dmr_date, deal_name, location, activity_raw),
            ).fetchone()
        return self._row(row) if row else None

    def get(self, site_id: int) -> Site | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        return self._row(row) if row else None

    def by_prompt_message(self, message_id: int) -> Site | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM sites WHERE prompt_message_id=?", (message_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, dmr_date: str | None = None, status: str | None = None) -> list[Site]:
        sql = "SELECT * FROM sites WHERE 1=1"
        params: list[object] = []
        if dmr_date:
            sql += " AND dmr_date=?"
            params.append(dmr_date)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def update(self, site_id: int, **changes) -> Site:
        allowed = {"status", "prompt_message_id", "stage", "responses"}
        clean = {k: v for k, v in changes.items() if k in allowed}
        if not clean:
            site = self.get(site_id)
            if not site:
                raise KeyError(site_id)
            return site
        keys, values = [], []
        for k, v in clean.items():
            keys.append(f"{k}=?")
            if k == "responses":
                v = json.dumps(v)
            values.append(v)
        values.append(site_id)
        with self.connect() as con:
            con.execute(f"UPDATE sites SET {', '.join(keys)} WHERE id=?", values)
            con.commit()
        site = self.get(site_id)
        if not site:
            raise KeyError(site_id)
        return site

    @staticmethod
    def _row(row: sqlite3.Row) -> Site:
        return Site(
            id=row["id"],
            dmr_date=row["dmr_date"],
            scheduled_time=row["scheduled_time"] or "",
            staff_numbers=json.loads(row["staff_numbers"]),
            deal_name=row["deal_name"],
            location=row["location"] or "",
            activity_raw=row["activity_raw"] or "",
            visit_type=row["visit_type"] or "other",
            flags=json.loads(row["flags"] or "[]"),
            work_items=json.loads(row["work_items"] or "[]"),
            proposal_url=row["proposal_url"] or "",
            payment_required=bool(row["payment_required"]),
            status=row["status"],
            prompt_message_id=row["prompt_message_id"],
            stage=row["stage"],
            responses=json.loads(row["responses"] or "{}"),
        )

    @staticmethod
    def _draft_row(row: sqlite3.Row) -> DmrDraft:
        return DmrDraft(
            id=row["id"],
            chat_id=row["chat_id"],
            requested_by=row["requested_by"],
            dmr_date=row["dmr_date"],
            source_text=row["source_text"],
            sites=[Site(**item) for item in json.loads(row["sites"] or "[]")],
            status=row["status"],
            schedule_job_ids=json.loads(row["schedule_job_ids"] or "[]"),
            created_at=row["created_at"],
        )
