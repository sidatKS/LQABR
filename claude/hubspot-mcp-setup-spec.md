# Specification — Connect a New HubSpot Account to the LQABR MCP Server

**Version:** 3.0 · 2026-08-25 · Author: Stephen Miller · Supersedes v2
**Audience & scenario:** an operator who has exactly two things — (1) the LQABR MCP server
**Docker image** and (2) a **newly created HubSpot account** — and this document. Following
§5's master prompt end-to-end yields a running MCP server connected to that new portal with
all four tools verified. No GCP account, no source code, and no prior LQABR environment are
required (GCP Secret Manager is offered as the hardened production option; migration from an
existing portal is Appendix A).

**Basis:** live analysis of the image `tne736/lqabr-mcp-server:latest`
(digest `sha256:373e9d36…`), its embedded source (`/app/hubspot-crm-mcp-server/`), and
verified live tool calls (2026-08-21/24). Nothing here is a generic assumption.

---

## 1. Architecture & Overview

### 1.1 Objective

Run the LQABR HubSpot MCP server from the image, point it at the new HubSpot portal, and
verify every tool. The server is configured **entirely by environment variables** — no code
changes, no rebuild.

### 1.2 Components

| Component | What it is |
|---|---|
| **MCP server** `lqabr-hubspot` v0.1.0 | FastMCP 3.4.7 app inside the image; exposes 4 tools over streamable-http on `:8080/mcp` |
| **HubSpot portal** | The new account — system of record for all lead + blog-summary state; identified by its Hub ID |
| **Private-app token** | The single credential; it alone determines which portal the server talks to |
| **Token store** | Path A: plain env var (quick start) · Path B: Google Secret Manager (production) — §3.3 |
| **MCP clients** | Anything speaking MCP streamable-http: raw JSON-RPC/curl, Claude Code, ADK `MCPToolset` |

### 1.3 The four tools

| Tool | R/W | Purpose |
|---|---|---|
| `upsert_lead_profile` | WRITE | The ONLY lead writer. Upserts Company, upserts Contact, associates them. Idempotent — create-or-update is one upsert; safe to call twice. |
| `get_lead_profile` | read | Shared read path, keyed **only** on the contact's `hs_object_id`. |
| `upsert_blog_summary` | WRITE | The ONLY writer to the blog store. One HubSpot **Ticket** per published post, upsert-keyed on `blog_published_at`. |
| `get_blog_summary` | read | Blog read path by `blog_published_at`. Not-found is a valid result (`found:false`), never an error. |

### 1.4 Request / data / auth flow

```
MCP client ──JSON-RPC over streamable-http──▶ :8080/mcp (FastMCP session layer)
   │  initialize → mcp-session-id header → notifications/initialized → tools/call
   ▼
tools.py (transport & shape only, no logic)
   ▼
crm.py / blog_summary.py (validate → search by dedup key → PATCH-or-POST → associate)
   │  per call: fresh run_id (UUID) correlates every log line; audit line per HubSpot call
   ▼
auth.py: HUBSPOT_AUTH_MODE=private_app (baked into the image, fail-closed)
   ▼
config_secrets.py: LQABR_SECRET_BACKEND = env → read HUBSPOT_PRIVATE_APP_TOKEN
                                        = gcp → read Secret Manager via mounted ADC
   ▼
https://api.hubapi.com  (Authorization: Bearer <token>)
```

Retry policy (one place, `crm.py`): 3 retries, exponential backoff on 429/5xx honouring
`Retry-After`; transport exceptions (reset/timeout/DNS) retried the same way; 401/403
retried exactly once with a forced token refresh. Auth *misconfiguration* is systemic —
the server halts the call loudly instead of blaming a record. Bad records are always
reported with reasons (`errors/*.jsonl`), never silently dropped.

### 1.5 Key design facts to keep in mind

- **The token IS the portal selection.** Nothing else in the config chooses the portal.
- Fail-closed everywhere: unset `HUBSPOT_AUTH_MODE` raises (the image sets it to
  `private_app`); the `gcp` secret backend never silently falls back to env vars; an empty
  token raises.
- HubSpot wins on conflict (it is the system of record).
- Every tool call gets a fresh `run_id` (UUID) visible in `docker logs` — the correlation
  key for the whole validate → search → write chain of one call. It is never in the
  response payload.

---

## 2. New HubSpot Account Setup

### 2.1 Portal requirements

- Any HubSpot tier works for the objects used (Contacts, Companies, Tickets).
- Free/Starter tiers allow **exactly one ticket pipeline**, and every ticket pipeline must
  have a *closed* stage — HubSpot refuses both a second pipeline and a single-stage one.
  The blog store therefore parks rows in the default pipeline's closed stage (§2.5); on a
  fresh portal the defaults (`0`/`4`) almost always apply, but §5 verifies rather than
  assumes.
- Record the portal's **Hub ID** (top-right account menu, or Settings → Account & Billing).
  It anchors the "am I really talking to MY portal?" check (§6 T4).

### 2.2 Private app + scopes — **MANUAL** (HubSpot UI, ~5 minutes)

Settings → Integrations → Private Apps → *Create private app*, name `lqabr-mcp`. Scopes:

