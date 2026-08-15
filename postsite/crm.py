from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import settings
from .models import Site
from .questions import work_summary


def build_closure_note(site: Site) -> str:
    r = site.responses
    label = {
        "installation": "Installation",
        "service": "Servicing",
        "handover": "Handover",
        "delivery": "Delivery",
        "assessment": "Site assessment",
        "testing": "Testing",
        "other": site.activity_raw or "Site visit",
    }.get(site.visit_type, site.activity_raw or "Site visit")

    planned = work_summary(site, max_items=50)
    if planned:
        lines = [f"{label}: {planned}."]
    elif site.activity_raw and site.activity_raw.strip().lower() not in {label.lower(), "servicing", "install", "installation", "handover"}:
        lines = [f"{label}: {site.activity_raw}."]
    else:
        lines = [f"{label}."]

    if r.get("completed") is True:
        lines.append("Completed as planned.")
    elif r.get("completed") is False:
        lines.append("Not fully completed.")

    for key, title in (
        ("resolution", "Result"),
        ("result", "Result"),
        ("outcome", "Outcome"),
        ("delivery_result", "Delivery"),
        ("recipient", "Received by"),
        ("outstanding", "Outstanding"),
        ("next_action", "Next action"),
    ):
        if r.get(key):
            lines.append(f"{title}: {r[key]}")

    if site.payment_required:
        if r.get("payment_collected") is True:
            lines.append("Payment: Collected.")
        elif r.get("payment_collected") is False:
            note = f" — {r.get('payment_note')}" if r.get("payment_note") else ""
            lines.append(f"Payment: Not collected{note}.")
        elif r.get("payment_note"):
            lines.append(f"Payment: {r['payment_note']}")

    return " ".join(line if line.rstrip().endswith((".", "!", "?")) else line + "." for line in lines)


def build_crm_payload(site: Site) -> dict:
    completed = site.responses.get("completed")
    return {
        "source": "post-site-closure-agent",
        "dmr_date": site.dmr_date,
        "site_id": site.id,
        "deal_name": site.deal_name,
        "location": site.location,
        "visit_type": site.visit_type,
        "activity": site.activity_raw,
        "assigned_staff": [settings.staff_name(n) for n in site.staff_numbers],
        "planned_work": site.work_items,
        "proposal_url": site.proposal_url,
        "completed": completed,
        "needs_follow_up": completed is False or bool(site.responses.get("outstanding")),
        "payment_required": site.payment_required,
        "payment_collected": site.responses.get("payment_collected"),
        "closure_note": build_closure_note(site),
        "raw_responses": site.responses,
    }


def post_to_webhook(site: Site, url: str, timeout: int = 10) -> tuple[bool, str]:
    if not url:
        return False, "CRM_WEBHOOK_URL not configured"
    body = json.dumps(build_crm_payload(site)).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except URLError as exc:
        return False, str(exc)
