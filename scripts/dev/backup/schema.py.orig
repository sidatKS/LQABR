"""HubSpot schema validation — the same schema on the read (step 5) and the
write-back (step 9).

Two directions:

*Read* — `validate_profile()` checks the 9-parameter profile the MCP is
about to hand back to the agent. Five of the nine are named explicitly in
the design as what email construction needs: **company ID, email ID,
industry, job title, employee ID**. A profile missing an email ID is not workable
and is rejected here, at the boundary, with a reason — never dropped
silently, and never sent to.

*Write* — `validate_writeback()` checks the property bag before it is
PATCHed. HubSpot rejects an unknown property name or an out-of-vocabulary
enumeration value with a 400, so catching it here turns a runtime failure
into a named, logged validation result.

Property names are owned by the HubSpot schema and must match it exactly.
Confirmed live (ldqfingsrv-dev, 2026-07-23):

    contacts   employee_id, jobtitle, email_id, phone,
               employee_id, lqabr_email_status (enumeration:
               PENDING/SENT/DELIVERED/OPENED/FAILED/BOUNCED), probability
    companies  company_id, industry, annualrevenue, frequency_of_purchase

`email_campaign_complete` is a **placeholder name pending confirmation**
against the owning schema, so it is env-overridable
(``LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY``) rather than hard-coded —
renaming it is a config change. `object_id` is likewise the contact
property HubSpot chunks a campaign's leads under and is overridable via
``LQABR_HUBSPOT_OBJECT_ID_PROPERTY``.

`lead_context` — ADDED FOR REV 8 (v4). The research agent derives a
100-200 word knowledge graph per lead and persists it on the contact
(step 7); the email agent reads it back (step 9) and frames construction
with it (step 10). It is read on the way out and allowed on the way in, so
one MCP serves both agents through the same schema. Strictly additive: a
portal without the property, or a lead the research agent has not reached
yet, validates exactly as it did before and carries ``lead_context=""``.
The name is env-overridable (``LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY``) for
the same reason as the two above — it is not confirmed against the live
schema yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from lqabr_core.types import LeadProfile

#: The 9 parameters. Read-side contract between HubSpot and every agent.
#: These are `LeadProfile` ATTRIBUTE names, because that is what
#: `validate_profile` getattrs them against — not the HubSpot column names.
#: The two differ for the external ids (`external_employee_id` ->
#: `employee_id`, `external_company_id` -> `company_id`) and for the address
#: (`email` -> `email_id`); using the column name here silently reports every
#: lead as missing that pointer.
PROFILE_POINTERS = (
    "external_employee_id", "job_title", "external_company_id", "email", "phone",
    "industry", "company_size_revenue", "location", "linkedin_url",
)

#: THE CONSTRUCTION FIELD LIST, in report order. Confirmed with the user
#: 2026-08-18 and the single source of truth for two things that must never
#: drift apart: what `ValidatedProfile.as_context()` hands the model, and what
#: the `get_lead_profile` tool shows an operator. If those two disagree, the
#: operator is reviewing a different lead than the one the email is written
#: from.
#:
#: `employee_id` and `company_id` are deliberately ABSENT. They stay on the
#: profile and in `to_dict()` — the portal shows them — but they are internal
#: references that must never reach an email, so they are not construction
#: inputs and are not what an operator checks the copy against.
CONSTRUCTION_FIELDS = (
    "email_id", "first_name", "last_name", "company", "job_title",
    "industry", "industry_group", "company_about", "company_website",
    "annual_revenue", "lead_context",
)

#: Named for email construction. The email greets the lead by `first_name`
#: (firstname/lastname are standard HubSpot properties); `employee_id` stays
#: an internal identifier and is never written into the prose. Of these only
#: the email ID is fatal on its own — a lead with no address cannot be emailed.
NAMED_FOR_CONSTRUCTION = ("first_name", "last_name", "company_id", "email_id",
                          "industry", "job_title")
REQUIRED_TO_SEND = ("email_id",)

#: Contact properties this MCP is allowed to write.
#: firstname/lastname are STANDARD HubSpot properties that exist on every
#: portal; the lead-profile agent writes them and the email agent reads them
#: to greet the lead by first name.
WRITABLE_CONTACT_PROPERTIES = frozenset({
    "firstname", "lastname", "jobtitle", "company_id", "email_id", "phone",
    "employee_id", "lqabr_email_status", "probability",
})

#: Confirmed allowed values of the lqabr_email_status enumeration. Anything
#: else is a 400 from HubSpot.
EMAIL_STATUS_VALUES = ("PENDING", "SENT", "DELIVERED", "OPENED", "FAILED", "BOUNCED")

PROBABILITY_MIN = 0
PROBABILITY_MAX = 100


def campaign_complete_property() -> str:
    """The single column the voice campaign reads. Placeholder name pending
    confirmation against the owning HubSpot schema — overridable by config."""
    return os.environ.get("LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY", "email_campaign_complete")


def object_id_property() -> str:
    """The contact property HubSpot chunks a campaign's leads under."""
    return os.environ.get("LQABR_HUBSPOT_OBJECT_ID_PROPERTY", "object_id")


