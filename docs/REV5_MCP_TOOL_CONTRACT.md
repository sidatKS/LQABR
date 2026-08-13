# Rev 5 Step 5 — MCP tool contract (`lqabr_core/crm/hubspot.py`)

**Status:** handoff document. The Text/Voice Agent needs these five functions
at module level in `packages/lqabr_core/lqabr_core/crm/hubspot.py`. That file is
owned by another developer, so `agents/text_voice/src/text_voice.py` currently
carries a temporary `_MCPAdapter` that satisfies this contract over the existing
`HubSpotClient`.

**The switchover needs no agent-code change.** `text_voice.py` runs:

```python
_MCP_TOOL_NAMES = ("get_lead", "upsert_lead", "record_call_outcome",
                   "find_lead_by_phone", "leads_in_stage")

def _resolve_mcp():
    if all(hasattr(_hubspot, name) for name in _MCP_TOOL_NAMES):
        return _hubspot          # the real Step 5 tools
    return _MCPAdapter()         # the temporary stand-in
```

Land all five names and the adapter is bypassed automatically. Once that is
merged, delete the `TEMPORARY — Step 5 MCP adapter` block from `text_voice.py`
(everything from `_MCP_TOOL_NAMES` down to `mcp = _resolve_mcp()`, roughly 230
lines) and replace it with `from lqabr_core.crm import hubspot as mcp`.

---

## 1. Verified property names — please don't trust the UI labels

Read from portal **246777241** on **2026-07-30** via
`GET /crm/v3/properties/contacts` and `.../companies`. The Rev 5 spec's Data
Fields Reference lists **labels**, and three of them differ from the real API
name:

| Rev 5 / UI label | Real API name | Object | Notes |
|---|---|---|---|
| `voice_status` | **`lqabr_voice_status`** | contact | plain `voice_status` → `propertiesNotFound` |
| `email_status` | **`lqabr_email_status`** | contact | plain `email_status` → `propertiesNotFound` |
| `Phone number` | `phone` | contact | standard |
| `Job Title` | `jobtitle` | contact | standard |
| `employee_id` | `employee_id` | contact | custom, string |
| `email_id` | `email_id` | contact | custom, string |
| `decision_maker` | `decision_maker` | contact | custom, **string**, not bool |
| `probability` | `probability` | contact | custom, **number** |
| `opted_out` | `opted_out` | contact | custom, **string** — compare `"true"`, not truthiness |
| `company_id` | `company_id` | company | custom, string |
| `Industry` | `industry` | company | standard enumeration |
| `Annual revenue` | `annualrevenue` | company | standard |
| `frequency_of_purchase` | `frequency_of_purchase` | company | custom, string |

### Enumeration options (exact, complete)

```
lqabr_voice_status:  PENDING · INITIATED · COMPLETED · FAILED · VOICEMAIL_LEFT
lqabr_email_status:  PENDING · SENT · DELIVERED · OPENED · FAILED · BOUNCED
```

⚠️ **`lqabr_email_status` has no `CLICKED` option.** Rev 5 Step 1 says the
workflow fires when `email_status` becomes `"clicked"`; that value cannot exist
in this portal. `OPENED` is the eligibility value in use, config-driven via
`LQABR_TEXT_VOICE_ELIGIBLE_EMAIL_STATUS`.

---

## 2. The five functions

### `get_lead(employee_id: str) -> VoiceLead | None`

Returns `lqabr_core.types.VoiceLead` (added by this task — already merged in
`types.py`). `None` means no contact carries that `employee_id`. **Raise
`CRMError` for a HubSpot failure** — the agent branches differently on
"not found" than on "CRM is down", so the two must not be conflated.

Must populate the contact fields **and the associated company's**. Suggested
call pattern (what the adapter does today):

1. `GET /crm/v3/objects/contacts/{employee_id}?idProperty=employee_id&properties=…&associations=companies`
   — one call, associations included.
2. If that 400s (`employee_id` is not a unique-value property in this portal),
   fall back to `POST /crm/v3/objects/contacts/search` then re-`GET` by record
   id for the associations. Search never returns associations.
3. `GET /crm/v3/objects/companies/{id}?properties=…` for the company.
   **Swallow a failure here** and return the lead without company fields:
   Step 3's stop conditions are "no contact" and "no phone number", so a
   company read that 500s must degrade personalization, not cancel a call.

### `upsert_lead(contact_id, voice_status=None, probability=None, outcome=None) -> dict`

