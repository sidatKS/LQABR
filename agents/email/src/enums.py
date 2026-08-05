"""Closed Mailgun event vocabulary — NEW IN V2.

The v2 design closes the Mailgun failure enums: this module is the full
Mailgun vocabulary this agent recognises, not only the happy path.

    delivered · opened · clicked · failed · bounced · complained ·
    unsubscribed · stopped

Two things live here and nowhere else:

1. **The closed enum** (`MailgunEvent`) and which of its members are
   TERMINAL — a terminal status ends the run after the step-9 write-back,
   with no hand-off to text/voice.
2. **The translation** from Mailgun's real wire vocabulary onto that closed
   set (`from_mailgun`). Mailgun does not literally emit "bounced" or
   "stopped": a hard bounce arrives as ``failed`` with
   ``severity: permanent``, and a send Mailgun refuses arrives as
   ``rejected``. Collapsing those here is what lets the rest of the agent
   reason about exactly eight values.

Probability increments are NOT defined here. Delivered/opened/clicked map
onto `lqabr_core.types.EventType`, and `lqabr_core.probability` remains the
single source of truth for what each is worth (CLAUDE.md §6).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Optional, Tuple

from lqabr_core.types import EventType


class MailgunEvent(str, Enum):
    """The eight-value closed vocabulary. Anything Mailgun sends that does
    not map onto one of these is acknowledged and ignored, never guessed at."""

    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    FAILED = "failed"              # transient failure — Mailgun gave up retrying
    BOUNCED = "bounced"            # permanent failure — address is dead
    COMPLAINED = "complained"      # recipient marked it as spam
    UNSUBSCRIBED = "unsubscribed"  # recipient opted out
    STOPPED = "stopped"            # send refused before it left Mailgun


#: Engagement — these raise probability and keep the run alive.
POSITIVE_EVENTS = frozenset({
    MailgunEvent.DELIVERED, MailgunEvent.OPENED, MailgunEvent.CLICKED,
})

#: A terminal status ends the run after the write-back, with no hand-off.
TERMINAL_EVENTS = frozenset({
    MailgunEvent.FAILED, MailgunEvent.BOUNCED, MailgunEvent.COMPLAINED,
    MailgunEvent.UNSUBSCRIBED, MailgunEvent.STOPPED,
})

#: Terminal statuses that must also suppress the address from future sends.
#: (Suppression itself is deferred past the Rev 5 MVP — this is the set the
#: suppression work will consume when it lands.)
SUPPRESSING_EVENTS = frozenset({
    MailgunEvent.BOUNCED, MailgunEvent.COMPLAINED, MailgunEvent.UNSUBSCRIBED,
})

#: Which scored EventType each engagement event is. Terminal events have no
#: entry: they are recorded, but they do not move probability.
SCORED_AS: Dict[MailgunEvent, EventType] = {
    MailgunEvent.DELIVERED: EventType.EMAIL_DELIVERED,
    MailgunEvent.OPENED: EventType.EMAIL_OPENED,
    MailgunEvent.CLICKED: EventType.EMAIL_CLICKED,
}

#: `lqabr_email_status` is a confirmed HubSpot enumeration accepting exactly
#: PENDING / SENT / DELIVERED / OPENED / FAILED / BOUNCED. There is no
#: CLICKED option, so a click records as OPENED (a click implies an open),
#: and the three opt-out style terminals record as FAILED — the only value
#: the enumeration has for "this address is not workable".
HUBSPOT_EMAIL_STATUS: Dict[MailgunEvent, str] = {
    MailgunEvent.DELIVERED: "DELIVERED",
    MailgunEvent.OPENED: "OPENED",
    MailgunEvent.CLICKED: "OPENED",
    MailgunEvent.FAILED: "FAILED",
    MailgunEvent.BOUNCED: "BOUNCED",
    MailgunEvent.COMPLAINED: "FAILED",
    MailgunEvent.UNSUBSCRIBED: "FAILED",
    MailgunEvent.STOPPED: "FAILED",
}

#: Which status wins when two arrive for the same message. Engagement events
#: are asynchronous and out of order — `opened` can land before `delivered`
#: — so the resolved status is the highest-ranked seen so far, never simply
#: the latest. Terminals outrank everything: once an address bounced, a
#: stale `delivered` must not walk the record back.
_PRECEDENCE: Dict[MailgunEvent, int] = {
    MailgunEvent.DELIVERED: 10,
    MailgunEvent.OPENED: 20,
    MailgunEvent.CLICKED: 30,
    MailgunEvent.FAILED: 40,
    MailgunEvent.STOPPED: 50,
    MailgunEvent.UNSUBSCRIBED: 60,
    MailgunEvent.COMPLAINED: 70,
    MailgunEvent.BOUNCED: 80,
}

#: Mailgun wire event name -> closed enum, for the cases that need no extra
#: signal. `failed` is deliberately absent: it needs `severity` to resolve.
_DIRECT: Dict[str, MailgunEvent] = {
    "delivered": MailgunEvent.DELIVERED,
    "opened": MailgunEvent.OPENED,
    "clicked": MailgunEvent.CLICKED,
    "complained": MailgunEvent.COMPLAINED,
    "unsubscribed": MailgunEvent.UNSUBSCRIBED,
    "rejected": MailgunEvent.STOPPED,
    "stopped": MailgunEvent.STOPPED,
    "bounced": MailgunEvent.BOUNCED,
}

#: Wire events that are real Mailgun events but carry no meaning for this
#: design — acknowledged and skipped, never treated as unknown/erroneous.
IGNORED_WIRE_EVENTS = frozenset({"accepted", "stored", "list_member_uploaded"})


def from_mailgun(event_name: str, severity: str = "") -> Optional[MailgunEvent]:
    """Translate a raw Mailgun ``event-data.event`` onto the closed enum.

    ``severity`` is Mailgun's ``delivery-status.severity`` / ``severity``
    field, which is what separates a hard bounce from a transient failure:

        failed + permanent -> BOUNCED
        failed + anything  -> FAILED

    Returns None for events outside the vocabulary (``accepted``,
    ``stored``, …) so the caller can acknowledge and skip them rather than
    guessing a status."""
    name = (event_name or "").strip().lower()
    if name == "failed":
        return (MailgunEvent.BOUNCED
                if (severity or "").strip().lower() == "permanent"
                else MailgunEvent.FAILED)
    return _DIRECT.get(name)


def is_terminal(event: MailgunEvent) -> bool:
    """Does this status end the run (after the step-9 write-back)?"""
    return event in TERMINAL_EVENTS


def precedence(event: MailgunEvent) -> int:
    return _PRECEDENCE[event]


def resolve_status(current: Optional[MailgunEvent],
                   incoming: MailgunEvent) -> Tuple[MailgunEvent, bool]:
    """WHICH STATUS WON — returns ``(winner, changed)``.

    `changed` is False when a lower-ranked event arrives after a higher one
    (a late `delivered` behind a `clicked`, a stale event after a bounce).
    The caller still logs the arrival to process_log, but must not write the
    weaker value back to HubSpot."""
    if current is None:
        return incoming, True
    if precedence(incoming) > precedence(current):
        return incoming, True
    return current, False