def lead_context_property() -> str:
    """The contact property carrying the research agent's knowledge graph.

    Written by the research agent at step 7, read by the email agent at
    step 9. Not confirmed against the live schema yet — the 2026-08-05 audit
    of all 410 contact properties found neither `object_id` nor
    `email_campaign_complete`, so this one is assumed absent too until
    proven otherwise. Overridable by config, never a code edit.

    Set it to the empty string to disable the lead_context hop entirely: the
    MCP then stops asking HubSpot for the property and every profile comes
    back with `lead_context=""`. That is the escape hatch for a portal where
    the column does not exist yet."""
    return os.environ.get("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY", "lead_context")


def last_modified_email_property() -> str:
    """The HubSpot datetime property that records when the last email event
    landed (SENT, DELIVERED, OPENED, BOUNCED, etc.).

    Shown in the portal as "Last Modified Email" (Date and time picker,
    Contact information group). The internal API name defaults to
    ``last_modified_email``; override with
    ``LQABR_HUBSPOT_LAST_MODIFIED_EMAIL_PROPERTY`` if the real name differs
    — it is a config change, never a code edit."""
    return os.environ.get("LQABR_HUBSPOT_LAST_MODIFIED_EMAIL_PROPERTY", "last_modified_email")


class SchemaValidationError(ValueError):
    """A profile or property bag that does not satisfy the HubSpot schema.
    Always carries a reason — bad records are flagged, never dropped."""