`PATCH` of `lqabr_voice_status` and/or `probability` only. Passing `outcome`
(one of the four Step 7 values) derives `voice_status` from it. **Reject a
`voice_status` outside the five enum options** with `CRMError` rather than
letting HubSpot return an opaque 400.

> Note: this is **not** `HubSpotClient.upsert_lead`, which still maps onto
> `lqabr_*` properties that were deleted from the portal and silently writes
> nothing. Please don't route this through `_to_properties`.

### `record_event(contact_id, event_type: EventType, detail=None) -> dict`

Delegate to `HubSpotClient.record_event`, which already applies the increment
from `lqabr_core/probability.py`. Never write a probability number at the call
site. Return at least:

```python
{"status": "recorded", "contact_id": …, "event_type": …,
 "probability": int, "stage": str, "promoted_to_scheduling": bool}
```

### `record_call_outcome(contact_id, outcome, detail=None) -> dict`

Rev 5 Step 8's two writes in order: `upsert_lead` then `record_event`.

**The mapping that matters:**

| Step 7 outcome | `lqabr_voice_status` | events recorded | probability from 30 |
|---|---|---|---|
| `not_answered` | `FAILED` | `CALL_NOT_ANSWERED` | 30 (unmoved, by design) |
| `voicemail` | `VOICEMAIL_LEFT` | `VOICEMAIL_LEFT` | 32 |
| `answered_not_engaged` | `COMPLETED` | `CALL_ANSWERED` | 45 |
| `answered_and_engaged` | `COMPLETED` | `CALL_ANSWERED`, **then** `CALL_ENGAGED` | **60 → promotes** |

⚠️ **An engaged call is two events, not one.** `probability.py` is built so
30 (text/voice entry) + 15 (answered) + 15 (engaged) = 60 =
`SCHEDULING_THRESHOLD`. Recording only `CALL_ENGAGED` leaves an engaged lead at
45 and it never promotes to the Scheduling Agent.

Attempt every write even if an earlier one failed, and return failures in the
result rather than raising:

```python
{"status": "ok" | "partial", "failures": [ "crm-error: …" ], "events": [ … ],
 "probability": int, "promoted_to_scheduling": bool}
```

"voice_status written, probability not" is a materially different state to
recover from than "nothing was written", so a partial write must stay visible.

### `find_lead_by_phone(phone) -> LeadProfile | None` and `leads_in_stage(stage, limit=100) -> list[LeadProfile]`

Thin passthroughs to the existing client methods. `find_lead_by_phone` is
Step 8's fallback when the call report carries no CRM id.

---

## 3. Audit logging

Rev 5 requires `audit_log` entries for the Step 3 and Step 8 HTTP calls
carrying *"which JWT/credential was used, status codes, retries, and auth
failures"*. `lqabr_core/observability.py` (merged by this task) provides it:

```python
from lqabr_core import observability as obs

obs.log_http_out(method, url, status_code=resp.status_code,
                 credential="lqabr-hubspot-access-token",  # the NAME, never the value
                 attempt=attempt + 1, duration_ms=…, service="hubspot")
```

Log **per attempt**, not per call — that is what makes a retried-then-succeeded
request distinguishable from a clean one. Never log a token value; use
`obs.redact()` if a credential has to appear at all.

---

## 4. Auth: the spec asks for something HubSpot rejects

Rev 5 Steps 3 and 8 say to mint a short-lived JWT and send it as
`Authorization: Bearer <jwt>` to HubSpot, "in place of the static
`LQABR_HUBSPOT_ACCESS_TOKEN`".

**HubSpot's CRM v3 API does not accept self-signed JWTs.** Verified against the
official docs: the accepted credentials are OAuth access tokens, private-app
(static) access tokens, and client-credentials tokens — all HubSpot-issued. The
token endpoint documents only `authorization_code` and `refresh_token` grants;
there is no `jwt-bearer` grant and no `client_assertion` parameter. A
self-minted JWT returns 401.

Per the product owner's decision, Steps 3 and 8 use the **private-app access
token from Secret Manager** (`lqabr-hubspot-access-token`). The "no static
tokens" requirement is unmet on the HubSpot leg and is tracked as a separate
task, not silently dropped.

Sources:
[Authentication methods](https://developers.hubspot.com/docs/guides/apps/authentication/intro-to-auth) ·
[Private apps](https://developers.hubspot.com/docs/guides/apps/private-apps/overview) ·
[OAuth token API](https://developers.hubspot.com/docs/api-reference/legacy/oauth-v1)
