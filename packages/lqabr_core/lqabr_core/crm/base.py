"""Vendor-neutral CRM interface.

Rules (CLAUDE.md):
- The CRM is the system of record. Agents hold no canonical lead state;
  every status/probability change writes back, and on conflict CRM wins.
- Adapters are typed, mockable, and own their retry/failure behavior.
- A CRM failure must never silently advance or drop a lead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from lqabr_core.types import EngagementEvent, LeadProfile, LeadStage


class CRMError(RuntimeError):
    """Raised when a CRM operation fails after retries. Callers must handle
    this and flag the lead for review — never swallow it."""


class CRMClient(ABC):
    @abstractmethod
    def upsert_lead(self, profile: LeadProfile) -> LeadProfile:
        """Create or update the contact for this profile; returns profile with
        the CRM contact id populated."""

    @abstractmethod
    def get_lead(self, contact_id: str) -> LeadProfile:
        """Fetch one lead by CRM contact id."""

    @abstractmethod
    def find_lead_by_email(self, email: str) -> Optional[LeadProfile]:
        """Fetch a lead by email, or None."""

    @abstractmethod
    def leads_in_stage(self, stage: LeadStage, min_probability: int = 0, limit: int = 100) -> List[LeadProfile]:
        """Leads currently owned by a pipeline stage, optionally above a
        probability floor — the orchestrator's work queue query."""

    @abstractmethod
    def record_event(self, event: EngagementEvent) -> LeadProfile:
        """Apply one engagement event: increment its counter, recompute
        probability, update stage if a threshold was crossed. Returns the
        updated lead."""

    @abstractmethod
    def set_stage(self, contact_id: str, stage: LeadStage, reason: Optional[str] = None) -> None:
        """Explicit stage transition writeback."""
