# Email Agent — v2 flow map

> Implements **LQABR Email Agent Outreach v2** (system architecture rev 6,
> revision of rev 5 after design review) against the existing Python/ADK
> monorepo. Ticket: **SP-1 · Email Campaign — business logic development**.
>
> One process · one container · one run ID · four log streams.
> MCP defined at project root, loaded in-process at runtime.
> System of record: **HubSpot CRM**.

---

## 1. File tree

Legend — **NEW** file added by v2 · **CHG** existing file rewired ·
*(unchanged)* touched by nothing.

```
lqabr/
│
├── mcp/                                     NEW  ← central, at project ROOT
│   ├── __init__.py                          NEW  package note: why not the MCP SDK
│   ├── mailgun/                             NEW  the Mailgun tool call
│   │   └── events.py                        NEW  fetch_event_status — track-ID status
│   ├── hubspot/                             NEW  the one HubSpot tool folder
│   │   ├── __init__.py                      NEW  ObservabilitySink protocol + NullObservability
│   │   ├── auth.py                          NEW  [STEP 4] machine-to-machine bearer
│   │   │     TokenProvider (ABC)
│   │   │     SecretManagerTokenProvider     default — lqabr-hubspot-access-token
│   │   │     OAuth2ClientCredentialsProvider  for when OAuth replaces the private app
│   │   │     build_provider()               config-driven: LQABR_HUBSPOT_AUTH_MODE
│   │   │     RunTokenCache                  one bearer per run, refreshed on expiry
│   │   ├── schema.py                        NEW  HubSpot schema validation, both directions
│   │   │     validate_profile()             [STEP 5] read side → ValidatedProfile
│   │   │     validate_writeback()           [STEP 9] write side → normalised property bag
│   │   │     campaign_complete_property()   placeholder name, env-overridable
│   │   │     object_id_property()          env-overridable
│   │   ├── crm.py                           NEW  all HubSpot REST work
│   │   │     HubSpotCRM.get_lead_profile()  [5] delegates to lqabr_core.crm.HubSpotClient
│   │   │     HubSpotCRM.leads_for_trigger() [5] the campaign's chunk, by trigger id
│   │   │     HubSpotCRM.patch_contact()     [9] validated PATCH
│   │   │     HubSpotCRM.mark_sent()         [7] PENDING → SENT
│   │   │     HubSpotCRM.mark_campaign_complete()  [10] the single hand-off column
│   │   └── server.py                        NEW  the tool surface
│   │         MCPSession.acquire_bearer()          [4]
│   │         MCPSession.get_lead_profile_details()[5]
│   │         MCPSession.list_trigger_leads()      [5]
│   │         MCPSession.post_patch_crm()          [9]
│   │         TOOLS                                the three named tools
│   └── tests/                               NEW
│       ├── conftest.py · mcp_fakes.py       NEW  FakeSession/FakeResponse/RecordingObs
│       ├── test_auth.py                     NEW  13 tests
│       ├── test_schema.py                   NEW  18 tests
│       └── test_crm_and_server.py           NEW  15 tests
│
├── agents/
│   └── email/
│       ├── skills/                          NEW  ← templatized outreach emails
│       │   ├── __init__.py                  NEW  loader · select_skill() · render() · fill()
│       │   ├── DRAFTING_RULES.md            NEW  shared — prepended to every skill
│       │   ├── technology/SKILL.md          NEW  software / saas / it services / …
│       │   ├── financial_services/SKILL.md  NEW  banking / insurance / fintech / …
│       │   ├── healthcare/SKILL.md          NEW  hospitals / pharma / medical devices / …
│       │   └── manufacturing/SKILL.md       NEW  industrial / logistics / automotive / …
│       │        (no default skill — the industry selects, or the lead is
│       │         flagged unresolved)
│       ├── src/
│       │   ├── agent.py                          ADK discovery shim (unchanged)
│       │   ├── email_agent.py                CHG  the model-facing tool surface only
│       │   ├── outreach.py                   NEW  [STEPS 3-7] synchronous
│       │   │     start_run()                      [3][4] bind token, acquire bearer
│       │   │     build_model_fn()                 [6] one model call per lead
│       │   │     construct_email()                [6] select skill + fill
│       │   │     send_one()                       [7] Mailgun send + run state
│       │   │     run_campaign()                   the batch loop, one profile at a time
│       │   ├── events.py                     NEW  [STEPS 8-9] asynchronous
│       │   │     handle_event()                   match by token, resolve, write back
│       │   ├── enums.py                      NEW  closed Mailgun vocabulary
│       │   │     MailgunEvent                     the eight values
│       │   │     from_mailgun()                   wire → closed enum
│       │   │     resolve_status()                 which status won
│       │   │     HUBSPOT_EMAIL_STATUS             → the confirmed HubSpot enumeration
│       │   ├── observability.py              NEW  the four log streams
│       │   │     RunContext · bind_run()          [3] object_id + run_id
│       │   │     system() process() audit() model()
│       │   │     bearer_fingerprint()             which bearer, never the value
│       │   │     MCPObservability                 the sink handed to mcp/hubspot
│       │   ├── runstate.py                   NEW  [7 → 8] persisted run state
│       │   │     LeadRunRecord · RunStateStore
│       │   ├── service_app.py                NEW  the ONE HTTP surface: gateway
│       │   │                                     entry + Mailgun push + health
│       │   └── (webhook_app.py DELETED 2026-08-04 — second image removed)
│       ├── tests/
│       │   ├── conftest.py · email_fakes.py  CHG  FakeCRM/FakeSession/FakeMailgun
│       │   ├── test_email_agent.py           CHG   8 tests
│       │   ├── test_mailgun_webhook.py       CHG  6 tests
│       │   ├── test_enums.py                 NEW  19 tests
│       │   ├── test_observability.py         NEW  18 tests
│       │   ├── test_skills.py                NEW  16 tests
│       │   ├── test_outreach.py              NEW  15 tests
│       │   ├── test_events.py                NEW  16 tests
│       │   └── test_runstate.py              NEW   9 tests
│       ├── requirements.txt                  CHG  + google-genai; note on the MCP dep
│       └── .env.example                      CHG  + auth mode, property names, log config
│
├── packages/lqabr_core/                           unchanged — reused, not forked
│   └── lqabr_core/
│       ├── crm/hubspot.py                         the confirmed property mapping
│       ├── probability.py                         the ONLY source of increments
│       ├── mailgun.py                             send + HMAC verification
│       └── secrets.py                             Secret Manager, env fallback
│
├── .claude/skills/mailgun-integration/
│   └── SKILL.md                              NEW  Mailgun integration skill
├── docs/EMAIL_AGENT_V2_FLOW.md               NEW  this document
├── tests/pytest.ini                          CHG  + mcp/tests; moved from root
└── .gitignore                                CHG  + .runstate/ .logs/ *_log.jsonl
```

