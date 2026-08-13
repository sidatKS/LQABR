# SP-1 Voice Campaign — Rev 5 `text_voice` flow map

Implementation map for **SP-1: Voice Campaign — Build text_voice agent (MVP)**,
against `LQABR_text_voice_Vapi_Functional_Requirements` Rev 5.
Built 2026-07-30. Vapi replaces Twilio; the agent no longer runs mid-call.

---

## 1. The flow, end to end

```
                            ┌───────────────────────────────────────┐
   ①  HubSpot               │  ②  API GATEWAY — the only way in     │
   ─────────                │      agents/text_voice/src/tools.py   │
   lead marked              │                                        │
   lqabr_email_status       │   route 1  POST /hubspot/lead   ───┐   │
      = OPENED             ─┼──▶ verify sig · 200 · hand off    │   │
   workflow fires           │   route 2  POST /vapi/report  ─┐  │   │
   (nothing polls)          │       verify secret · 200      │  │   │
                            └────────────────────────────────┼──┼───┘
                                                             │  │
                        in-process handoff (function call) ───┘  │
                                                                 ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  THE AGENT — one place                                              │
   │  agents/text_voice/src/text_voice.py            [MODEL]             │
   │                                                                     │
   │   ③  Read the lead        get_lead(employee_id)                     │
   │      → MCP, in-process    stop if no contact · no phone ·           │
   │                             opted out · already complete            │
   │                                                                     │
   │   ④  Place the call       tools.place_call(lead)                    │
   │      POST /call           the only outbound telephony leg           │
   │                           → voice_status = INITIATED                │
   │  ─────────────── AFTER THE CALL ENDS · SAME AGENT ───────────────   │
   │   ⑦  Summarise            summarise_report(endedReason, transcript) │
   │      the report           the AGENT decides, not analysisPlan       │
   │                                                                     │
   │   ⑧  Push it to MCP       push_to_mcp(outcome, summary, recording)  │
   └──────────┬──────────────────────────────────────────▲──────────────┘
              │ ③ ⑧ calls                                │ ⑥ report
              ▼                                          │
   ┌────────────────────────────────────┐    ┌────────────────────────────┐
   │  ⑤  CENTRAL MCP FOLDER [IN-PROCESS]│    │ ⑥ Vapi runs the call       │
   │  packages/lqabr_core/              │    │   api.vapi.ai · not ours   │
   │                                     │    │                            │
   │  read tools     get_lead()          │    │ dials · every turn ·       │
   │  write tools    upsert_lead()       │◀───│ voicemail handling         │
   │                 record_event()      │    │                            │
   │                                     │    │ THREE OUTCOMES             │
   │  crm/hubspot.py  ← another dev      │    │  not answered              │
   │  observability.py  system/process/  │    │  rings or busy → voicemail │
   │                    audit            │    │  answered → ask questions  │
   │  types.py  VoiceLead · VoiceOutcome │    │                            │
   └──────────────┬─────────────────────┘    └────────────────────────────┘
                  │ writes
                  ▼
            ① HubSpot  ← probability crosses 60 → next stage's trigger fires
```

**One deployable service, two inbound routes, one outbound leg.** `④ POST /call`
is the only place this system originates a call. `⑥` is entirely Vapi's: no code
in this repo runs mid-call.

---

## 2. Step-by-step — what runs where

| Step | Trigger | Code | Credential | Logs |
|---|---|---|---|---|
| **① HubSpot trigger** | contact's `lqabr_email_status` → `OPENED` | *none — inside HubSpot* | — | none (first appears at ②) |
| **② API gateway** | inbound `POST /hubspot/lead` | `tools.py :: hubspot_lead()` | HubSpot sig v3 → v2 → shared token | `audit_log` (sig check, status) + `process_log` (handoff) |
| **③ Read the lead** | agent, straight after ②'s handoff | `text_voice.py :: get_lead()` → `mcp.get_lead()` | `lqabr-hubspot-access-token` | `process_log` (result) + `audit_log` (1–3 HTTP GETs) |
| **④ Place the call** | ③ returned a callable lead | `text_voice.py :: handle_new_lead()` → `tools.py :: place_call()` | `lqabr-vapi-api-key` | `process_log` (step, call id) + `audit_log` (`POST /call`) |
| **⑥ Vapi runs the call** | the call connects | *none — Vapi's side* | — | none (first appears at ⑦) |
| **⑥→⑦ gateway** | inbound `POST /vapi/report` | `tools.py :: vapi_report()` | `lqabr-vapi-webhook-secret` | `audit_log` (status, endedReason) |
| **⑦ Summarise** | ⑥→⑦'s handoff | `text_voice.py :: summarise_report()` | model provider key | `process_log` (**model, input/output tokens, latency**) |
| **⑧ Push to MCP** | ⑦ produced an outcome | `text_voice.py :: push_to_mcp()` → `mcp.record_call_outcome()` | `lqabr-hubspot-access-token` | `process_log` (result) + `audit_log` (both writes) |
| **always on** | — | `observability.py :: log_startup/log_shutdown` | — | `system_log` |

