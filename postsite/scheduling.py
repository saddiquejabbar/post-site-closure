from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Site


@dataclass(frozen=True)
class SchedulePlan:
    due: list[Site]
    next_at: datetime | None


def site_target_dt(site: Site, tz: ZoneInfo, fallback_time: str) -> datetime:
    """Return the exact local time when a site's closure prompt is due."""
    time_str = site.scheduled_time or fallback_time
    hour, minute = [int(value) for value in time_str.split(":", 1)]
    try:
        date = datetime.strptime(site.dmr_date, "%Y-%m-%d").date()
    except ValueError:
        date = datetime.now(tz).date()
    return datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz)


def build_schedule_plan(
    sites: list[Site],
    now: datetime,
    tz: ZoneInfo,
    fallback_time: str,
) -> SchedulePlan:
    """Split pending sites into due work and the next future wake-up time."""
    scheduled = sorted(
        ((site_target_dt(site, tz, fallback_time), site) for site in sites),
        key=lambda item: (item[0], item[1].id or 0),
    )
    due = [site for target, site in scheduled if target <= now]
    next_at = next((target for target, _site in scheduled if target > now), None)
    return SchedulePlan(due=due, next_at=next_at)