---

## 2. The flow, step by step

### Synchronous — business logic 1

```
 ┌ STEP 1 ── HubSpot campaign trigger ──────────────────── outside this system
 │  A HubSpot campaign fires. Lead profiles are chunked under one trigger ID
 │  and STAY IN HUBSPOT. The trigger carries the trigger ID and nothing more.
 │  ⚠ Custom HubSpot triggers are a paid-tier feature. On the free tier this
 │    entry does not exist as designed.
 │  logs: none — the run first appears in our logs at step 2.
 ▼
 ┌ STEP 2 ── Agent gateway ─────────────────────────────── outside this repo
 │  HTTPS → agent protocol. Identifies email_agent, forwards the trigger ID,
 │  audits the entry. No profile payload passes through. Agents never call
 │  each other.  (Owner: Saroja — Agent Gateway track.)
 │  logs: audit_log (inbound). ⚠ The gateway sits outside the runtime
 │        boundary — whether its record is one of our four streams or a
 │        fifth log is undecided.
 ▼
 ┌ STEP 3 ── Run start ────────────────── observability.bind_run()
 │  object_id + run_id bound for the life of the run.
 │  correlation_token = "<object_id>:<run_id>"
 │  From here every record in every stream carries that token.
 │  logs: process_log  run_started
 ▼
 ┌ STEP 4 ── Acquire the bearer ───────── mcp/hubspot/auth.py :: RunTokenCache
 │  Short-lived machine-to-machine token, cached for the run, refreshed on
 │  expiry. Acquired BEFORE any lead is constructed, so a credential problem
 │  fails the run rather than half a batch. The value is never logged.
 │  logs: audit_log (outbound token call, status)
 │        process_log token_acquired / token_refreshed
 ▼
 ┌ STEP 5 ── Request the lead profiles ── mcp/hubspot/crm.py + schema.py — GET
 │  leads_for_trigger(object_id)  → the campaign's chunk
 │  get_lead_profile(contact_id)   → schema-validated 9-parameter profile
 │                                    (company ID · industry · first name ·
 │                                     last name · email ID + 4 more)
 │  The agent never makes a direct HubSpot call. Bearer on the header.
 │  A profile that cannot be emailed → `unresolved` with a reason. Never dropped.
 │  logs: process_log schema_validated / lead_unresolved
 │        audit_log   the HubSpot GET + which bearer (fingerprint)
 ▼
 ┌ STEP 6 ── Construct the email ──────── skills/ + ONE model call per lead
 │  select_skill(industry) → technology | financial_services | healthcare |
 │                           manufacturing
 │  NO DEFAULT SKILL. An empty industry, or one no skill claims, raises
 │  SkillError → `unresolved` with the reason. Never generic copy, never
 │  a guessed near match, never a silent drop.
 │  render(skill, context, model_fn) — the model personalises the wording
 │  INSIDE the approved template; it does not invent an offer, and never
 │  writes one email for a batch. Model output is re-substituted, so an
 │  unfilled {company} can never reach a prospect. A model failure degrades
 │  to deterministic substitution rather than stopping the send.
 │  logs: process_log skill_selected — WHICH SKILL, on what industry/fields
 │        model_log   input tokens · output tokens (content off by default)
 ▼
 ┌ STEP 7 ── Send, one email per lead ─── outreach.send_one() → Mailgun
 │  v:lqabr_correlation_token · v:lqabr_object_id · v:lqabr_run_id ·
 │  v:hubspot_contact_id  — echoed back on every later event.
 │  Run state persisted: object_id + run_id → lead → message id.
 │  CRM mirrored: lqabr_email_status PENDING → SENT.
 │  ✗ Rejected send → terminal `stopped` from enums.py, persisted, and
 │    written back immediately (no webhook is coming for a mail that never left).
 │  logs: audit_log   the outbound Mailgun call + response
 │        process_log run_state_written / send_rejected
 ▼
   … hours or days pass …
```

