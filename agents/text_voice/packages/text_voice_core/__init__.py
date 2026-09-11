"""text_voice_core — the Text/Voice agent's own library.

Carved out of lqabr_core on 2026-09-08 per the lqabr-agent-scaffold contract:
this package imports nothing from the rest of the repo, and the only platform
coupling left is a runtime URL (the Step 5 MCP endpoint), never an import.
tests/test_independence.py polices that by AST.

What came across, and what did not:

  types.py          EventType, VoiceOutcome, VoiceLead, CRMError.
                    LeadProfile / LeadSource / LeadStage / EngagementEvent
                    stayed in lqabr_core — nothing here reads them.
  probability.py    Copied WHOLE, deliberately. See the warning below.
  secrets.py        Secret Manager by name, with an env fallback.
  observability.py  The four-stream logger, ported unchanged so the call
                    sites did not move; the scaffold's three-stream
                    text_voice_logging.py replaces it in the next stage.
  gcp_id_token.py   Cloud Run identity token for calling the MCP.

WARNING — probability.py is a DUPLICATE, and that is a knowing exception.

CLAUDE.md says twice that probability increments and thresholds live only in
lqabr_core/probability.py and are never redefined elsewhere. Standing alone
requires a copy, so the two rules cannot both hold; Rao chose the copy on
2026-09-08.

The increments are the scoring contract BETWEEN agents. If this copy drifts
from the shared one, leads land at 45 instead of 60 and silently never reach
the Scheduling Agent — with both sides' own tests still green.
tests/test_probability_conformance.py exists to catch exactly that, and only
works while both copies live in one repo. The real fix is for the MCP to
return the new probability so no agent computes it.
"""