| Scope | Why | Keep after setup? |
|---|---|---|
| `crm.objects.contacts.read` / `.write` | lead read/upsert | yes |
| `crm.objects.companies.read` / `.write` | company upsert + association | yes |
| `tickets` (or granular `crm.objects.tickets.read`/`.write` where offered) | blog summary store | yes |
| `crm.schemas.contacts.write` / `crm.schemas.companies.write` / `crm.schemas.tickets.write` | one-time API property bootstrap (§2.6) | may remove after bootstrap |

Create the app and copy the access token (`pat-na1-…` / `pat-eu1-…`). It is shown **once**.
Put it straight into the token store (§3.3) — never into a chat, a committed file, or shell
history.

### 2.3 Contact properties — **AUTOMATED** by §5 (Properties API)

Standard properties used as-is: `email`, `phone`, `jobtitle`, `firstname`, `lastname`.
Custom properties the bootstrap creates (group `contactinformation`):

| Internal name | type / fieldType | Options / notes |
|---|---|---|
| `employee_id` | string / text | **DEDUP KEY** — searched EQ |
| `decision_maker` | bool / booleancheckbox | wire field `decision_maker_flag`; `"yes"` → true |
| `lead_context` | string / textarea | only non-empty values are ever sent — an upsert can never blank it |
| `lqabr_voice_status` | enumeration / select | exactly: `PENDING`, `INITIATED`, `COMPLETED`, `FAILED`, `VOICEMAIL_LEFT`, `CALL_PLACED` |
| `lqabr_email_status` | enumeration / select | exactly: `PENDING`, `SENT`, `DELIVERED`, `OPENED`, `FAILED`, `BOUNCED` |
| `probability` | number / number | routing thresholds elsewhere in LQABR: voice ≥ 30, scheduling ≥ 60 |
| `last_modfied_voice` | datetime / date | ⚠ INTENTIONALLY MISSPELLED — §2.4 |
| `last_modified_email` | datetime / date | auto-stamped when `email_status` is written |

Wire-name → internal-name mapping the server hardcodes: `voice_status` →
`lqabr_voice_status`, `email_status` → `lqabr_email_status`, `last_modified_voice` →
`last_modfied_voice`. A near-miss enum value is **rejected before anything is written**.

### 2.4 The misspelling is part of the contract

The server hardcodes `VOICE_MODIFIED_PROPERTY = "last_modfied_voice"` (sic — "modfied").
HubSpot internal names are immutable after creation, so on the new portal the property
**must be created with exactly that internal name** (the UI *label* may be spelled
correctly). Creating it as `last_modified_voice` silently breaks every voice-status write —
the only alternative is changing the constant in the server source and rebuilding the
image, which this scenario (image-only) rules out.

### 2.5 Company + Ticket properties — **AUTOMATED** by §5

**Companies** — standard used as-is: `name` (wire `company_name`), `industry`,
`annualrevenue`, `hs_industry_group` (wire `industry_group`), `about_us`, `website`
(wire `website_url`). Custom to create: `company_id` (string/text, **DEDUP KEY**),
`frequency_of_purchase` (string/text).

> ⚠ `industry` is a HubSpot **enumeration**. The server uppercases input but does not
> validate membership — an invalid value (e.g. `Software`) comes back as a HubSpot 400 and
> the lead is reported `status:"failed"`, `failure_kind:"record"` (verified live;
> `COMPUTER_SOFTWARE` succeeds). A fresh portal ships HubSpot's default option set, which
> includes `COMPUTER_SOFTWARE` — §5 confirms rather than assumes.

**Tickets** — native used: `subject`, `hs_pipeline`, `hs_pipeline_stage`. The bootstrap
creates property group `blog_context`, then:

| Internal name | type / fieldType | Notes |
|---|---|---|
| `blog_summary` | string / textarea | |
| `blog_published_at` | string / **text (single-line)** | **UPSERT KEY**, searched EQ as an exact string — text, not datetime, to preserve exact-string semantics. Server validates it parses as an ISO timestamp. |
| `blog_industry` | enumeration / select | exactly: `FINANCIAL_SERVICES`, `LEGAL_SERVICES`, `HEALTHCARE`. A near-miss (e.g. `HEALTH_CARE`) selects **zero leads downstream and raises no error**. |

**Pipeline placement:** the server defaults to `LQABR_BLOG_PIPELINE_ID=0`,
`LQABR_BLOG_STAGE_ID=4` — on a fresh portal that is the default Support Pipeline and its
Closed stage. §5 reads `GET /crm/v3/pipelines/tickets` and overrides the two env vars only
if the portal differs.

**Collision handling:** HubSpot enforces no uniqueness on custom properties, so the server
searches `limit 2` and **refuses to write** when >1 ticket shares a `blog_published_at`,
recording the collision to `errors/blog_summary_mismatch.jsonl`.

### 2.6 Behavioral constraints inherited from HubSpot

- **One contact per email address** (standard `email` property): a second row with the same
  address has its create rejected → reported in `reasons`, never dropped.
- `probability` is a number in HubSpot but a **string** on the MCP wire.

### 2.7 Manual vs automated — summary

| Step | Mode |
|---|---|
| Create HubSpot account; note Hub ID | MANUAL |
| Create private app, grant scopes, copy token | MANUAL |
| Put the token in the token store (§3.3) | MANUAL (one command / one paste) |
| Create all custom properties + `blog_context` group | AUTOMATED (idempotent; 409 = exists = OK) |
| Verify `industry` options / pipeline ids | AUTOMATED (read-only API calls) |
| Load/run container, handshake, all tests | AUTOMATED (§5) |
| Eyeball the test records in the portal UI | MANUAL (final confirmation) |