### Step ③ — the four stop conditions

Rev 5 names two; two more are enforced deliberately.

| Condition | Reason string | Why |
|---|---|---|
| no contact for that `employee_id` | `not-found: …` | Rev 5 Step 3.4 — ④ never runs |
| `phone` missing | `bad-data: contact has no phone number` | Rev 5 Step 3.4 |
| `opted_out` is `"true"` | `opted-out: …` | real field on the contact; calling anyway is a compliance failure, not a bug |
| `lqabr_voice_status` ∈ {`COMPLETED`, `VOICEMAIL_LEFT`} | `already-complete: …` | a duplicate webhook delivery must not dial a person twice |

A HubSpot failure returns `crm-error: …` and is never reported as "not found" —
those two lead to opposite decisions.

### Step ⑦ — how the outcome is decided

Three paths, in order. Only the third spends a completion.

| Path | When | Outcome |
|---|---|---|
| `ended_reason` | `customer-did-not-answer`, `customer-busy`, `voicemail`, any `pipeline-error-*` / `call.start.error-*` / `*vapifault*` | settled without the model — no conversation happened, so there is nothing to interpret |
| `empty_transcript` | call connected, zero words transcribed | **`voicemail`** — words are the only evidence a person was there |
| `model` | there is a transcript | the model classifies + summarises; tokens and latency logged |
| `fallback` | the model call failed | deterministic outcome, logged at ERROR — Step ⑧ still runs |

**The four outcomes → what gets written:**

| Outcome | `lqabr_voice_status` | Events | Probability from 30 |
|---|---|---|---|
| `not_answered` | `FAILED` | `CALL_NOT_ANSWERED` | 30 — unmoved by design |
| `voicemail` | `VOICEMAIL_LEFT` | `VOICEMAIL_LEFT` | 32 |
| `answered_not_engaged` | `COMPLETED` | `CALL_ANSWERED` | 45 |
| `answered_and_engaged` | `COMPLETED` | `CALL_ANSWERED` **+** `CALL_ENGAGED` | **60 → promotes to Scheduling** |

An engaged call records **two** events: `probability.py` is built so
30 + 15 + 15 = 60 = `SCHEDULING_THRESHOLD`. One event leaves the lead at 45 and
it never promotes — which is how ⑧ "closes the loop back to ①".

---

## 3. Repository tree

Annotated with Rev 5 step numbers. `+` new this task, `~` modified, `-` retired.

