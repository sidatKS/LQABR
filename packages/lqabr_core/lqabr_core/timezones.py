"""Timezone support for the Scheduling Agent.

Scheduling invites always offer the four zones LQABR operates in:
EST, CST, PST, IST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

SUPPORTED_ZONES = {
    "EST": "America/New_York",
    "CST": "America/Chicago",
    "PST": "America/Los_Angeles",
    "IST": "Asia/Kolkata",
}


@dataclass(frozen=True)
class ZoneOption:
    label: str      # "EST"
    iana: str       # "America/New_York"
    local_time: str  # formatted current/slot time in that zone


def zone_options(at: datetime | None = None) -> list[ZoneOption]:
    """The four scheduling zones, with `at` (default: now UTC) rendered in each."""
    moment = at or datetime.now(tz=ZoneInfo("UTC"))
    options = []
    for label, iana in SUPPORTED_ZONES.items():
        local = moment.astimezone(ZoneInfo(iana))
        options.append(ZoneOption(label=label, iana=iana, local_time=local.strftime("%Y-%m-%d %I:%M %p %Z")))
    return options


def to_iana(label_or_iana: str) -> str:
    """Accept 'EST'/'CST'/'PST'/'IST' or a raw IANA name; return IANA name."""
    key = label_or_iana.strip().upper()
    if key in SUPPORTED_ZONES:
        return SUPPORTED_ZONES[key]
    # validate raw IANA input
    ZoneInfo(label_or_iana)
    return label_or_iana