---

## 3. MCP Configuration

### 3.1 Prerequisites (operator machine)

- **Docker** (Docker Desktop on Windows/macOS, docker-ce on Linux). That is the only hard
  dependency. `curl` for the verification calls (present on all modern OSes).
- The image, one of:
  - registry: `docker pull tne736/lqabr-mcp-server:latest`
  - handed as a file: `docker load -i lqabr-mcp-server.tar`
  Confirm with `docker images` — the digest of the verified build is `sha256:373e9d36…`.
- Path B only: a GCP project, `gcloud` CLI, and ADC
  (`gcloud auth application-default login`).

### 3.2 Server structure (inside the image — for reference; nothing to edit)

```
/app/hubspot-crm-mcp-server/
├── tools.py                # MCP tool surface — transport & shape only
├── crm.py                  # HubSpot HTTP transport, retries, upsert/read logic
├── blog_summary.py         # Ticket-based blog store
├── schema.py               # THE data contract — property names, enums, validation
├── auth.py                 # HUBSPOT_AUTH_MODE resolution (fail-closed)
├── config_secrets.py       # token resolver: env or gcp backend (fail-closed)
├── obs/                    # run_id-correlated logging
└── hubspot_crm_server.py   # FastMCP entrypoint (uvicorn, PID 1)
```

### 3.3 Token delivery — choose ONE path

