"""Shared data contract + validation.

FR §0 — LeadProfile, 9 fields. The field names AND the HubSpot property names
ARE the contract. Do not rename either.

  #  field (snake_case)      source column                      HubSpot property        object
  1  employee_id             employees  Employee_ID             employee_id             Contact (DEDUP)
  2  company_id              employees  Company_ID              company_id              Company (DEDUP)
  3  decision_maker_flag     employees  Decision_Maker_Flag     decision_maker (bool)   Contact
  4  job_title               contacts   Job_Title               jobtitle                Contact
  5  email                   contacts   Email                   email_id (CUSTOM)       Contact
  6  phone                   contacts   Phone                   phone                   Contact
  7  industry                companies  Industry                industry (UPPER)        Company
  8  annual_revenue_m        companies  "Annual_Revenue (M)"    annualrevenue           Company
  9  frequency_of_purchase   companies  Frequency_of_Purchase   frequency_of_purchase   Company

email -> "email_id" is deliberate: HubSpot's standard ``email`` property enforces
one contact per address, and the seed data shares a placeholder address, so
contacts dedup on ``employee_id`` in a custom field instead.

Validation (Step 5, item 1) is a step INSIDE the write tool, not a tool of its
own. Scope is DECIDED (context §8, Q2): baseline only, no network — required
fields present, types per FR §0, non-null dedup keys, industry uppercased.
Missing email / phone / job_title are Optional by design and are NOT mismatches.
Anything HubSpot itself rejects (bad industry option, non-numeric revenue) is
caught by the upsert error handling and routed to errors/schema_mismatch.jsonl.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

# --- HubSpot property names (the contract — do not rename) ------------------

CONTACT_DEDUP_PROPERTY = "employee_id"
COMPANY_DEDUP_PROPERTY = "company_id"

CONTACT_PROPERTIES = ("employee_id", "decision_maker", "jobtitle", "email_id", "phone")
COMPANY_PROPERTIES = ("company_id", "industry", "annualrevenue", "frequency_of_purchase")

REQUIRED_FIELDS = ("employee_id", "company_id", "decision_maker_flag")
OPTIONAL_FIELDS = (
    "job_title",
    "email",
    "phone",
    "industry",
    "annual_revenue_m",
    "frequency_of_purchase",
)
LEAD_PROFILE_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS  # all 9, in contract order

DECISION_MAKER_TRUE = "yes"


class SchemaMismatchError(Exception):
    """A record failed baseline validation, or HubSpot rejected it.

    Carries the lead_ref_id so the record can be written to
    errors/schema_mismatch.jsonl and reported as failed — kept, not dropped,
    not inserted. The run continues.
    """

    def __init__(self, reasons: list[str], lead_ref_id: str | None = None, source: str = "validation"):
        self.reasons = reasons
        self.lead_ref_id = lead_ref_id
        self.source = source  # "validation" | "hubspot"
        super().__init__("; ".join(reasons))


@dataclass
class LeadProfile:
    """One decision-maker lead. Built in memory by Step 3; never written to disk."""

    # required
    employee_id: str
    company_id: str
    decision_maker_flag: str
    # optional by design
    job_title: str | None = None
    email: str | None = None
    phone: str | None = None
    industry: str | None = None
    annual_revenue_m: str | None = None
    frequency_of_purchase: str | None = None

    # -- derived ------------------------------------------------------------

    @property
    def decision_maker(self) -> bool:
        """HubSpot ``decision_maker`` — bool, true when the flag reads "yes"."""
        return (self.decision_maker_flag or "").strip().lower() == DECISION_MAKER_TRUE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- HubSpot payloads ---------------------------------------------------

    def to_contact_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "employee_id": self.employee_id,
            "decision_maker": self.decision_maker,
        }
        if self.job_title is not None:
            props["jobtitle"] = self.job_title
        if self.email is not None:
            props["email_id"] = self.email  # CUSTOM property, not standard "email"
        if self.phone is not None:
            props["phone"] = self.phone
        return props

    def to_company_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {"company_id": self.company_id}
        if self.industry is not None:
            # HubSpot custom option internal names are the label uppercased (verified).
            props["industry"] = normalise_industry(self.industry)
        if self.annual_revenue_m is not None:
            props["annualrevenue"] = self.annual_revenue_m
        if self.frequency_of_purchase is not None:
            props["frequency_of_purchase"] = self.frequency_of_purchase
        return props


@dataclass
class UnresolvedLead:
    """A decision-maker row whose contact and/or company join did not resolve.

    Flagged and returned separately — never pushed to HubSpot, never discarded
    (context §7.9: never silently drop a lead).
    """

    employee_id: str
    company_id: str
    reason: str
    source_row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PushResult:
    """Outcome of one upsert_lead_profiles call (Step 5)."""

    lead_ref_id: str
    employee_id: str
    company_id: str
    contact_hs_id: str | None = None
    company_hs_id: str | None = None
    contact_action: str | None = None  # "create" | "update"
    company_action: str | None = None  # "create" | "update"
    associated: bool = False
    status: str = "pushed"  # "pushed" | "failed"
    # B1: which KIND of failure. "record" = the data is wrong (schema_mismatch),
    # "transport" = the dependency misbehaved. Systemic failures never reach a
    # PushResult at all — they halt the run.
    failure_kind: str | None = None  # None | "record" | "transport"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeadProfileRecord:
    """Return shape of get_lead_profile (Step 6).

    DECIDED (context §8, Q3): the 9-field LeadProfile PLUS the HubSpot ids on a
    wrapper, so the email / voice / scheduler agents can write status back
    without re-searching. Not the raw HubSpot payload.

    B9: ``company_resolved`` and ``warnings`` exist because the previous build
    could return ``found=True`` with an EMPTY required ``company_id`` when the
    associated-company read failed. Three downstream agents consume this shape;
    a silently half-populated profile is the worst thing to hand them.
    """

    profile: LeadProfile | None
    contact_hs_id: str | None = None
    company_hs_id: str | None = None
    found: bool = True
    company_resolved: bool = True
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "profile": self.profile.to_dict() if self.profile else None,
            "contact_hs_id": self.contact_hs_id,
            "company_hs_id": self.company_hs_id,
            "company_resolved": self.company_resolved,
            "warnings": list(self.warnings),
        }


def not_found() -> LeadProfileRecord:
    """B20: a factory, not a shared mutable module-level singleton."""
    return LeadProfileRecord(profile=None, found=False, company_resolved=False)


# --- helpers ----------------------------------------------------------------


def normalise_industry(raw: str | None) -> str | None:
    """FR §0 field 7: industry is ``raw.strip().upper()``."""
    if raw is None:
        return None
    return raw.strip().upper()


def clean_optional(raw: str | None) -> str | None:
    """Trim an optional source value; empty string becomes None."""
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def normalise_profile(profile: LeadProfile) -> LeadProfile:
    """Return the profile with contract normalisation applied (B12).

    Step 3 already normalises, but ``upsert_lead_profiles`` is a SHARED MCP
    tool: the email / voice / scheduler agents can call it with a raw
    ``industry``. Normalising inside the tool means they are not rejected for
    something the writer fixes two lines later.
    """
    if not isinstance(profile, LeadProfile):
        return profile
    normalised = normalise_industry(profile.industry)
    if normalised == profile.industry:
        return profile
    return replace(profile, industry=normalised)


# --- validation (Step 5, item 1 — baseline only, no network) ----------------


def validate_lead_profile(profile: LeadProfile) -> list[str]:
    """Return a list of mismatch reasons. Empty list == valid.

    Baseline only, per the DECIDED scope. Deliberately does NOT check:
      - presence of job_title / email / phone (Optional by design)
      - whether ``industry`` is a real HubSpot option
      - whether ``annual_revenue_m`` parses as a number
    Those are HubSpot's call and surface as 400s during the upsert.
    """
    reasons: list[str] = []

    if not isinstance(profile, LeadProfile):
        return [f"not a LeadProfile: {type(profile).__name__}"]

    # required fields present, non-null dedup keys
    for name in REQUIRED_FIELDS:
        value = getattr(profile, name, None)
        if value is None:
            reasons.append(f"missing required field: {name}")
        elif not isinstance(value, str):
            reasons.append(f"field {name} must be str, got {type(value).__name__}")
        elif not value.strip():
            reasons.append(f"required field is empty: {name}")

    # optional fields must be str or None
    for name in OPTIONAL_FIELDS:
        value = getattr(profile, name, None)
        if value is not None and not isinstance(value, str):
            reasons.append(f"field {name} must be str or None, got {type(value).__name__}")

    # industry must already be uppercased by Step 3
    industry = getattr(profile, "industry", None)
    if isinstance(industry, str) and industry != industry.strip().upper():
        reasons.append("industry must be normalised to raw.strip().upper()")

    return reasons


def assert_valid(profile: LeadProfile, lead_ref_id: str | None = None) -> None:
    """Raise SchemaMismatchError if the profile fails baseline validation."""
    reasons = validate_lead_profile(profile)
    if reasons:
        raise SchemaMismatchError(reasons, lead_ref_id=lead_ref_id, source="validation")
