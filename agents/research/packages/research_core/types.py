"""Typed values that cross a boundary in this agent.

Dataclasses, not dicts, wherever a value moves between modules — the shape is
the contract. Everything here is plain Python: no HubSpot types, no ADK types,
nothing that would tie the library to a transport.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class LeadFacts:
    """What the Research Agent is entitled to know about a lead.

    Read from the MCP's lead-profile tool. The agent needs only the handful of
    fields that shape a research note — the industry it operates in and the
    company it belongs to — plus the three ids the write tool demands back.
    """

    objectId: str = ""
    first_name: str = ""
    last_name: str = ""
    job_title: str = ""
    industry: str = ""
    company: str = ""
    company_about: str = ""
    company_website: str = ""
    #: Required by the MCP's upsert tool; carried so the write can be made.
    employee_id: str = ""
    company_id: str = ""
    decision_maker_flag: str = ""
    #: What is already on the record — used to avoid a pointless rewrite.
    existing_lead_context: str = ""

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @property
    def writable(self) -> List[str]:
        """Which of the three write-required ids are missing. Empty == writable."""
        return [name for name, value in (
            ("employee_id", self.employee_id),
            ("company_id", self.company_id),
            ("decision_maker_flag", self.decision_maker_flag),
        ) if not str(value).strip()]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BlogFacts:
    """The published post that triggered this run, read from the MCP."""

    blog_published_at: str = ""
    blog_summary: str = ""
    blog_industry: str = ""
    ticket_id: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.blog_summary.strip())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchFindings:
    """What the web-search pass produced. `sources` are URLs actually cited."""

    text: str = ""
    sources: List[str] = field(default_factory=list)
    searches: int = 0
    model: str = ""


@dataclass
class ResearchNote:
    """The composed note, as it will be written to the CRM."""

    text: str = ""
    sources: List[str] = field(default_factory=list)
    #: How many web searches grounded it. Computed by the provider, logged,
    #: and — until now — dropped at THIS boundary, so every response on the
    #: wire reported `searches: 0`.
    searches: int = 0

    def as_hubspot_text(self, max_chars: int = 60_000) -> str:
        """The single blob written to the lead-context property — prose only.

        The cited URLs used to be appended here as a `Sources:` tail. Measured
        over one live campaign that was 48% of everything written (8,384 of
        17,630 characters), and the field's only consumer is the Email agent,
        which writes outreach and cannot use a URL list. The citations stay on
        the `model_response` log line, under this run's id.

        HubSpot multi-line text caps at 65 536 characters; stay under it rather
        than discovering the limit as a 400 at write time.
        """
        return self.text.strip()[:max_chars]


@dataclass
class WriteResult:
    """What the MCP did. A failed write never reads as a success."""

    #: written      the note is on the record
    #: dry_run      deliberately not sent (LQABR_RESEARCH_DRY_RUN=1)
    #: skipped      nothing needed doing (context already present)
    #: not_writable the note could NOT be landed — bad data on the lead
    #: error        the MCP refused or could not be reached
    status: str = ""
    objectId: str = ""
    property_name: str = ""
    chars: int = 0
    tool: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """The note is on the record, or deliberately did not need to be.

        `not_writable` is NOT ok: a lead missing the ids the write tool demands
        got no note, and reporting that as `completed` with an empty error is
        the exact thing "status follows the WRITE" forbids.
        """
        return self.status in ("written", "dry_run", "skipped")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