**Path A — env backend (quick start; this scenario's default).** No GCP anywhere. The
container source supports `LQABR_SECRET_BACKEND=env`, which reads the token from the
`HUBSPOT_PRIVATE_APP_TOKEN` environment variable. To keep the token out of shell history
and transcripts, stage it in a permission-restricted file once:

```bash
mkdir -p ~/.lqabr && chmod 700 ~/.lqabr
# paste the token into this file with your editor — one line, no quotes:
#   ~/.lqabr/hubspot_token
chmod 600 ~/.lqabr/hubspot_token
```

Trade-off to accept knowingly: with Path A the token is visible in
`docker inspect <container>` to anyone with Docker access on that host, and the server's
own docstring marks the env backend as "local development, CI and tests only". Fine for a
single-operator machine; use Path B for anything shared or production.

**Path B — Google Secret Manager (production).**

```bash
printf '%s' "$(cat ~/.lqabr/hubspot_token)" | gcloud secrets create <TOKEN_SECRET> \
  --project <GCP_PROJECT> --data-file=-
```

The ADC identity needs `roles/secretmanager.secretAccessor` on the secret. Rotation is
`gcloud secrets versions add` — picked up within the 900 s in-process cache TTL, no
redeploy.

### 3.4 Startup — the docker run per path

**Path A (env backend):**

```bash
docker run -d --name lqabr-mcp -p 8080:8080 \
  -e LQABR_SECRET_BACKEND=env \
  -e HUBSPOT_PRIVATE_APP_TOKEN="$(cat ~/.lqabr/hubspot_token)" \
  -e LQABR_ERRORS_DIR=/tmp/errors \
  --restart unless-stopped \
  tne736/lqabr-mcp-server:latest
```

**Path B (Secret Manager):**

```bash
docker run -d --name lqabr-mcp -p 8080:8080 \
  -e LQABR_SECRET_PROJECT=<GCP_PROJECT> \
  -e LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=<TOKEN_SECRET> \
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcp/adc.json \
  -e LQABR_ERRORS_DIR=/tmp/errors \
  -v ~/.config/gcloud/application_default_credentials.json:/gcp/adc.json:ro \
  --restart unless-stopped \
  tne736/lqabr-mcp-server:latest
```

Add to either, **only if** §5's pipeline check finds non-defaults:
`-e LQABR_BLOG_PIPELINE_ID=<id> -e LQABR_BLOG_STAGE_ID=<id>`.

Boot takes ~3 s. **Health:**
`curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/mcp` → **406 is healthy**
(streamable-http rejects a bare GET); `000` = still booting or crashed (`docker logs`).

### 3.5 Environment contract — every value

| Variable | Default | Path A | Path B |
|---|---|---|---|
| `HUBSPOT_AUTH_MODE` | `private_app` (baked into image) | keep | keep |
| `LQABR_SECRET_BACKEND` | `gcp` | **set `env`** | keep `gcp` |
| `HUBSPOT_PRIVATE_APP_TOKEN` | — | **set (the token value)** | unused |
| `LQABR_SECRET_PROJECT` | — | unused | **set** |
| `LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN` | `lqabr-hubspot-private-app-token` | unused | **set** (secret id, or full `projects/…/versions/N` to pin) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | unused | **set** `/gcp/adc.json` + mount |
| `LQABR_SECRET_TTL_SECONDS` | `900` | n/a | keep |
| `HUBSPOT_API_BASE` | `https://api.hubapi.com` | keep | keep |
| `HUBSPOT_HTTP_TIMEOUT_SECONDS` / `HUBSPOT_MAX_RETRIES` | `30` / `3` | keep | keep |
| `LQABR_BLOG_PIPELINE_ID` / `LQABR_BLOG_STAGE_ID` | `0` / `4` | verify | verify |
| `LQABR_ERRORS_DIR` | — | set (`/tmp/errors`) | set |
| `MCP_TRANSPORT` / `MCP_HOST` / `PORT` / `MCP_PATH` | `streamable-http` / `0.0.0.0` / `8080` / `/mcp` (baked) | keep | keep |

### 3.6 Transport & session protocol

Streamable-http with mandatory sessions; responses are SSE-framed
(`event: message` / `data: {json}` — parse the `data:` line):

1. `POST /mcp` — `initialize` (protocolVersion `2024-11-05`), headers
   `Content-Type: application/json` and `Accept: application/json, text/event-stream`.
   Capture the **`mcp-session-id` response header**.
2. `POST /mcp` — `notifications/initialized`, carrying that `mcp-session-id` header.
3. Every `tools/list` / `tools/call` carries the header. Without it: `400 Missing session ID`.

### 3.7 Client configuration

**Claude Code** (`.mcp.json` in the project/working directory):

```json
{ "mcpServers": { "lqabr-hubspot": { "type": "http", "url": "http://localhost:8080/mcp" } } }
```

**ADK agents** — `MCPToolset` with streamable-http params at the same URL (endpoint URLs
config-driven, never hard-coded). **Raw curl** — per §3.6; this is what §5/§6 use.

---

## 4. Security & Credentials

### 4.1 Secrets inventory

| Secret | Where it lives | Never |
|---|---|---|
| Private-app token | Path A: `~/.lqabr/hubspot_token` (chmod 600) + container env · Path B: Secret Manager | in Git, in chat/transcripts, in shell history, in logs |
| ADC json (Path B only) | local user profile, mounted read-only | committed, copied into the image |

### 4.2 Git hygiene

If this setup lives near a repo: `.gitignore` must cover `.env`, `*.token`,
credential json files, and the errors directory (`errors/` jsonl files can quote HubSpot
payloads). `.env` files are git-ignored **templates of names**, never of values.

### 4.3 In-image guarantees (verified in the container source)

- The token is held in memory and **never logged** — the obs layer logs a redacted
  fingerprint (`<N chars, ends XXXX>`) and, on Path B, the secret's *resource name*.
- Fail-closed: empty/missing token raises `SecretConfigError` with an actionable message;
  the `gcp` backend has **no silent env fallback**; unknown backends raise.

### 4.4 Token & scope hygiene

- One private app per environment; name it so the portal audit trail is legible.
- Drop the three `crm.schemas.*.write` scopes after property bootstrap.
- Rotation: Path A — update the file, recreate the container. Path B — add a secret
  version; picked up within 900 s.
- If the token ever leaks: HubSpot → Private Apps → rotate token; update the store; done.

---

## 5. Single-Prompt Setup Procedure — THE MASTER PROMPT

**Operator does two things manually first** (they cannot be delegated):
1. §2.2 — create the private app in the new portal, copy the token.
2. §3.3 Path A — put the token in `~/.lqabr/hubspot_token` (chmod 600).

Then fill the CONFIG block and paste the whole prompt into a fresh Claude Code (or
equivalent coding-agent) session on the machine with Docker and the image.

````text
You are an experienced integration developer. Connect the LQABR HubSpot MCP server (Docker
image) to a NEWLY CREATED HubSpot portal and verify all four MCP tools end-to-end. Work
step-by-step and deterministically; only stop if a CONFIG value is missing or a step fails
in a way you cannot fix. NEVER print, echo, or log the token value.

CONFIG (fill in before submitting; placeholders in <>):
- HUB_ID          = <the new portal's Hub ID, from the HubSpot account menu>
- IMAGE           = tne736/lqabr-mcp-server:latest   # or a local tag / tar file path
- CONTAINER_NAME  = lqabr-mcp
- PORT            = 8080
- TOKEN_FILE      = ~/.lqabr/hubspot_token           # one line, the pat-… token, chmod 600
- MIGRATING       = no                               # "yes" only if replacing an older
                                                     # LQABR portal — then also do Appendix A
                                                     # steps from the spec

STEP 0 — Preconditions. Verify and report:
  a) docker daemon reachable (docker version).
  b) The image is present (docker images | grep lqabr-mcp-server). If IMAGE is a .tar path,
     docker load -i it first. If neither, docker pull $IMAGE.
  c) $TOKEN_FILE exists, is one line, starts with "pat-" — check with test/grep, DO NOT
     print the contents.
  d) Nothing else is bound to $PORT (docker ps, or curl returning 000/refused is fine).

STEP 1 — Portal identity precheck (read-only; proves the token belongs to the intended
portal BEFORE anything is created). Read the token into a shell variable from $TOKEN_FILE
(never echo) and call:
  GET https://api.hubapi.com/account-info/v3/details   (Authorization: Bearer $TOKEN)
  EXPECT: HTTP 200 and portalId == $HUB_ID. On 401 → the token is wrong/revoked: stop and
  tell the operator to re-copy it from the private app. On portalId mismatch → the token
  belongs to a DIFFERENT portal: stop.

