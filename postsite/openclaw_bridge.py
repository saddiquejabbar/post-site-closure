from __future__ import annotations

import argparse
import html
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .dmr import parse_dmr
from .scheduling import site_target_dt
from .service import ClosureService
from .store import Store
from .telegram_flow import schedule_preview_text


def _plain(value: str) -> str:
    return html.unescape(value.replace("<b>", "").replace("</b>", ""))


def _preview(sites, include_instruction: bool = True) -> str:
    return _plain(
        schedule_preview_text(
            sites,
            settings.post_site_time,
            settings.staff_name,
            include_instruction=include_instruction,
        )
    )


def _error(reply: str, code: str, terminal: bool = False) -> dict[str, Any]:
    return {
        "handled": True,
        "ok": False,
        "terminal": terminal,
        "error_code": code,
        "reply": reply,
    }


def _identity(payload: dict[str, Any]) -> tuple[str, int] | None:
    group_id = str(payload.get("group_id", "")).strip()
    try:
        sender_id = int(str(payload.get("sender_id", "")).strip())
    except ValueError:
        return None
    return (group_id, sender_id) if group_id else None


def _authorized_draft(store: Store, payload: dict[str, Any]):
    identity = _identity(payload)
    try:
        draft_id = int(payload.get("review_id"))
    except (TypeError, ValueError):
        return None, None, _error(
            "This post-site review is no longer active.",
            "invalid_review",
            terminal=True,
        )
    if identity is None:
        return None, None, _error("Not authorized.", "unauthorized")
    group_id, sender_id = identity
    draft = store.get_draft(draft_id)
    if not draft or draft.chat_id != group_id or draft.requested_by != sender_id:
        return None, None, _error("Not authorized.", "unauthorized")
    return draft, identity, None


def handle(command: str, payload: dict[str, Any], store: Store) -> dict[str, Any]:
    if command == "review":
        identity = _identity(payload)
        source = str(payload.get("source_text", ""))
        if identity is None:
            return _error("Not authorized.", "unauthorized")
        sites = parse_dmr(source)
        if not sites:
            return _error("I could not find any DMR site entries in that message.", "invalid_dmr")
        group_id, sender_id = identity
        draft = store.create_draft(group_id, sender_id, source, sites)
        return {
            "handled": True,
            "ok": True,
            "review_id": str(draft.id),
            "can_approve": True,
            "reply": _preview(draft.sites),
        }

    draft, identity, error = _authorized_draft(store, payload)
    if error:
        return error
    assert draft is not None and identity is not None
    group_id, sender_id = identity

    if command == "prepare":
        if draft.status != "pending":
            return _error(
                f"This post-site review is already {draft.status}.",
                "inactive_review",
                terminal=draft.status not in {"pending", "scheduling"},
            )
        claimed = store.claim_draft_for_scheduling(draft.id, group_id, sender_id)
        if not claimed:
            return _error("This post-site review is no longer active.", "claim_failed")
        tz = ZoneInfo(settings.timezone)
        service = ClosureService(store)
        jobs = []
        for index, site in enumerate(claimed.sites, start=1):
            jobs.append(
                {
                    "key": f"post-site:{claimed.id}:{index}",
                    "name": f"Post-site {claimed.dmr_date} #{index} {site.deal_name}"[:120],
                    "at": site_target_dt(site, tz, settings.post_site_time).isoformat(),
                    "prompt": _plain(service.prompt_text(site)),
                }
            )
        return {
            "handled": True,
            "ok": True,
            "review_id": str(claimed.id),
            "jobs": jobs,
            "reply": "Preparing approved post-site schedule.",
        }

    if command == "commit":
        if draft.status != "scheduling":
            return _error("This post-site review is no longer active.", "commit_failed", terminal=True)
        raw_job_ids = payload.get("job_ids", [])
        if not isinstance(raw_job_ids, list) or not raw_job_ids or not all(
            isinstance(value, str) and value for value in raw_job_ids
        ):
            return _error("The approved schedule could not be verified.", "invalid_jobs")
        approved = store.finalize_openclaw_schedule(draft.id, raw_job_ids)
        if not approved:
            return _error("The approved schedule could not be saved.", "commit_failed")
        return {
            "handled": True,
            "ok": True,
            "review_id": str(approved.id),
            "reply": (
                f"{_preview(approved.sites, include_instruction=False)}\n\n"
                f"✅ APPROVED — {len(raw_job_ids)} installer check-in(s) scheduled.\n"
                "CRM remains draft-only."
            ),
        }

    if command == "rollback":
        rolled_back = store.rollback_openclaw_schedule(draft.id)
        return {
            "handled": True,
            "ok": rolled_back is not None,
            "review_id": str(draft.id),
            "reply": "Scheduling failed safely. Nothing was approved; you can try again.",
        }

    if command == "revise":
        if draft.status != "pending" or not store.update_draft_status(draft.id, "revising"):
            return _error("This post-site review is no longer active.", "inactive_review", terminal=True)
        return {
            "handled": True,
            "ok": True,
            "review_id": str(draft.id),
            "reply": f"{_preview(draft.sites, include_instruction=False)}\n\n✏️ REVISION REQUESTED — paste the corrected DMR and tag me.",
        }

    if command == "cancel":
        if draft.status != "pending" or not store.update_draft_status(draft.id, "cancelled"):
            return _error("This post-site review is no longer active.", "inactive_review", terminal=True)
        return {
            "handled": True,
            "ok": True,
            "review_id": str(draft.id),
            "reply": f"{_preview(draft.sites, include_instruction=False)}\n\n❌ CANCELLED — no check-ins were scheduled.",
        }

    return _error("Unsupported post-site action.", "unsupported")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["review", "prepare", "commit", "rollback", "revise", "cancel"])
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError
        settings.ensure_data_dir()
        result = handle(args.command, payload, Store(settings.db_path))
    except Exception:
        result = _error("post-site could not complete this request safely.", "internal_error")
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
