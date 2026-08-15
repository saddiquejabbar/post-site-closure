from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Site:
    id: int | None = None
    dmr_date: str = ""
    scheduled_time: str = ""
    staff_numbers: list[int] = field(default_factory=list)
    deal_name: str = ""
    location: str = ""
    activity_raw: str = ""
    visit_type: str = "other"
    flags: list[str] = field(default_factory=list)
    work_items: list[str] = field(default_factory=list)
    proposal_url: str = ""
    payment_required: bool = False
    status: str = "pending"
    prompt_message_id: int | None = None
    stage: str = "primary"
    responses: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DmrDraft:
    id: int
    chat_id: str
    requested_by: int | None
    dmr_date: str
    source_text: str
    sites: list[Site] = field(default_factory=list)
    status: str = "pending"
    schedule_job_ids: list[str] = field(default_factory=list)
    created_at: str = ""