```
LQABR/
│
├── agents/text_voice/                    the one service that gets deployed
│   ├── src/                              three files, nothing else
│   │   ├── agent.py                    ~ ADK shim: re-exports root_agent
│   │   ├── text_voice.py               + [③][⑦][⑧] the model and its tools
│   │   │   ├── get_lead()                    ③ read + 4 stop conditions
│   │   │   ├── summarise_report()            ⑦ outcome + summary
│   │   │   ├── push_to_mcp()                 ⑧ both writes
│   │   │   ├── handle_new_lead()             entrypoint: ③ → ④
│   │   │   ├── handle_call_report()          entrypoint: ⑦ → ⑧
│   │   │   ├── list_text_voice_queue()       operator tool (queue ≥ 30)
│   │   │   ├── _MCPAdapter          TEMPORARY — see §5
│   │   │   └── root_agent                    ADK agent + instructions
│   │   ├── tools.py                    + [②][④] the two routes in + POST /call
│   │   │   ├── app                           FastAPI, one service
│   │   │   ├── POST /hubspot/lead            ② verify · 200 · hand off
│   │   │   ├── POST /vapi/report             ⑥→⑦ verify · 200 · hand off
│   │   │   ├── GET  /healthz
│   │   │   ├── place_call()                  ④ the one outbound leg
│   │   │   ├── build_call_payload()          destination + personalization
│   │   │   ├── build_assistant_config()      the Q&A script, in version control
│   │   │   ├── VapiClient                    retry + audit, REST to api.vapi.ai
│   │   │   ├── verify_hubspot_signature()    v3 → v2 → shared token
│   │   │   └── verify_vapi_secret()          X-Vapi-Secret / Bearer
│   │   ├── twilio_client.py            - retired
│   │   ├── conversation.py             - retired (Vapi owns every turn now)
│   │   └── webhook_app.py              - retired (replaced by tools.py)
│   ├── tests/
│   │   ├── conftest.py                 ~ loads tools.py + text_voice.py
│   │   ├── test_conversation.py        - retired
│   │   ├── test_twilio_client.py       - retired
│   │   ├── test_twilio_retry_edge_cases.py  - retired
│   │   ├── test_voice_webhook.py       - retired
│   │   └── test_webhook_edge_cases.py  - retired
│   ├── requirements.txt                ~ + vapi-server-sdk  − twilio
│   └── .env.example                    ~ Vapi + gateway + eligibility config
│
├── packages/lqabr_core/                  central tool folder — NOT under the agent
│   └── lqabr_core/
│       ├── crm/hubspot.py                [⑤] read tools + write tools
│       │                                     ← owned by another developer
│       ├── observability.py            + system_log · process_log · audit_log
│       ├── types.py                    ~ + VoiceLead · VoiceOutcome
│       ├── probability.py                increments + thresholds (unchanged)
│       ├── secrets.py                    Secret Manager + env fallback
│       └── timezones.py
│
├── docs/
│   ├── REV5_TEXT_VOICE_FLOW_MAP.md    + this file
│   └── REV5_MCP_TOOL_CONTRACT.md      + the ⑤ handoff spec
│
└── infra/gcp/
    ├── config.sh                       ~ + lqabr-vapi-* secrets
    └── config.dev.sh                   ~ + lqabr-vapi-* secrets
```

---

## 4. Observability — three streams, one correlation id

One JSON object per line on stdout, which is what Cloud Run ingests: `severity`
and `message` land on the LogEntry, everything else is queryable under
`jsonPayload`.

| Stream | Question it answers | Written by |
|---|---|---|
| `system_log` | is the service itself healthy? | startup/shutdown, config problems. Carries no lead identity on purpose. |
| `process_log` | where did this call get stuck, and what did the model decide at what cost? | every step: `started` → `ok`/`stopped`/`error`/`degraded`, with `duration_ms`; plus `model` / `input_tokens` / `output_tokens` / `latency_ms` at ⑦ |
| `audit_log` | did this request actually go out, and what came back? | every inbound (② and ⑥→⑦: `signature_check`, status) and outbound (③ ④ ⑧: `credential`, `status_code`, `attempt`) HTTP call |

A `correlation_id` is bound once at the gateway and carried through the whole
lead journey along with `employee_id`, `hubspot_contact_id` and `call_id`, so two
concurrent calls are separable in the logs. Canonical step names
(`step3.read_lead`, `step7.summarise_report`, …) make a per-step filter exact
rather than a substring match.

Credentials are logged by **Secret Manager name**, never by value; `obs.redact()`
fingerprints anything that must appear at all.

Useful queries:

```
jsonPayload.log_stream="audit"   AND jsonPayload.status_code>=400
jsonPayload.log_stream="process" AND jsonPayload.step="step7.summarise_report"
jsonPayload.correlation_id="<id>"                       # one lead, every step
jsonPayload.log_stream="process" AND jsonPayload.status="degraded"
```

---

## 5. Deviations from Rev 5, and why

Each of these is a place the spec could not be implemented literally. None is
silently dropped.