### Asynchronous — business logic 2

```
 ┌ STEP 8 ── Receive engagement events ── service_app.py → events.handle_event()
 │  HMAC verified first, always, no bypass flag.
 │  from_mailgun(event, severity) → the closed eight-value enum:
 │      delivered · opened · clicked · failed · bounced ·
 │      complained · unsubscribed · stopped
 │  Matched to a lead by correlation token against persisted run state —
 │  however long after the send it arrives, so events never collide.
 │  resolve_status() decides WHICH STATUS WON: a late `delivered` behind a
 │  `clicked` is recorded but never written back.
 │  ⚠ An event whose token matches no run state has no path today — it is
 │    flagged `unresolved` (422), never guessed at.
 │  logs: audit_log   the inbound push
 │        process_log status_resolved / event_matched_no_run_state
 ▼
 ┌ STEP 9 ── Write the status back ────── mcp/hubspot — POST · PATCH
 │  Same central MCP, same schema as the read.
 │    lqabr_email_status      DELIVERED | OPENED | FAILED | BOUNCED
 │    probability             += lqabr_core.probability increments
 │                               (+2 delivered · +5 opened · +10 clicked)
 │    email_campaign_complete set WHEN lqabr_email_status REACHES OPENED
 │  Probability is read back from HubSpot first — on conflict, CRM wins.
 │  Terminal status → the run ends after the write-back, with no hand-off.
 │  logs: process_log writeback_validated / writeback_applied / run_ended
 │        audit_log   the HubSpot PATCH + status
 ▼
 ┌ STEP 10 ─ Hand off to text / voice ─── no new surface in the email agent
 │  Triggered by the campaign-complete COLUMN ALONE — not a probability
 │  threshold. The column is set when lqabr_email_status reaches OPENED. Probability is still written, for reporting only.
 │  Ownership passes; the email agent stops acting on the lead. Terminal.
 │  logs: process_log handoff_condition_met
 └──────────────────────────────────────────────────────────────────────────
```