@dataclass
class ValidatedProfile:
    """The schema-validated 9-parameter profile the MCP returns at step 5,
    flattened into exactly what email construction consumes.

    Field names follow the HubSpot columns (`email_id`, not `email`) so the
    read side, the write side and the CRM all say the same word."""

    object_id: str
    email_id: str
    first_name: str = ""
    last_name: str = ""
    employee_id: str = ""
    job_title: str = ""
    company: str = ""
    industry: str = ""
    company_id: str = ""
    company_size_revenue: str = ""
    location: str = ""
    linkedin_url: str = ""
    phone: str = ""
    probability: int = 0
    email_status: str = "PENDING"
    #: The research agent's knowledge graph for this lead (step 7 -> step 9).
    #: Empty when the research agent has not reached the lead yet, or when the
    #: portal has no such property. Never derived here — the email agent does
    #: no research and must not invent a context it was not given.
    lead_context: str = ""
    #: Company-owned inputs the FRD names for construction, read off the
    #: associated company by the MCP (rev 8). Confirmed populated live:
    #: `website` -> https://m1.com, `about_us` -> "Fintech platform offering...",
    #: `hs_industry_group` -> "Investment / Wealth Management (Automated
    #: Investing)". Empty when the company holds none.
    company_website: str = ""
    company_about: str = ""
    industry_group: str = ""
    missing_pointers: List[str] = field(default_factory=list)

    @property
    def has_lead_context(self) -> bool:
        """Whether this lead carries a research context. The email agent's
        step-9 gate reads this: `lead_context` being written is what triggers
        the email campaign at step 8, so its absence means the run arrived
        before research finished — not that the lead is bad."""
        return bool(self.lead_context.strip())

    def as_context(self) -> Dict[str, str]:
        """The substitution context an email skill is rendered against.

        The greeting addresses the lead by `first_name` (with `last_name`
        offered for a fuller salutation); when neither is populated they are
        empty and DRAFTING_RULES tells the model to open with a plain,
        nameless greeting rather than invent one. The internal `employee_id`
        is deliberately NOT here — it is an internal identifier and must
        never appear in the prose.

        CHANGED IN REV 8 — the company NAME is now offered, and `company_id`
        no longer is. The name was withheld because the contact GET does not
        carry it and nobody was reading it off the company; the MCP now does,
        so DRAFTING_RULES no longer has to tell the model to write around a
        company it cannot name. `company_id` (`C0021`) was only ever a stand-in
        for the name and is an internal reference that must never appear in
        prose, so offering it stopped being justified the moment the real name
        arrived.

        Also offered as of rev 8: the company website, its About-Us text, its
        industry group and its annual revenue — the FRD's named construction
        inputs, read off the associated company. A field the company does not
        hold stays empty and is filtered out by `skills.lead_facts()` rather
        than reaching the model as a blank.

        THE TWO THAT NEED A RULE, NOT JUST A VALUE:

        `email_id` is offered (confirmed field list, 2026-08-18) so the model
        knows who it is writing to — a personal address and a corporate one are
        different readers. It must NEVER be written into the body; DRAFTING_RULES
        forbids it. Addressing the message is step 11's job, not the prose's.

        `annual_revenue` is a SIZING signal only. HubSpot stores it unitless —
        M1 Finance holds the literal string `"4.7"`, with nothing recording
        whether that is millions or billions. Quoting it would either be
        nonsense ("$4.7 in revenue") or an invention ("$4.7B"), so
        DRAFTING_RULES allows it to calibrate scale and forbids it appearing as
        a figure.

        The internal identifiers `employee_id` and `company_id` remain absent.
        They stay on the profile and on `to_dict()`; they are references for our
        systems and must never reach the prose.

        `lead_context` is the rev-8 addition and the most important entry
        here: it is the research agent's fit narrative for THIS lead, and it
        is what makes two leads sharing one skill and one industry receive
        different emails. It is offered as a fact like any other, so an empty
        one is filtered out by `skills.lead_facts()` rather than reaching the
        model as a blank."""
        return {
            "email_id": self.email_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "company": self.company,
            "job_title": self.job_title or "your role",
            "industry": self.industry or "",
            "industry_group": self.industry_group,
            "company_about": self.company_about,
            "company_website": self.company_website,
            "annual_revenue": self.company_size_revenue,
            "lead_context": self.lead_context,
        }

    def construction_view(self) -> Dict[str, str]:
        """The construction fields with their REAL values, for showing a human.

        Same keys as `as_context()` and in the agreed order, so what an
        operator reviews is exactly what the email is built from — but without
        `as_context()`'s "your role" fallback for an empty job title. A profile
        view has to show the gap; the model's copy of it needs something to
        write with."""
        values = {
            "email_id": self.email_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "company": self.company,
            "job_title": self.job_title,
            "industry": self.industry,
            "industry_group": self.industry_group,
            "company_about": self.company_about,
            "company_website": self.company_website,
            "annual_revenue": self.company_size_revenue,
            "lead_context": self.lead_context,
        }
        return {name: values[name] for name in CONSTRUCTION_FIELDS}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id, "email_id": self.email_id,
            "first_name": self.first_name, "last_name": self.last_name,
            "employee_id": self.employee_id,
            "job_title": self.job_title,
            "industry": self.industry, "company_id": self.company_id,
            "company_size_revenue": self.company_size_revenue,
            "location": self.location, "linkedin_url": self.linkedin_url,
            "phone": self.phone, "probability": self.probability,
            "email_status": self.email_status,
            "lead_context": self.lead_context,
            "company": self.company,
            "company_website": self.company_website,
            "company_about": self.company_about,
            "industry_group": self.industry_group,
            "missing_pointers": list(self.missing_pointers),
        }