STEP 2 — HubSpot property bootstrap (idempotent: HTTP 409 / "already exists" = success;
report each property as created / already-existed / failed):
  Contacts (POST /crm/v3/properties/contacts, groupName "contactinformation"):
    employee_id string/text · decision_maker bool/booleancheckbox ·
    lead_context string/textarea ·
    lqabr_voice_status enumeration/select [PENDING, INITIATED, COMPLETED, FAILED,
      VOICEMAIL_LEFT, CALL_PLACED] ·
    lqabr_email_status enumeration/select [PENDING, SENT, DELIVERED, OPENED, FAILED,
      BOUNCED] ·
    probability number/number ·
    last_modfied_voice datetime/date   <-- INTERNAL NAME INTENTIONALLY MISSPELLED
      ("modfied"): the server hardcodes it; creating it spelled correctly breaks voice
      writes. The LABEL may be "Last Modified Voice". ·
    last_modified_email datetime/date
  Companies (POST /crm/v3/properties/companies, groupName "companyinformation"):
    company_id string/text · frequency_of_purchase string/text
  Tickets: POST /crm/v3/properties/tickets/groups name "blog_context", then
  (POST /crm/v3/properties/tickets, groupName "blog_context"):
    blog_summary string/textarea ·
    blog_published_at string/text single-line (exact-string search key — NOT datetime) ·
    blog_industry enumeration/select [FINANCIAL_SERVICES, LEGAL_SERVICES, HEALTHCARE]
  Checks (read-only):
    GET /crm/v3/properties/companies/industry → options must include COMPUTER_SOFTWARE.
    GET /crm/v3/pipelines/tickets → record the pipeline id and the id of a stage whose
    metadata marks it closed. If they are not "0" and "4", remember them as
    BLOG_PIPELINE_ID / BLOG_STAGE_ID for Step 3.

STEP 3 — Start the MCP server (Path A, env backend — no GCP):
  Remove any container already named $CONTAINER_NAME. Then:
  docker run -d --name $CONTAINER_NAME -p $PORT:8080 \
    -e LQABR_SECRET_BACKEND=env \
    -e HUBSPOT_PRIVATE_APP_TOKEN="$(cat $TOKEN_FILE)" \
    -e LQABR_ERRORS_DIR=/tmp/errors \
    [plus -e LQABR_BLOG_PIPELINE_ID=… -e LQABR_BLOG_STAGE_ID=… if Step 2 found
     non-defaults] \
    --restart unless-stopped \
    $IMAGE
  Wait ~3 s. Health: curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/mcp
  MUST be 406 (406 IS the healthy answer for streamable-http). If 000: docker logs
  $CONTAINER_NAME, fix, retry before continuing.

STEP 4 — MCP handshake and tool discovery (responses are SSE-framed — parse the "data:"
line of each response):
  a) POST http://localhost:$PORT/mcp
     {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":
     "2024-11-05","capabilities":{},"clientInfo":{"name":"setup-verify","version":"1.0"}}}
     headers: Content-Type: application/json; Accept: application/json, text/event-stream
     Capture the mcp-session-id RESPONSE HEADER; send it as a header on EVERY call below.
     EXPECT serverInfo.name == "lqabr-hubspot".
  b) POST {"jsonrpc":"2.0","id":2,"method":"notifications/initialized"}.
  c) POST {"jsonrpc":"2.0","id":3,"method":"tools/list"}
     EXPECT exactly 4 tools: upsert_lead_profile, get_lead_profile, upsert_blog_summary,
     get_blog_summary — and company_name in upsert_lead_profile's inputSchema.

STEP 5 — Test calls, READ-ONLY FIRST (method tools/call; record every result verbatim):
  R1 (read) get_blog_summary {"blog_published_at":"1999-01-01T00:00:00Z"}
     EXPECT found:false, summary:null, isError:false — proves auth + read path, zero writes.
  R2 (read) get_lead_profile {"object_id":"1"}
     EXPECT a clean handled not-found — never an unhandled exception or auth error.
  W1 (WRITE) upsert_lead_profile {"employee_id":"E-TEST-001","company_id":"C-TEST-001",
     "decision_maker_flag":"false","company_name":"Test Company Inc.","firstname":"Test",
     "lastname":"Lead","email":"test.lead@example.com","phone":"+15555550100",
     "job_title":"QA Tester","industry":"COMPUTER_SOFTWARE","lead_ref_id":"test-lead-001"}
     EXPECT status "pushed", contact_action "create", company_action "create",
     associated true. RECORD contact_hs_id and company_hs_id.
  W2 (WRITE) repeat W1 verbatim. EXPECT both actions "update", SAME ids — idempotency.
  R3 (read) get_lead_profile {"object_id": <contact_hs_id from W1>}
     EXPECT found true, company_resolved true, company_name "Test Company Inc.",
     warnings [].
  W3 (WRITE) upsert_blog_summary {"subject":"Test Blog Post","blog_summary":
     "Setup verification test.","blog_published_at":"<today>T00:00:00Z",
     "blog_industry":"FINANCIAL_SERVICES","summary_ref_id":"test-001"}
     EXPECT status "written", action "created". RECORD ticket_hs_id.
  W4 (WRITE) repeat W3 verbatim. EXPECT action "updated" with the SAME ticket_hs_id.
  R4 (read) get_blog_summary with the same blog_published_at.
     EXPECT found true, all four fields round-trip, warnings [].
  N1 (negative) upsert_lead_profile {"employee_id":"E-TEST-002","company_id":"C-TEST-002",
     "decision_maker_flag":"false","industry":"Software"}
     EXPECT status "failed", failure_kind "record", a HubSpot 400 quoted in reasons —
     proves bad data is flagged and reported, never dropped or half-written.
  N2 (negative) upsert_lead_profile {"employee_id":"E-TEST-001","company_id":"C-TEST-001",
     "decision_maker_flag":"false","email_status":"SENDED"}
     EXPECT rejection naming the allowed enum values, BEFORE any HubSpot call
     (no new hubspot audit line in docker logs for it).

