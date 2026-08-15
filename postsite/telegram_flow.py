from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Callable

from .models import Site


def is_run_post_site(text: str, bot_username: str = "") -> bool:
    """Accept the natural workflow trigger with harmless spacing variations."""
    normalized = text.casefold()
    if bot_username:
        normalized = normalized.replace(f"@{bot_username.casefold()}", " ")
    normalized = re.sub(r"[-_\s]+", " ", normalized).strip(" .!,:;")
    return normalized == "run post site"


def looks_like_dmr(text: str) -> bool:
    lowered = text.casefold()
    return "daily meeting report" in lowered and "📍" in text


def display_time(value: str) -> str:
    hour, minute = [int(part) for part in value.split(":", 1)]
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    minute_text = f":{minute:02d}" if minute else ""
    return f"{display_hour}{minute_text}{suffix}"


def schedule_preview_text(
    sites: list[Site],
    fallback_time: str,
    staff_name: Callable[[int], str],
    include_instruction: bool = True,
) -> str:
    date_text = sites[0].dmr_date if sites else ""
    try:
        parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
        date_label = parsed_date.strftime("%d %b").lstrip("0").upper()
    except ValueError:
        date_label = date_text

    lines = [f"<b>POST-SITE SCHEDULE — {escape(date_label)}</b>", ""]
    for index, site in enumerate(sites, start=1):
        if site.scheduled_time:
            time_label = display_time(site.scheduled_time)
        else:
            time_label = f"{display_time(fallback_time)} fallback"
        names = " + ".join(staff_name(number) for number in site.staff_numbers) or "Unassigned"
        location = f" @ {escape(site.location)}" if site.location else ""
        lines.append(
            f"{index}. <b>{escape(time_label)}</b> — {escape(site.deal_name)}"
            f"{location} — {escape(names)}"
        )
    if include_instruction:
        lines.extend(["", "Nothing is scheduled until you approve this list."])
    return "\n".join(lines)
