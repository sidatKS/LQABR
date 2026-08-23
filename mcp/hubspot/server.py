"""The MCP tool surface — what agents actually call.

    get_lead_profile_details   STEP 5  schema.py · crm.py — GET
    list_trigger_leads         STEP 5  the campaign's chunk, by trigger id
    post_patch_crm             STEP 9  crm.py — POST · PATCH

Loaded **in-process at runtime**: the MCP is declared as a dependency of
the agent's container, so there is no second process, no second instance
and no MCP service running on its own. `MCPSession` is the whole surface —
build one per run, hand it the run's observability sink, and every tool
call is audited on the agent's own streams.

Central, not co-located: this folder sits at the project root and the
email, voice and scheduling agents all reference the same copy. The profile
payload moves HubSpot <-> agent over this surface only — never through the
gateway, and agents never call HubSpot directly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import requests

from mcp.hubspot import NullObservability, ObservabilitySink
from mcp.hubspot.auth import RunTokenCache, TokenProvider
from mcp.hubspot.crm import HubSpotCRM
from mcp.hubspot.schema import ValidatedProfile, SchemaValidationError


class MCPSession:
    """One run's handle on the central HubSpot MCP."""

    def __init__(self, obs: Optional[ObservabilitySink] = None,
                 token_provider: Optional[TokenProvider] = None,
                 session: Optional[requests.Session] = None,
                 crm: Optional[HubSpotCRM] = None) -> None:
        self._obs = obs or NullObservability()
        self._tokens = RunTokenCache(provider=token_provider, obs=self._obs)
        self._crm = crm or HubSpotCRM(tokens=self._tokens, obs=self._obs, session=session)

    # ------------------------------------------------------------ step 4
    def acquire_bearer(self) -> str:
        """STEP 4 — hold a short-lived machine-to-machine token for the life
        of the run. Called before any tool call; every later hop re-uses the
        cached value until it expires."""
        return self._tokens.get()

    @property
    def crm(self) -> HubSpotCRM:
        return self._crm

    # ------------------------------------------------------------ step 5
    def get_lead_profile_details(self, object_id: str) -> Dict[str, Any]:
        """STEP 5 — retrieve a lead's profile through the central MCP.

        Validates against the HubSpot schema, then returns the 9-parameter
        profile. Both the validation and the HubSpot hop appear in the
        agent's own logs. A profile that cannot be emailed comes back as an
        explicit `{"error": "bad-data: ..."}` — flagged, never dropped."""
        try:
            profile = self._crm.get_lead_profile(str(object_id))
        except SchemaValidationError as exc:
            self._obs.process(step=5, event="schema_validation_failed",
                              object_id=str(object_id), reason=str(exc))
            return {"error": str(exc), "object_id": str(object_id)}
        return profile.to_dict()

    def get_profile(self, object_id: str) -> ValidatedProfile:
        """Typed variant for in-process callers that want the object rather
        than the tool-shaped dict."""
        return self._crm.get_lead_profile(str(object_id))

    def list_trigger_leads(self, object_id: str, limit: int = 25) -> List[Dict[str, Any]]:
        """STEP 5 — the set of lead profiles HubSpot chunked under one
        object ID. The trigger itself carried no profile data; this is
        where the payload is fetched back."""
        return [lead.to_dict() for lead in self._crm.leads_for_trigger(object_id, limit=limit)]

    # ------------------------------------------------------------ step 9
    def post_patch_crm(self, object_id: str, properties: Dict[str, Any],
                       object_type: str = "contact") -> Dict[str, Any]:
        """STEP 9 — land engagement state on the system of record, through
        the same central MCP and against the same schema used for the read.

        Returns `{"status": "written", ...}` or an explicit error — a failed
        write-back never reads as a success."""
        try:
            self._crm.patch_object(str(object_id), properties, object_type=object_type)
        except SchemaValidationError as exc:
            self._obs.process(step=9, event="writeback_validation_failed",
                              object_id=str(object_id), reason=str(exc))
            return {"error": str(exc), "object_id": str(object_id)}
        return {"status": "written", "object_id": str(object_id),
                "object_type": object_type,
                "properties": sorted(properties.keys())}


#: The named tool surface, for callers that want to enumerate it (an ADK
#: tool list, a future protocol adapter). Kept as factories over a session
#: so nothing here holds global state.
TOOLS: Dict[str, Callable[[MCPSession], Callable[..., Any]]] = {
    "get_lead_profile_details": lambda s: s.get_lead_profile_details,
    "list_trigger_leads": lambda s: s.list_trigger_leads,
    "post_patch_crm": lambda s: s.post_patch_crm,
}


def build_session(obs: Optional[ObservabilitySink] = None, **kwargs: Any) -> MCPSession:
    """Factory used by agents at run start."""
    return MCPSession(obs=obs, **kwargs)