STEP 6 — Prove the writes landed in THIS portal:
  a) Direct API with the token: GET /crm/v3/objects/contacts/<contact_hs_id>?properties=
     employee_id → EXPECT employee_id "E-TEST-001". Combined with Step 1's portalId check,
     this binds the created records to $HUB_ID.
  b) docker logs $CONTAINER_NAME: extract the run_id lines for W1 and W3; confirm the
     audit endpoints hit api.hubapi.com. Include both run_ids in the report.
  c) Tell the operator to eyeball contact "Test Lead" (Test Company Inc.) and ticket
     "Test Blog Post" in the portal UI — the human confirmation.

STEP 7 — Final report, exactly this structure:
  1 Environment: image + digest, container name, port, health code, token path (A/env)
  2 HubSpot configuration: Hub ID; properties created vs already-existing; industry
    options confirmed; pipeline/stage ids used; typo-property confirmation
  3 Tools discovered (4 names)
  4 Test results table: R1,R2,W1,W2,R3,W3,W4,R4,N1,N2 — pass/fail + recorded ids
    (contact_hs_id, company_hs_id, ticket_hs_id, run_ids)
  5 Portal verification: Step 1 portalId + Step 6 outcomes
  6 Remaining manual steps: eyeball check; delete-or-keep the three test records
    (E-TEST-001 contact, C-TEST-001 company, Test Blog Post ticket); drop the
    crm.schemas.*.write scopes from the private app
  7 Known limitations observed (single ticket pipeline tier limit; email-uniqueness;
    near-miss blog_industry silently selects zero leads)
  8 Security notes (token visible in docker inspect on Path A; recommend Path B /
    Secret Manager for shared or production hosts)
  Do not delete any HubSpot records yourself.
````

---

## 6. MCP Tool Testing — full test plan

Order is read-only first; **WRITE** rows create real CRM records in the new portal (three
test records total, all clearly named for later cleanup). All `tools/call` invocations
follow §3.6's session protocol.

| # | Test | R/W | Purpose |
|---|---|---|---|
| T1 | Health | — | container up, transport answering |
| T2 | Initialize | — | MCP session established, correct server identity |
| T3 | tools/list | — | discovery: 4 tools, current schemas |
| T4 | Portal identity | read | token belongs to `<HUB_ID>` — runs BEFORE any write |
| T5 | get_blog_summary (absent key) | read | auth + read path, zero writes |
| T6 | get_lead_profile (absent id) | read | not-found handled cleanly |
| T7 | upsert_lead_profile | **WRITE** | create path end-to-end (Contact + Company + association) |
| T8 | upsert_lead_profile repeat | **WRITE** | idempotency — update, no duplicates |
| T9 | get_lead_profile round-trip | read | wrote what we think we wrote |
| T10 | upsert_blog_summary + repeat | **WRITE** | create then update, same ticket |
| T11 | get_blog_summary round-trip | read | four fields + empty warnings |
| T12 | invalid `industry` | write-attempt | flagged-never-dropped error path |
| T13 | invalid `email_status` | none (pre-write reject) | enum guard fires before network |
| T14 | Portal cross-check | read + manual | records provably in `<HUB_ID>` |

Per-test detail (Purpose in the table; below: Preconditions / Command / Expected / Pass /
Failure / Troubleshooting):

**T1 — Health.** Pre: container started ≥3 s ago. Cmd:
`curl -s -o /dev/null -w "%{http_code}" http://localhost:<PORT>/mcp`. Expected & Pass:
`406`. Fail `000` → booting or crashed: `docker logs`, §8.7. Connection refused → port
mapping: `docker ps`.

**T2 — Initialize.** Pre: T1. Cmd: §3.6 step 1. Expected: `serverInfo.name` =
`"lqabr-hubspot"`, `mcp-session-id` response header present. Fail `400 Missing session ID`
on later calls → the header isn't being sent back: resend on every request, §8.8.

**T3 — tools/list.** Pre: T2 + `notifications/initialized`. Expected: exactly the 4 tools;
`company_name` in the upsert schema. Fail (2 tools / missing field) → stale image: compare
digests, §8.12.

**T4 — Portal identity.** Pre: token staged. Cmd (token in shell var, never echoed):
`GET https://api.hubapi.com/account-info/v3/details` → read `portalId`. Pass: equals
`<HUB_ID>`. Fail: 401 → §8.1; mismatch → wrong token for this portal, §8.5.

**T5 — Blog read, absent key.** Cmd: `get_blog_summary`
`{"blog_published_at":"1999-01-01T00:00:00Z"}`. Pass: `found:false`, `summary:null`,
`isError:false`. Fail with auth text in the result → §8.1/§8.4.

**T6 — Lead read, absent id.** Cmd: `get_lead_profile {"object_id":"1"}`. Pass: clean
not-found, no unhandled exception. Fail 403 → missing contacts scope, §8.3.

**T7 — Lead upsert.** Pre: T4 passed; §5 Step 2 bootstrap done. Cmd: §5 W1 payload. Pass:
`status:"pushed"`, both actions `"create"`, `associated:true`, non-null ids. Fail
`failure_kind:"record"` + 400 naming a property → property missing or enum mismatch
(§8.11); `failure_kind:"transport"` → §8.9/§8.10.