| # | Rev 5 says | Reality | What was built |
|---|---|---|---|
| 1 | ③/⑧ authenticate to HubSpot with a self-minted short-lived JWT | HubSpot's CRM v3 API accepts only HubSpot-issued credentials; a self-signed JWT returns **401**. Its token endpoint has no `jwt-bearer` grant. | Private-app token from Secret Manager, per the product owner's call. The "no static tokens" requirement is **unmet on the HubSpot leg** and tracked as its own task. |
| 2 | ④ authenticates to Vapi with a private-scope JWT | Vapi *does* support JWTs, but its docs never state the signing algorithm — HS256 is only an inference from their `jsonwebtoken` sample. | Private API key from Secret Manager (`lqabr-vapi-api-key`), per the product owner's call. Tracked as its own task. |
| 3 | ① fires when `email_status` is set to `"clicked"` | `lqabr_email_status` has no `CLICKED` option: PENDING / SENT / DELIVERED / OPENED / FAILED / BOUNCED. The trigger **cannot fire** as written. | `OPENED` is the eligibility value, config-driven via `LQABR_TEXT_VOICE_ELIGIBLE_EMAIL_STATUS`. ③ re-checks it and logs a mismatch. |
| 4 | Data Fields lists `voice_status`, `email_status` | Those are **UI labels**. The real API names are `lqabr_voice_status` / `lqabr_email_status`; the unprefixed names return `propertiesNotFound`. | Prefixed names everywhere, with the label mapping documented at each use. |
| 5 | ⑤ lives in `crm/hubspot.py` | That file is owned by another developer this sprint. | `text_voice.py` carries a clearly-marked temporary `_MCPAdapter` satisfying the same contract, which defers automatically once the real tools land. See `REV5_MCP_TOOL_CONTRACT.md`. |
| 6 | requirements `+ vapi-server-sdk` | The SDK hides the HTTP layer that `audit_log` is required to record. | SDK is declared (and is the reference for the report payload shape), but calls go out through `VapiClient` over REST so every attempt, status code and credential reference is logged. |
| 7 | ② is a gateway service separate from the agent | The separate Agent Gateway service is a different SP-1 task (owner: SN). | Both routes are implemented in this agent's `tools.py`, so the voice campaign is independently deployable now. Front-able by the gateway later without touching ③–⑧. |
| 8 | ⑧ writes `summary` and `recordingUrl` | Rev 5's Data Fields Reference defines no HubSpot property for either. | Both travel on the engagement event's `detail` field. No property was invented. |

---

## 6. Twilio bugs this retires

The confirmed live-testing bug list was against the Twilio implementation.
Removing it resolves most of them structurally rather than by patching.

| Bug | Status |
|---|---|
| `AnsweredBy` "unknown"/"fax" treated as a confirmed human answer | **Gone by design.** No human-vs-machine guess is made at connect time. ⑦ decides from what was actually said, and a connected call with no speech is classified `voicemail`, not `answered`. |
| No `try/except` around `_context_for()` — a CRM error 500s the webhook | **Gone.** `webhook_app.py` is retired. Both routes verify → 200 → background task; a CRM failure inside a step is caught, logged and reported, and cannot fail the webhook response. |
| A call that rings out never invokes `/voice/answer`, so nothing is recorded | **Gone.** Vapi posts `end-of-call-report` for *every* call however it ends, including `customer-did-not-answer`. The SMS follow-up leg no longer exists (Rev 5 has no SMS). |
| `list_text_voice_queue()` has no `try/except` — a CRM failure kills the adk session | **Fixed.** Returns `{"error": "crm-error: …"}`. |
| No opt-out enforcement before dialing | **Fixed twice.** ③ blocks with `opted-out: …`, and `place_call()` re-checks at the boundary — it is the last code before a real phone rings. |
| `is_affirmative()` substring matching ("sure" inside "assure") | **Not applicable.** `conversation.py` is retired; Vapi's model handles intent. |

---

## 7. Configuration needed before the first live call

| What | Where | Status |
|---|---|---|
| `lqabr-vapi-api-key` | Secret Manager (+ `config.sh` `LQABR_SECRETS`) | **to create** — the org's *private* key |
| `lqabr-vapi-webhook-secret` | Secret Manager | **to create** — any long random string |
| `lqabr-hubspot-webhook-token` | Secret Manager | **to create** — set the same value in the HubSpot workflow's `X-LQABR-Webhook-Token` header |
| `LQABR_VAPI_PHONE_NUMBER_ID` | Cloud Run env | **to set** — ④ cannot dial without it |
| `LQABR_GATEWAY_BASE_URL` | Cloud Run env | **to set** — wrong value means calls run and no result is ever recorded |
| HubSpot workflow → `POST /hubspot/lead` | HubSpot portal 246777241 | **to configure** on `lqabr_email_status = OPENED` |
| `CLICKED` option on `lqabr_email_status` | HubSpot portal | **decision** — add it, or stay on `OPENED` |

Local dev only, never in a gcp env: `LQABR_SKIP_HUBSPOT_SIGNATURE=1`,
`LQABR_SKIP_VAPI_SIGNATURE=1`.