### The four streams, and what each answers

| Stream | Question it answers | Written at |
|---|---|---|
| `system_log` | *Is the service itself healthy?* | continuously — not tied to a step |
| `process_log` | *Where did this run get stuck, and what did the agent decide?* | 3 · 4 · 5 · 6 · 7 · 8 · 9 · 10 |
| `audit_log` | *Did this request go out, and what came back?* | 2 · 4 · 5 · 7 · 8 · 9 |
| `model_log` | *What did this run cost?* | 6 only — one record per lead |

Every record in all four is keyed by `object_id` + `run_id`, so one run
reassembles across every layer. Credential-shaped fields are redacted;
bearers appear only as a `bearer_fingerprint`.

---

## 3. Contracts

**Correlation token** — `"<object_id>:<run_id>"`. Bound at step 3, sent to
Mailgun as `v:lqabr_correlation_token` at step 7, parsed back at step 8, and
the key on every log record in between.

**Run state** — `object_id + run_id → {contact_id, email, message_id, skill,
status, delivered, clicked, campaign_complete, terminal, events[]}`.

**HubSpot properties** (system of record):

| Property | Written at | Values |
|---|---|---|
| `lqabr_email_status` | 7 · 9 | PENDING · SENT · DELIVERED · OPENED · FAILED · BOUNCED |
| `probability` | 9 | 0–100, increments from `lqabr_core/probability.py` only |
| `email_campaign_complete` | 9 | boolean — set when `lqabr_email_status` reaches OPENED (confirmed 2026-08-04). A click maps to OPENED too, so one rule covers both. |
| `object_id` | read at 5 | the campaign chunk key |

**Status precedence** (which status won):
`delivered < opened < clicked < failed < stopped < unsubscribed < complained < bounced`

---

## 3b. The deployed HTTP contract (added 2026-08-04)

Step 2 is only real if the gateway can actually reach step 3. It could not.

**What was listening.** The deployed `lqabr-dev-email-agent` ran
`adk api_server agents/email/src` on port 8080 — the *generic ADK runner*.
Its routes are ADK's, confirmed against `google-adk==2.3.0`:

```
POST /apps/{app}/users/{u}/sessions[/{sid}]    create a session first
POST /run        {appName:"src", userId, sessionId, newMessage:{...}}
POST /run_sse    ·  WS /run_live
GET  /health  ·  /version  ·  /list-apps  ·  /docs  ·  /openapi.json
```

`appName` is `"src"` because `adk api_server` accepts "a path pointing
directly to a single agent folder" and names the app after that folder.
There is no `/`, no `/readyz`, and no `/healthz` — which is exactly what the
4 Aug console check saw (`/openapi.json` 200, `/` and `/readyz` 404).

Meanwhile text_voice was deployed with a *domain* surface
(`POST /hubspot/lead`, `POST /vapi/report`). So the gateway faced two stage
agents with two unrelated contracts, disagreeing even on the health path.
Setting `LQABR_EMAIL_AGENT_URL` to the ADK runner would have dispatched a
domain call into something that cannot parse one.

**The fix** — `agents/email/src/service_app.py`, the email agent's own
FastAPI surface, the same shape as text_voice:

| Route | Step | Notes |
|---|---|---|
| `POST /hubspot/campaign` | 2 → 3-7 | `{"object_id": "..."}`; `trigger_id` accepted as an alias. `limit`, `dry_run`, `run_id` optional. |
| `POST /mailgun/events` | 8 → 9 | HMAC verified first, always. |
| `POST /engagement/sync` | 8 → 9 | The Mailgun **tool call** — "give me the status of these track IDs" for one run. |
| `GET /health`, `GET /healthz` | — | **Both**, identical payloads. Nothing downstream should have to know which spelling we picked. |
| `GET /` | — | Service identity + route index, so a smoke-test curl describes the service instead of 404ing. |
| `GET /runs/{object_id}/{run_id}` | — | Run state: "where did this run get stuck". |