**T8 — Idempotency.** Cmd: T7 verbatim. Pass: actions `"update"`, **same** ids. Fail — new
ids each run → the dedup searches found nothing → `employee_id`/`company_id` properties
missing: re-run the bootstrap.

**T9 — Lead round-trip.** Cmd: `get_lead_profile` with T7's `contact_hs_id`. Pass:
`found:true`, `company_resolved:true`, `company_name` correct, `warnings:[]`. Fail
`company_resolved:false` → association failed: re-check T7's `associated`, then §8.3
(companies scope).

**T10 — Blog upsert + repeat.** Cmd: §5 W3, then W4. Pass: `created` then `updated`, same
`ticket_hs_id`. Fail on create with a pipeline/stage 400 → wrong
`LQABR_BLOG_PIPELINE_ID`/`STAGE_ID`: fix env, recreate container (§2.5).

**T11 — Blog round-trip.** Pass: `found:true`, all four fields match, `warnings:[]`.
Non-empty `warnings` → duplicate `blog_published_at` rows, §8.11(c).

**T12 — Invalid industry (negative).** Cmd: T7 payload but `industry:"Software"`, fresh ids
`E-TEST-002`/`C-TEST-002`. Pass: `status:"failed"`, `failure_kind:"record"`, HubSpot 400
quoted in `reasons`, `isError:false` (the tool succeeded *at reporting*). This is the
flag-never-drop guarantee, working.

**T13 — Invalid enum pre-write (negative).** Cmd: any upsert with `email_status:"SENDED"`.
Pass: rejection naming the allowed values **before** any network call — no new HubSpot
audit line appears in `docker logs`.

**T14 — Portal cross-check.** (a) Direct API GET of T7's contact with the token →
`employee_id` matches; T4 already bound the token to `<HUB_ID>`. (b) Manual: the contact,
company, and ticket are visible in the new portal's UI. Pass: both agree. (If migrating
from an older portal — Appendix A — additionally confirm the records do NOT appear there.)

---

## 7. Verification Checklist

- [ ] New HubSpot account created; Hub ID recorded
- [ ] Private app created with §2.2 scopes; token captured once, straight into
      `~/.lqabr/hubspot_token` (600) or Secret Manager
- [ ] Docker present; image loaded/pulled; digest noted
- [ ] All §2.3/§2.5 custom properties exist — **including the deliberately misspelled
      `last_modfied_voice` internal name** — and the `blog_context` ticket group
- [ ] Company `industry` options include every value the data will use
      (`COMPUTER_SOFTWARE` confirmed for the test)
- [ ] Ticket pipeline + closed-stage ids verified; env overrides set only if ≠ `0`/`4`
- [ ] Container running with the correct §3.5 env for the chosen path; health = 406
- [ ] MCP initialize succeeds; server identifies as `lqabr-hubspot`
- [ ] tools/list: exactly 4 tools, `company_name` present in the upsert schema
- [ ] T4 portal identity: token's `portalId` == Hub ID — **before any write**
- [ ] Read-only tests pass (T5, T6) before any write test runs
- [ ] Write + idempotency + round-trips pass (T7–T11)
- [ ] Negative tests behave (T12, T13): errors reported with reasons, nothing dropped,
      nothing half-written
- [ ] T14: test records visible in THIS portal (and absent from any older portal, if
      migrating)
- [ ] No token value in any file, log, transcript, or shell history produced during setup
- [ ] `crm.schemas.*.write` scopes removed from the private app after bootstrap
- [ ] Setup reproducible: §5 re-run from scratch on a clean machine succeeds

---

## 8. Troubleshooting

Format: **Symptom → Root cause → Diagnose → Fix → Verify.**

**8.1 Authentication failure (401, `TokenError`).** → Token invalid, revoked, or
mis-pasted. → T4 against `/account-info/v3/details`; check the token file is one line,
`pat-` prefix, no trailing newline garbage (`wc -l`, `head -c 8` — never print it all). →
Re-copy from the private app (or rotate); restage; recreate the container (Path A bakes
the value at `docker run`). → T4 then T5 pass.

**8.2 Token rotated but old one still used.** → Path A: env is fixed at container creation.
Path B: 900 s cache TTL. → `docker inspect` env (A) / `secret_resolved` log timestamps
(B). → A: recreate the container. B: wait out the TTL or restart. → T4 passes.

**8.3 Missing scopes (403, "app hasn't been granted all required scopes").** → A §2.2
scope wasn't ticked. → The 403 body names the missing scope. → Add it in the portal's
private-app settings — applies immediately, token unchanged. → Re-run the failing call.

**8.4 `SecretConfigError` at first call.** → Path A: `LQABR_SECRET_BACKEND=env` set but
`HUBSPOT_PRIVATE_APP_TOKEN` empty/unset (e.g. the `$(cat …)` expanded to nothing). Path B:
missing project or accessor role. → `docker logs` — the message says exactly which variable.
→ Fix the env / the file path / the IAM role; recreate the container. → T5 passes.

**8.5 Wrong HubSpot portal.** → The token belongs to a different portal than intended
(multiple portals under one login is the classic cause). → T4 `portalId` mismatch. →
Log into the intended portal, create/re-copy the private-app token there, restage. →
T4 matches; T14 shows the records in the right portal.