def validate_profile(profile: LeadProfile) -> ValidatedProfile:
    """Validate one lead profile coming OUT of HubSpot (step 5).

    Raises SchemaValidationError with a named reason when the lead cannot be
    emailed at all. Merely-incomplete profiles pass, carrying their gaps in
    `missing_pointers` so the skill can write around a missing field rather
    than inventing a value."""
    if not profile.object_id:
        raise SchemaValidationError("bad-data: profile carries no HubSpot contact id")
    if not profile.email:
        raise SchemaValidationError(
            f"bad-data: object {profile.object_id} has no email ID listed")

    missing = [name for name in PROFILE_POINTERS if not getattr(profile, name, None)]

    probability = profile.probability or 0
    if not PROBABILITY_MIN <= probability <= PROBABILITY_MAX:
        raise SchemaValidationError(
            f"bad-data: object {profile.object_id} probability {probability} out of range")

    first_name, last_name = "", ""
    if profile.full_name:
        parts = profile.full_name.strip().split()
        if parts:
            first_name = parts[0]
            last_name = " ".join(parts[1:])

    return ValidatedProfile(
        object_id=str(profile.object_id),
        email_id=profile.email,   # LeadProfile.email <- HubSpot column email_id
        first_name=first_name,
        last_name=last_name,
        employee_id=profile.external_employee_id or "",
        job_title=profile.job_title or "",
        company=profile.company or "",
        industry=profile.industry or "",
        company_id=profile.external_company_id or "",
        company_size_revenue=profile.company_size_revenue or "",
        location=profile.location or "",
        linkedin_url=profile.linkedin_url or "",
        phone=profile.phone or "",
        probability=probability,
        email_status=(profile.extra or {}).get("email_status") or "PENDING",
        # Carried on `extra` because `LeadProfile` is shared with the sibling
        # agents and its 9-pointer shape is not this schema's to change.
        # Absent -> "" and the email agent's step-9 gate flags the lead; it is
        # never defaulted to a placeholder narrative.
        lead_context=str((profile.extra or {}).get("lead_context") or "").strip(),
        # Company-owned construction inputs, same carriage as lead_context and
        # for the same reason — LeadProfile is shared and not this schema's to
        # widen. Absent stays empty; never defaulted to a placeholder.
        company_website=str((profile.extra or {}).get("company_website") or "").strip(),
        company_about=str((profile.extra or {}).get("company_about") or "").strip(),
        industry_group=str((profile.extra or {}).get("industry_group") or "").strip(),
        missing_pointers=missing,
    )


def validate_writeback(properties: Dict[str, Any]) -> Dict[str, str]:
    """Validate the property bag going INTO HubSpot (step 9).

    Returns the bag normalised to the string values the REST API expects.
    Raises SchemaValidationError on an unknown property name or an
    out-of-vocabulary value — the same schema as the read, checked before
    the hop rather than discovered as a 400 after it."""
    if not properties:
        raise SchemaValidationError("bad-data: empty property bag — nothing to write")

    # `lead_context` is allowed here so the research agent's step-7 persist
    # goes through the SAME validation as every other write. The email agent
    # never writes it — it only reads it back at step 9.
    allowed = (set(WRITABLE_CONTACT_PROPERTIES) | {campaign_complete_property()}
               | {last_modified_email_property()} | {lead_context_property()}) - {""}
    validated: Dict[str, str] = {}

    for name, value in properties.items():
        if name not in allowed:
            raise SchemaValidationError(
                f"schema-error: '{name}' is not a writable contact property "
                f"(allowed: {sorted(allowed)})")
        if value is None:
            continue

        if name == "lqabr_email_status":
            text = str(value).upper()
            if text not in EMAIL_STATUS_VALUES:
                raise SchemaValidationError(
                    f"schema-error: lqabr_email_status={value!r} is not one of {EMAIL_STATUS_VALUES}")
            validated[name] = text
        elif name == "probability":
            try:
                number = int(float(value))
            except (TypeError, ValueError) as exc:
                raise SchemaValidationError(
                    f"schema-error: probability={value!r} is not numeric") from exc
            if not PROBABILITY_MIN <= number <= PROBABILITY_MAX:
                raise SchemaValidationError(
                    f"schema-error: probability={number} outside {PROBABILITY_MIN}-{PROBABILITY_MAX}")
            validated[name] = str(number)
        elif name == campaign_complete_property():
            # Boolean-typed in HubSpot; the REST API takes "true"/"false".
            validated[name] = "true" if value in (True, "true", "True", 1, "1") else "false"
        elif name == last_modified_email_property():
            # Datetime-typed in HubSpot ("Date and time picker"). The REST API
            # v3 expects a Unix epoch millisecond integer. Pass as a numeric
            # string — the PATCH body is JSON so HubSpot accepts either, but
            # validate_writeback always produces strings for consistency.
            try:
                ts = int(value)
            except (TypeError, ValueError) as exc:
                raise SchemaValidationError(
                    f"schema-error: {name}={value!r} is not an integer ms timestamp") from exc
            validated[name] = str(ts)
        else:
            validated[name] = str(value)

    if not validated:
        raise SchemaValidationError("bad-data: property bag held only null values")
    return validated