Status codes the gateway can act on:

| Condition | Code | Why |
|---|---|---|
| no `object_id`/`trigger_id` | 400 | nothing ran |
| gateway token configured and wrong/absent | 401 | — |
| bad Mailgun HMAC | 401 | — |
| run state unwritable (`RunStateError`) | 503 | transient, the run never started — retry |
| bearer or HubSpot failure | 502 | fault is upstream, not in the request |

The campaign route is **synchronous by design**: Cloud Run throttles CPU to
near zero once a response is returned, so answering 202 and finishing the
batch in the background would silently strand half the leads. `limit` /
`LQABR_EMAIL_BATCH_LIMIT` is what keeps a run inside the request timeout.

**Deployment.** A third `SERVICE_KIND=service` runs `uvicorn service_app:app`;
`agent` (ADK) and `webhook` are unchanged for everything else.
`docker-compose.yml` builds `lqabr-dev-email-agent` from `service` now, and
`SERVICE_NAME_OVERRIDES` in `config.sh` pins the Cloud Run service name — so
the **URL does not change** and `LQABR_EMAIL_AGENT_URL` stays valid. Only the
contract inside the container moves.

`adk web|run|api_server agents/email/src` still works for local development;
`agent.py`/`root_agent` are untouched. `LQABR_EMAIL_MOUNT_ADK=1` mounts the
ADK runner under `/adk` for anything mid-transition.

**Auth posture (revised 2026-08-04).** One service, `LQABR_EMAIL_ROUTES=all`,
deployed `--allow-unauthenticated` — Mailgun cannot present a Google ID
token and Swaroop's design has it pushing to this same service. Each entry
proves itself instead:

- `/mailgun/events` — the Mailgun HMAC on every event, no bypass flag;
- `/hubspot/campaign` and `/engagement/sync` — currently UNAUTHENTICATED.
  The `LQABR_EMAIL_GATEWAY_TOKEN` / `X-LQABR-Gateway-Token` check was
  removed 2026-08-05 (the gateway wasn't sending the header). Rev 3's
  scoped JWTs are the intended fix once the gateway track defines them.

## 3c. Triggers, container initiation and health (added 2026-08-04)

> Recorded because Swaroop asked for it explicitly (12:12): *"In the
> functional requirements or in the functional steps, I want you to record
> the triggers — what is actually initiating the agent, where the trigger is
> coming from, and how the trigger is happening, and how the container is
> initiating. And when the container initiates, what is the healthy status of
> the container?"*

### The two triggers

| # | Trigger | Comes from | How it arrives | Starts a run? |
|---|---|---|---|---|
| 1 | **Campaign** | HubSpot campaign → agent gateway → sandbox | `POST /hubspot/campaign` with `{"object_id": "..."}`. Carries the id and nothing more; profiles stay in HubSpot. | Yes — binds `object_id + run_id`, steps 3→7 |
| 2 | **Engagement** | Mailgun | `POST /mailgun/events`, HMAC-signed, pushed to **this same service**. Optionally `POST /engagement/sync` to pull the status of a run's track IDs via the Mailgun tool call. | No — attaches to an existing run's state, steps 8→9 |

Both land on **one service, one endpoint, one image**. There is no separate
webhook service and no second URL to configure.

### The agent does not listen for the gateway

Swaroop, 5:08: *"It's a push request, right? API gateway sends the request to
our agent and agent will pick that request and start working. Why would it
listen to API gateway?"*

At rest the service has **zero instances** — nothing is running, nothing is
polling, nothing is warm. The sequence is:

```
  0 instances
      │  trigger reaches the gateway
      ▼
  gateway identifies the request and forwards it to the sandbox
      │
      ▼
  sandbox resolves the registered endpoint; finds no instance
      │
      ▼
  sandbox spins a container  ──  ~32s cold start today (Swaroop, 9:44);
      │                          target <10s via a bootstrap cache, later
      ▼
  uvicorn binds 0.0.0.0:$PORT  ──  $PORT is 8080 on Cloud Run
      │
      ▼
  system_log: container_started {component, routes, gateway_token_required}
      │
      ▼
  the run does its work
      │
      ▼
  system_log: container_stopped   →   back to 0 instances
```

### What "healthy" means for this container

| Signal | Path | Healthy response |
|---|---|---|
| Liveness / readiness | `GET /health` **and** `GET /healthz` | `200 {"status":"ok","service":"email_agent","version":"2","routes":"all"}` |
| Identity / route index | `GET /` | `200` with the served route list — never a bare 404 |
| Startup record | `system_log` | one `container_started` line carrying the route mode and whether the gateway token is enforced |
| Shutdown record | `system_log` | one `container_stopped` line |

Both health spellings answer identically and are served the moment the
process binds the port — before any run starts, and with no dependency on
HubSpot or Mailgun being reachable. A probe therefore reports the
**container**, not the upstreams; upstream failures surface as 502/503 on the
work routes instead, so a bad HubSpot token never reads as an unhealthy
container.

`GET /` and `GET /readyz` returned 404 on the previous ADK-runner image,
which is what made a healthy deployment look broken during the 4 Aug console
check. `/` now answers; `/readyz` is deliberately not added — two spellings
are already one more than necessary.

### Nothing in this agent keeps itself alive

Swaroop, 18:43: *"the reason I'm constantly forcing you guys to not use
webhooks is the cost. [A webhook] is a scheduler. It has to run somewhere...
you have to keep that agent constantly up and running. Every 30 seconds...
that's an expense."*

Enforced structurally, not by convention:

- no scheduler, no background thread, no `while True`, no `asyncio` task
  anywhere in the agent or in `mcp/mailgun` — asserted by
  `mcp/tests/test_mailgun_events.py::test_nothing_in_the_module_schedules_itself`;
- the Mailgun events page walk is bounded (`_MAX_PAGES`), so a wrong filter
  cannot burn the container's time budget;
- `POST /hubspot/campaign` is synchronous and bounded by
  `LQABR_EMAIL_BATCH_LIMIT` — it returns and lets the instance go, rather
  than answering 202 and holding a container that Cloud Run has already
  throttled to near-zero CPU.

## 3d. Where credentials come from (added 2026-08-04)

Every credential resolves through `lqabr_core.secrets`, governed by
`LQABR_SECRETS_SOURCE` — deployed as `secret_manager`, which ignores the
environment entirely so a stray literal cannot shadow the real value. Each
resolution logs the secret NAME and its source, never the value.

| Credential | Secret | Reached by |
|---|---|---|
| HubSpot bearer (steps 4/5/9) | `lqabr-hubspot-access-token` | `mcp/hubspot/auth.py` — needs `LQABR_HUBSPOT_AUTH_MODE=secret_manager`; `env` bypasses Secret Manager |
| Mailgun send + events tool call (7/8) | `lqabr-mailgun-api-key` | `lqabr_core.mailgun`, `mcp/mailgun/events.py` |
| Mailgun HMAC (8) | `lqabr-mailgun-webhook-signing-key` | `verify_webhook_signature` |
| Step-6 model key | `lqabr-anthropic-api-key` / `lqabr-google-api-key` | `lqabr_core.model.ensure_provider_credentials` — litellm and google-genai read the key from the ENVIRONMENT, so it is resolved from Secret Manager and injected for them |
| Gateway shared secret | `lqabr-email-gateway-token` *(not created yet)* | read directly from the environment |

The API path needs `google-cloud-secret-manager`; the image installs
`lqabr-core[gcp]`. Without it an environment variable is the only thing that
can resolve a secret.

**Failure shape.** A credential that cannot be resolved is configuration, not
a bug, and never an opaque 500:

| Where | Response |
|---|---|
| `POST /hubspot/campaign`, `POST /engagement/sync` | `503 secret-error: <secret name> …` |
| `POST /mailgun/events` | `503 secret-error: cannot verify the Mailgun signature …` — the event is **deferred, not processed**: authenticity cannot be established, and a 5xx makes Mailgun retry |
| Bearer obtained but rejected upstream | `502 auth-error: …` |

## 4. Deviations from the design doc — for confirmation

The doc marks the repository layout *"proposed … to be confirmed before code
changes"*. These are the confirmations being asked for:

1. **`src/enums.py`, not `src/enum.py`.** The tree line says `enum.py`; steps
   7, 8 and the diagram all say `enums.py`. `enum` would also shadow the
   stdlib module. Implemented as `enums.py`.
2. **`src/runstate.py` added.** Run state is written by `outreach` (7) and
   read by `events` (8); putting it in either would make one import the
   other. It is its own module.
3. **`agents/email/`, not `agents/email_agent/`.** The doc uses both. The
   existing repo directory is `agents/email/` and the sibling agents match
   it, so nothing was renamed.
4. **`mcp/hubspot/crm.py` delegates to `lqabr_core.crm.HubSpotClient`** for
   the read path rather than re-implementing it. That client already holds
   the property mapping confirmed against live HubSpot, the contact↔company
   association walk, and the retry policy. New surface (trigger-id search,
   campaign-complete write) is implemented directly in `crm.py`.
5. **`auth.py` default backend is Secret Manager.** The HubSpot credential is
   already provisioned there as `lqabr-hubspot-access-token`; the process
   authenticates with its own workload identity and is handed the credential
   at runtime, cached for the run. Nothing is hard-coded and nothing is read
   from a checked-in file. A true OAuth2 client-credentials backend ships
   alongside it and is selected by `LQABR_HUBSPOT_AUTH_MODE=oauth2` — a
   config change, not a code edit. **`auth.py` is owned by the lead profile
   agent's author**; this is an implementation for them to take over.
6. **The package is named `mcp/` per the doc** and deliberately does not
   depend on the PyPI package of the same name. Because the design mandates
   in-process loading rather than a standalone server, the MCP SDK is not a
   dependency and the names never collide. If that ever changes, rename the
   package first.

## 5. Open questions carried from the design

- **`email_campaign_complete` is a placeholder name** pending confirmation
  against the owning HubSpot schema. Env-overridable
  (`LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY`) so renaming is config.
- **`object_id` as a contact property is unconfirmed.** If HubSpot rejects
  a filter on it, `leads_for_trigger` falls back to the not-yet-emailed
  queue and says so loudly on `process_log` — it never silently returns the
  wrong set.
- **Custom HubSpot triggers are paid-tier.** On the free tier step 1 does
  not exist as designed; `run_email_campaign(object_id)` can still be
  driven by the gateway, Cloud Scheduler or an operator in the meantime.
- **An event whose token matches no run state has no path today** — flagged
  as `unresolved`, by design.
- **Run state is container-local.** Correct for local dev and a single
  long-lived instance; a durable store (Firestore/GCS) must replace
  `RunStateStore` before scaled Cloud Run, because `opened`/`clicked`
  legitimately arrive days after a restart.
- **`model_log` content is off by default** — enabling it would put prospect
  PII in the logs, which the doc flags as unsettled.
- **Suppression and multi-touch are out of scope** for the Rev 5 MVP.
  `enums.SUPPRESSING_EVENTS` is the set that work will consume.

---

## 6. Running it

```bash
# setup
pip install -e packages/lqabr_core
pip install -r agents/email/requirements.txt
cp agents/email/.env.example agents/email/.env      # then fill it in

# tests — everything mocked, no credentials, no network
python3 -m pytest -c tests/pytest.ini -q

# the agent — local development only; Cloud Run runs service_app, not ADK
adk web agents/email/src            # browser dev UI
adk run agents/email/src            # terminal

# the service (gateway entry + Mailgun push, one process)
cd agents/email/src && uvicorn service_app:app --port 8080

# see the four streams locally
LQABR_EMAIL_LOG_DIR=./.logs adk run agents/email/src
```

Agent tools: `run_email_campaign(object_id, limit, dry_run)` ·
`preview_email(contact_email)` · `send_outreach_email(contact_email)` ·
`get_lead_status(contact_email)` · `list_email_queue(object_id)` ·
`get_run_state(object_id, run_id)`.
