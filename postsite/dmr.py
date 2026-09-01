from __future__ import annotations

import re
from datetime import datetime

from .models import Site

ENTRY_RE = re.compile(r"^([1-6](?:\+[1-6])*)\.\s+(.+?)\s*$")
DATE_RE = re.compile(r"Daily Meeting Report\s*[—-]\s*(\d{4}-\d{2}-\d{2})", re.I)
LOCATION_RE = re.compile(r"^📍\s*(.+)$")
URL_RE = re.compile(r"https?://\S+")
TIME_SUFFIX_RE = re.compile(r"\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*$", re.I)


def _split_scheduled_time(deal_name: str) -> tuple[str, str]:
    """Strip a trailing '- 10:30am' style appointment time off a deal name.

    Returns (clean_deal_name, "HH:MM" in 24h form or "" if no time found).
    """
    match = TIME_SUFFIX_RE.search(deal_name)
    if not match:
        return deal_name, ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3).lower()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return deal_name, ""
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    clean = deal_name[: match.start()].rstrip(" -").strip()
    return clean, f"{hour:02d}:{minute:02d}"


def normalize_visit_type(activity: str) -> str:
    a = activity.lower()
    if "servic" in a:
        return "service"
    if "handover" in a and "install" not in a:
        return "handover"
    if "install" in a:
        return "installation"
    if "deliver" in a:
        return "delivery"
    if any(x in a for x in ("site discussion", "site meeting", "site visit", "assessment", "assess")):
        return "assessment"
    if any(x in a for x in ("test", "testing", "compatibility")):
        return "testing"
    return "other"


def _clean_work_line(line: str, section: str = "") -> tuple[str, str]:
    line = line.strip().lstrip("-•").strip()
    if not line:
        return "", section
    if line.endswith(":") and len(line.split()) <= 4:
        return "", line[:-1].strip()
    if section:
        return f"{section}: {line}", section
    return line, section


def parse_dmr(text: str, fallback_date: str | None = None) -> list[Site]:
    """Parse the compact Daily Meeting Report format into site records."""
    date_match = DATE_RE.search(text)
    dmr_date = date_match.group(1) if date_match else (fallback_date or datetime.now().date().isoformat())

    lines = [ln.rstrip() for ln in text.splitlines()]
    sites: list[Site] = []
    current: Site | None = None
    section = ""

    def flush() -> None:
        nonlocal current
        if current:
            current.payment_required = any("collect payment" in x.lower() for x in current.work_items)
            sites.append(current)
            current = None

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("📋"):
            continue

        entry = ENTRY_RE.match(line)
        if entry:
            flush()
            staff_numbers = [int(x) for x in entry.group(1).split("+")]
            deal_name, scheduled_time = _split_scheduled_time(entry.group(2).strip())
            current = Site(
                dmr_date=dmr_date,
                scheduled_time=scheduled_time,
                staff_numbers=staff_numbers,
                deal_name=deal_name,
            )
            section = ""
            continue

        if current is None:
            continue

        location_match = LOCATION_RE.match(line)
        if location_match:
            parts = [p.strip() for p in location_match.group(1).split("|")]
            current.location = parts[0] if parts else ""
            current.activity_raw = parts[1] if len(parts) > 1 else ""
            current.flags = parts[2:] if len(parts) > 2 else []
            current.visit_type = normalize_visit_type(current.activity_raw)
            continue

        if "proposal" in line.lower() and URL_RE.search(line):
            current.proposal_url = URL_RE.search(line).group(0)
            continue

        work, section = _clean_work_line(line, section)
        if work:
            current.work_items.append(work)

    flush()
    return sites
