"""CRM adapters. HubSpot is the chosen CRM and the system of record.

The interface (`CRMClient`) is vendor-neutral so a future adapter can be
swapped in via config — agents import the interface, never a vendor class
directly (except in composition roots / config wiring).
"""

from lqabr_core.crm.base import CRMClient, CRMError  # noqa: F401
from lqabr_core.crm.hubspot import HubSpotClient  # noqa: F401

__all__ = ["CRMClient", "CRMError", "HubSpotClient"]