**8.6 Wrong/missing environment variables.** → Typo'd `-e` flag; forgot
`LQABR_SECRET_BACKEND=env` (server then tries GCP and fails with a Secret Manager error on
a machine with no GCP). → `docker inspect --format '{{.Config.Env}}'` vs §3.5. → Recreate
the container with corrected flags (env cannot be edited in place). → Health + T5.

**8.7 Server startup failure / health stays 000.** → Port already allocated; container
crashed at boot; (Path B) bad ADC mount path. → `docker logs`; `docker ps` for the port
holder. → Free or change the port; fix the mount source. → Health = 406.

**8.8 `400 Missing session ID` / empty tool list.** → Session protocol not followed. →
Confirm the `mcp-session-id` **response header** from initialize is echoed as a request
header on every subsequent call, including `notifications/initialized`. → Redo §3.6 in
order. → T3 passes.

**8.9 `failure_kind:"transport"` mid-call.** → Network to api.hubapi.com, DNS, or timeout —
already retried 3× with backoff. → docker logs show each attempt and the final error. →
Check egress/proxy/firewall; raise `HUBSPOT_HTTP_TIMEOUT_SECONDS` if the link is genuinely
slow. → Re-run; expect `pushed`.

**8.10 Rate limits (429).** → Burst exceeded portal limits. → Audit lines show
`status: 429` with `Retry-After` honoured. → Usually self-heals; pace callers for
sustained load. → Calls complete without exhausting retries.

**8.11 "Property values were not valid" / missing property / duplicate rows.** →
(a) enum value not in the portal's options (`industry`, `blog_industry`); (b) a §2 custom
property was never created; (c) >1 ticket sharing one `blog_published_at`. → The 400 body
names the property and allowed options; `get_blog_summary.warnings` flags duplicates;
`errors/*.jsonl` in the container records each rejected record. → (a) fix the value or add
the option; (b) re-run the idempotent bootstrap; (c) merge/delete duplicate tickets in the
portal. → Round-trips pass with empty warnings.

**8.12 Wrong image / schema drift (2 tools, or no `company_name`).** → Running an older
build. → `docker inspect --format '{{.Image}}'` vs `docker images --no-trunc`; `tools/list`
shape. → Pull/load the current image and recreate. Note: a `docker push`/`pull` reporting
an **unchanged digest** means nothing was rebuilt — rebuild before pushing (observed live
2026-08-24). → T3 shows the 4-tool schema.

**8.13 Records landing in an unexpected portal.** → §8.5's wrong token, or (if migrating)
an old container/client still pointed at the old server. → T14; `docker ps` for stray
containers; check clients' `.mcp.json` URLs. → Fix the token/URL; stop stray containers;
after verified cutover deactivate the old portal's private app (Appendix A). → T14 passes.

---

## 9. Expected Final Output From the Developer

The §5 master prompt's Step 7 produces this; a reviewer reads only this document.

```markdown
# LQABR MCP — New HubSpot Connection Report      <date> · <operator>

1 Environment           : image + digest · container name · port · health code (406) ·
                          token path used (A/env or B/Secret Manager)
2 HubSpot configuration : Hub ID · properties created vs pre-existing (full list) ·
                          industry options confirmed · pipeline/stage ids used ·
                          last_modfied_voice created with the exact (misspelled) internal
                          name: yes/no
3 Tools discovered      : upsert_lead_profile · get_lead_profile · upsert_blog_summary ·
                          get_blog_summary
4 Test results          : table R1,R2,W1,W2,R3,W3,W4,R4,N1,N2 — pass/fail ·
                          contact_hs_id · company_hs_id · ticket_hs_id · run_ids (W1, W3)
5 Portal verification   : Step 1 portalId check + Step 6 direct-API and UI confirmation
6 Remaining manual steps: eyeball the 3 test records; delete or keep them
                          (E-TEST-001 contact / C-TEST-001 company / "Test Blog Post"
                          ticket); drop crm.schemas.*.write scopes
7 Known limitations     : single ticket pipeline on this tier · one-contact-per-email ·
                          near-miss blog_industry silently selects zero leads
8 Security notes        : Path A token visible via docker inspect on this host —
                          recommend Path B for shared/production; anything else observed
```

**Overall pass:** sections 3–5 all green, and section 2 confirms the misspelled property
was created with the exact internal name. Any red row cites the §8 entry used (or still
needed).

---

## Appendix A — Migrating from an existing LQABR portal (only if `MIGRATING=yes`)

The greenfield flow above is unchanged; add these around it:

1. **Before Step 3:** audit for old-portal references — running `lqabr-mcp-*` containers
   (`docker ps` + `docker inspect` env), client `.mcp.json` URLs, scripts naming the old
   token secret (`lqabr-hubspot-access-token`) or old portal id. List every hit; change
   nothing yet.
2. **Run side-by-side:** keep the old container on its port; run the new one on another
   (e.g. 8081) until verification completes.
3. **Extend T14:** query the OLD server's `get_lead_profile` with the new `contact_hs_id`
   — expect not-found (record ids are portal-scoped). The test record appearing in the old
   portal is the smoking gun for §8.13.
4. **Cutover:** repoint clients to the new port/URL → stop the old container → deactivate
   the old portal's private app (Settings → Private Apps) so nothing can silently write
   there → re-run the old-reference audit and confirm zero live references.
5. **Report:** add the audit findings and cutover status as section 9 of the §9 report.

---

*End of specification. The §5 master prompt is self-contained and may be distributed with
this document; keep both in sync if the server contract changes.*
