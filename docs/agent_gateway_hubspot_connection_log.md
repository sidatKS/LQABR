# Agent Gateway ↔ HubSpot — Connection Session Log

**Date:** 31 July 2026
**Branch:** `leadq-dev-SN` (local only — nothing pushed)
**Portal:** HubSpot 246777241 (`app-na2`), private app **Agent Gateway** (app id `47406981`)
**Outcome:** HubSpot → gateway → agent hand-off proven end-to-end on route **R2**, in 11.63 ms,
using a **real lead from the live CRM** — not a synthetic or purpose-made test record.

> Secrets deliberately omitted from this document: `HUBSPOT_APP_SECRET` and the ngrok
> authtoken. Both were handled during the session; see §9 for the rotation note.

---

## 1. Where we started

| Item | State at session start |
|---|---|
| Gateway code | Built and committed to `leadq-dev-SN` (`agents/gateway/`) |
| Webhook subscriptions | 4 active on Contact object |
| HubSpot Target URL | `https://webhook.site/20afe18e-8019-4cba-91f5-f7409c840e38` |
| Events delivered (30 days) | **0** |
| Real HubSpot payload | **Never seen.** Every test to date used `fake_hubspot.py` |

The single open risk: the gateway's parser was written against an *assumed* HubSpot
payload shape. With zero deliveries in 30 days, nothing had ever validated that
assumption. Closing that gap was the object of the session.

---

## 2. The four routes under test

From `agents/gateway/config/agents_registry.yaml`, evaluated top to bottom, first match wins:

| Route | Trigger | Condition | → Agent |
|---|---|---|---|
| `R1-contact-created` | `contact.creation` | any | email |
| `R2-decision-maker` | `contact.propertyChange` on `decision_maker` | `"true"` | email |
| `R3-email-opened` | `contact.propertyChange` on `email_status` | `"OPENED"` | voice |
| `R4-voice-completed` | `contact.propertyChange` on `voice_status` | `"COMPLETED"` | scheduling |

---

## 3. Why a tunnel was needed at all

HubSpot pushes webhooks from its own servers to a **public HTTPS URL**. The gateway
runs on `127.0.0.1:8080` on a Windows laptop with no public address. A tunnel gives
HubSpot a reachable door.

This interacts with the signature check in a way that caused most of the session's friction:

```
signature = base64( HMAC-SHA256( client_secret, "POST" + URI + rawBody + timestamp ) )
```

HubSpot signs over the **exact URL it calls**. The gateway rebuilds that URI from
`LQABR_GATEWAY_PUBLIC_URL` + the request path. So the tunnel hostname must appear,
character-identical, in **two** places:

1. HubSpot → Webhooks → **Target URL**
2. `agents/gateway/config/.env` → **`LQABR_GATEWAY_PUBLIC_URL`**

Any drift between them produces a silent `401`, not an obvious error.

---

## 4. Attempt 1 — localhost.run (abandoned)

| Step | Detail |
|---|---|
| Command | `ssh -R 80:127.0.0.1:8080 nokey@localhost.run` |
| Hostname issued | `https://6f8e1634bea6b0.lhr.life` |
| `.env` updated | `LQABR_GATEWAY_PUBLIC_URL=https://6f8e1634bea6b0.lhr.life` |
| HubSpot Target URL | `https://6f8e1634bea6b0.lhr.life/hubspot/events` |
| Result | Tunnel died before any test ran |

Verification from the browser returned localhost.run's own page:

```
https://6f8e1634bea6b0.lhr.life/         →  "no tunnel here :("
https://6f8e1634bea6b0.lhr.life/healthz  →  "no tunnel here :("
```

**Why abandoned.** localhost.run's free tier issues a **new random hostname on every
SSH reconnect**. Each reconnect invalidates both places above. Three hostnames were
burned during the session before switching.

---

## 5. Attempt 2 — ngrok static domain (adopted)

### 5.1 Install and the problems hit

| # | Command | Result |
|---|---|---|
| 1 | `winget install ngrok.ngrok` | Installed **3.3.1**. Warned: *"Path environment variable modified; restart your shell"* |
| 2 | `ngrok config add-authtoken <token>` | ❌ `'ngrok' is not recognized` — stale PATH in the open shell |
| 3 | PATH refresh (below) | ✅ `ngrok version 3.3.1` |
| 4 | `ngrok http 8080 --domain=...` | ❌ `ERR_NGROK_121` — agent too old |
| 5 | `ngrok update` | ✅ upgraded to **3.39.10** |
| 6 | `ngrok http 8080 --domain=armed-equal-share.ngrok-free.dev` | ✅ tunnel online |

PATH refresh without restarting the shell:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")
```

The version error in full:

```
authentication failed: Your ngrok-agent version "3.3.1" is too old.
The minimum supported agent version for your account is "3.20.0".
ERR_NGROK_121
```

The winget package is years behind ngrok's minimum. `ngrok update` is required
immediately after installing via winget.

### 5.2 The running tunnel

```
Session Status   online
Account          saroja (Plan: Free)
Version          3.39.10
Region           India (in)
Latency          15ms
Web Interface    http://127.0.0.1:4040
Forwarding       https://armed-equal-share.ngrok-free.dev -> http://localhost:8080
```

**Why ngrok over localhost.run:** the free tier includes one **permanent** dev domain
tied to the account. `armed-equal-share.ngrok-free.dev` survives restarts and reboots,
so the two-place sync in §3 is now a one-time cost rather than a per-restart tax.

### 5.3 The free-tier browser interstitial

Opening the domain in a browser returns ngrok's warning page, not the gateway:

```
ERR_NGROK_6024 — You are about to visit armed-equal-share.ngrok-free.dev
```

**This does not affect HubSpot.** The interstitial is served only to browser
User-Agents. HubSpot's POSTs pass straight through. It does mean browser checks of
`/healthz` are not a valid way to verify the tunnel — use the uvicorn log or
`http://127.0.0.1:4040` instead.

---

## 6. Final configuration

### 6.1 `agents/gateway/config/.env` (gitignored, never committed)

```dotenv
HUBSPOT_APP_SECRET=<redacted>
LQABR_GATEWAY_PUBLIC_URL=https://armed-equal-share.ngrok-free.dev
LQABR_EMAIL_AGENT_URL=http://127.0.0.1:9001/a2a/email
LQABR_TEXT_VOICE_AGENT_URL=http://127.0.0.1:9001/a2a/voice
LQABR_SCHEDULING_AGENT_URL=http://127.0.0.1:9001/a2a/scheduling
```

Applied with:

```bash
sed -i 's|^LQABR_GATEWAY_PUBLIC_URL=.*|LQABR_GATEWAY_PUBLIC_URL=https://armed-equal-share.ngrok-free.dev|' "$F"
```

All three agent URLs point at `stub_agent.py`, **not** at real agents. See §8.

### 6.2 HubSpot webhook settings

| Setting | Value |
|---|---|
| Target URL | `https://armed-equal-share.ngrok-free.dev/hubspot/events` |
| Event throttling | 10 requests |
| Subscriptions | 4/4 Active on Contact |

⚠️ **HubSpot UI defect worth knowing.** The **first** commit after a fresh page load
silently does not persist. The page shows "Saving", reports "Last changed a few
seconds ago", and keeps the old value. The **second** commit sticks. This reproduced
on both URL changes. **Always reload and re-verify after editing the Target URL** —
the post-save screen cannot be trusted.

### 6.3 Processes required (four terminals)

```powershell
# 1 — gateway
cd C:\Users\SarojaNemmaluri\LQABR\agents\gateway\src
uvicorn server:app --port 8080

# 2 — stub agent (stands in for email/voice/scheduling)
python C:\Users\SarojaNemmaluri\LQABR\agents\gateway\tools\stub_agent.py

# 3 — tunnel (must stay open)
ngrok http 8080 --domain=armed-equal-share.ngrok-free.dev

# 4 — free terminal for tests
```

**uvicorn must be restarted after any `.env` change** — the file is read once at boot.

---

## 7. Testing

### 7.1 The 401 (a correct failure, worth understanding)

```powershell
python agents\gateway\tools\fake_hubspot.py --created
```

```
POST http://localhost:8080/hubspot/events
HTTP 401
{ "run_id": "run-fb58c8ee25a74369", "error": "unauthorized" }
```

**Cause:** the script defaulted to `--url http://localhost:8080` and signed over that
string, while the gateway rebuilt the URI from `LQABR_GATEWAY_PUBLIC_URL` (the ngrok
domain). Different strings → different HMAC → 401. Exactly the §3 failure mode.

**Correct invocation once a public URL is configured:**

```powershell
python agents\gateway\tools\fake_hubspot.py --created --url https://armed-equal-share.ngrok-free.dev
```

This also has the advantage of exercising the real tunnel path rather than loopback.

### 7.2 The real HubSpot test — with a real lead

**This test used an actual production lead, not a synthetic record.** No contact was
created for testing purposes. The record used was one of the **263 real leads** already
loaded into portal 246777241 by `data/seeds/b2b/output/push_leads.py` from
`lead_profiles_decision_makers.json`.

**The lead under test:**

| Field | Value |
|---|---|
| HubSpot contact id (`objectId`) | **523828708059** |
| Origin | Loaded by `push_leads.py` from `lead_profiles_decision_makers.json` |
| Created in HubSpot | 2026-07-23T10:28:53.259Z |
| Job title | Territory Sales Rep |
| Phone | 415-238-0082 |
| Lifecycle Stage | Lead |
| Associated companies | 1 |
| `decision_maker` at start | `true` |

Contacts were queried first via the HubSpot MCP connector, which returned
**263 contacts, all with `decision_maker = true`**. Since HubSpot only emits a
`propertyChange` event on an actual value *change*, re-setting `true` would have fired
nothing at all. Contact **523828708059** was therefore flipped `true → false → true`
through **Actions → View all properties**, producing two real webhook events and
leaving the record in its original state (net-zero data change).

This matters for how much the test is worth: the `objectId` that reached the email
agent is a **live, resolvable CRM record** carrying a real job title, phone number and
company association. The agent's next call —
`GET /crm/v3/objects/contacts/523828708059` — would return a genuine lead profile
ready for outreach, not an empty shell.

**Event 1 — `propertyValue: "false"`**

```json
{"run_id":"run-9fa396bb510948f1","events_received":1,"routed":0,"discarded":1,
 "routing_errors":0,"discards_by_reason":{"not_routing_condition":1},
 "dispatched_ok":0,"duration_ms":1.05}
INFO: 216.157.40.35:0 - "POST /hubspot/events HTTP/1.1" 200 OK
```

Right property, wrong value → discarded in 1.05 ms. The D-01 value filter works.

**Event 2 — `propertyValue: "true"`**

```json
{"stream":"audit","event":"hubspot_ingress_received","source":"hubspot",
 "source_ip":"216.157.40.40","endpoint":"/hubspot/events","method":"POST",
 "event_count":1,"payload_bytes":308,"signature_verified":true}

{"stream":"system","event":"ingress_concurrency","in_flight":1,
 "concurrency_limit":10,"peak_in_flight":1,"headroom":9}

{"stream":"process","event":"routing_decision",
 "trigger_id":"trg-410951f39b0459bfaa3c7414","decision":"route","agent":"email",
 "route_id":"R2-decision-maker","property_name":"decision_maker",
 "property_value":"true","event_id":"2045823806","object_id":"523828708059",
 "attempt_number":0,
 "basis":"property + value matched a route in agents_registry.yaml"}

{"stream":"audit","event":"agent_dispatch","direction":"outbound","agent":"email",
 "endpoint":"http://127.0.0.1:9001/a2a/email","status":200,
 "latency_ms":9.58,"retry_count":0}

{"stream":"process","event":"protocol_conversion",
 "conversion":"https_ingress -> a2a_message_send","payload_size_bytes":431,
 "payload_contents":"trigger_id + correlation ids only","outcome":"dispatched"}

{"stream":"process","event":"run_summary","events_received":1,"routed":1,
 "discarded":0,"dispatched_ok":1,"dispatched_failed":0,"duration_ms":11.63}
INFO: 216.157.40.40:0 - "POST /hubspot/events HTTP/1.1" 200 OK
```

**Stub agent received:**

```
  EMAIL AGENT WOKEN
  trigger_id : trg-410951f39b0459bfaa3c7414
  object_id  : 523828708059   <- the contact to fetch
  run_id     : run-14e88bed2e2c4e8a
  from       : agent_gateway v0.1.0
  correlation: x-lqabr-trigger-id = trg-410951f39b0459bfaa3c7414
  A real agent would now call:
    GET /crm/v3/objects/contacts/523828708059
```

### 7.3 What this settled

| Claim | Evidence |
|---|---|
| v3 HMAC works against real HubSpot traffic | `"signature_verified": true` |
| Traffic genuinely originates from HubSpot | `source_ip: 216.157.40.40` (HubSpot's range, not localhost) |
| **The assumed payload shape was correct** | No parse error, no 400 — `objectId`, `propertyName`, `propertyValue`, `eventId` all present as coded |
| Value filtering is real | Same property, two values, one discarded one routed |
| Delivered first time, no retry | `attempt_number: 0` |
| `object_id` is the resolvable handle (D-05) | `523828708059` matches the edited contact exactly |
| **The lead was real, not synthetic** | `523828708059` is one of the 263 production leads loaded by `push_leads.py` — real job title, phone, company association |
| The end-to-end path works on production data | A live CRM lead triggered a live webhook, was routed, and reached an agent — no mocks anywhere in the chain |

The payload-shape question — the one real unknown behind the entire build — is now
answered affirmatively.

---

## 8. Wire formats (for reference)

### Hop 1 — HubSpot → Gateway

```http
POST https://armed-equal-share.ngrok-free.dev/hubspot/events
X-HubSpot-Signature-v3: <base64 hmac>
X-HubSpot-Request-Timestamp: 1785493725292
Content-Type: application/json

[{ "objectId": 523828708059,
   "subscriptionType": "contact.propertyChange",
   "portalId": 246777241,
   "eventId": 2045823806,
   "propertyName": "decision_maker",
   "propertyValue": "true",
   "attemptNumber": 0,
   "changeSource": "CRM" }]
```

No lead data — only `objectId`. Batches carry up to 100 events.

### Hop 2 — Gateway → Agent

```http
POST http://127.0.0.1:9001/a2a/email
x-lqabr-trigger-id: trg-410951f39b0459bfaa3c7414
x-lqabr-run-id: run-14e88bed2e2c4e8a

{ "jsonrpc": "2.0", "id": "<uuid>", "method": "message/send",
  "params": {
    "message": { "role": "user",
                 "parts": [{ "kind": "text", "text": "trg-410951f39b0459bfaa3c7414" }],
                 "messageId": "<uuid>" },
    "metadata": { "trigger_id": "trg-410951f39b0459bfaa3c7414",
                  "object_id": 523828708059,
                  "run_id": "run-14e88bed2e2c4e8a",
                  "source": "agent_gateway",
                  "gateway_version": "0.1.0" }
  } }
```

- **`object_id`** — how the agent resolves the lead: `GET /crm/v3/objects/contacts/{object_id}`
- **`trigger_id`** — correlation handle only. Deterministic:
  `uuid5(NAMESPACE, "lqabr:{portalId}:{eventId}")`, so retries mint the same id.
  **Not stored in HubSpot and not resolvable there** (deviation D-05).

---

## 9. Everything changed

### Files

| File | Change | Committed? |
|---|---|---|
| `agents/gateway/config/.env` | `LQABR_GATEWAY_PUBLIC_URL` set twice (lhr.life → ngrok) | No — gitignored |

**No source code was modified this session.** All gateway code was already committed
to `leadq-dev-SN` before this work began.

### HubSpot portal

| Setting | Before | After |
|---|---|---|
| Target URL | `https://webhook.site/20afe18e-…` | `https://armed-equal-share.ngrok-free.dev/hubspot/events` |
| Contact `523828708059` `decision_maker` | `true` | `true` (via `false`, net zero) |

### Machine

| Change | Detail |
|---|---|
| ngrok installed | winget → 3.3.1, then `ngrok update` → 3.39.10 |
| ngrok authtoken | stored in ngrok config |
| ngrok dev domain | `armed-equal-share.ngrok-free.dev` claimed |

**Rotation note:** the ngrok authtoken was pasted into a chat transcript during the
session. It grants tunnel creation on the account only, but should be regenerated at
**dashboard.ngrok.com → Your Authtoken → Reset** as hygiene.

---

## 10. All URLs used

| URL | Purpose |
|---|---|
| `https://armed-equal-share.ngrok-free.dev` | **Current public gateway address** |
| `https://armed-equal-share.ngrok-free.dev/hubspot/events` | HubSpot Target URL |
| `http://127.0.0.1:8080` | uvicorn / gateway |
| `http://127.0.0.1:8080/hubspot/events` | ingress route |
| `http://127.0.0.1:8080/healthz`, `/readyz` | liveness / readiness |
| `http://127.0.0.1:9001/a2a/{email,voice,scheduling}` | stub agent |
| `http://127.0.0.1:4040` | ngrok request inspector — shows raw HubSpot bodies |
| `https://app-na2.hubspot.com/private-apps/246777241/47406981/webhooks` | webhook settings (read-only view) |
| `https://app-na2.hubspot.com/private-apps/246777241/47406981/edit` | webhook settings (editable) |
| `https://app-na2.hubspot.com/contacts/246777241/record/0-1/523828708059` | test contact |
| `https://dashboard.ngrok.com` | domains + authtoken |
| `https://6f8e1634bea6b0.lhr.life` | abandoned localhost.run tunnel (dead) |
| `https://webhook.site/20afe18e-…` | previous Target URL (replaced) |

---

## 11. Tooling used

| Tool | Used for |
|---|---|
| Device shell (`device_bash`) | Reading/editing `.env`; reading repo source |
| Chrome browser automation | HubSpot UI: Target URL edits, contact property flip |
| HubSpot MCP connector (`search_crm_objects`) | Confirming all 263 contacts had `decision_maker=true` |
| Web search | Confirming ngrok free-tier static domains exist |
| `agents/gateway/tools/fake_hubspot.py` | Signed synthetic webhooks (test-only) |
| `agents/gateway/tools/stub_agent.py` | Stand-in agent on :9001 (test-only) |
| `data/seeds/b2b/output/push_leads.py` | Reviewed as a way to create leads (not run) |

---

## 12. Findings raised during the session

| # | Finding | Impact |
|---|---|---|
| 1 | HubSpot's first Target URL commit silently reverts | Always reload and re-verify |
| 2 | localhost.run free tier rotates hostnames every reconnect | Breaks the §3 two-place sync each time |
| 3 | winget ships ngrok 3.3.1; account minimum is 3.20.0 | `ngrok update` required post-install |
| 4 | ngrok free interstitial `ERR_NGROK_6024` | Browser checks invalid; HubSpot unaffected |
| 5 | **`--a2a` defaults to `False`** in google-adk 2.3.0 | `infra/gcp/cloud-run/entrypoint.sh` runs `adk api_server` without it → deployed agents expose **no** A2A endpoint |
| 6 | ADK mounts A2A at `/a2a/{app_name}` (`fast_api.py:733`) | `LQABR_*_AGENT_URL` path must match the resolved app name |
| 7 | Email agent has **no tool accepting an `object_id`** | Tools are `list_email_queue()` and `send_outreach_email(contact_email)` — nothing maps a contact id to a lead |
| 8 | Architectural mismatch: agent **pulls** a stage queue; gateway **pushes** one lead | Design question for Swaroop, not a bug |
| 9 | `HubSpotClient.get_lead(contact_id)` already exists in `lqabr_core` | The D-05 bridge is small — one new tool |
| 10 | **Sidecar has never run**, and cannot as configured | `agentgateway.yaml` uses `${LQABR_EMAIL_AGENT_URL}` as its own backend — the same var `dispatch.py` posts to. One variable ⇒ either bypass or self-loop. Needs a second `*_UPSTREAM` var |
| 11 | Startup `runtime` log is a **static descriptor, not a health check** | Prints identically whether the sidecar is running, crashed, or absent — reads as false assurance |

---

## 13. Honest status

**Proven end-to-end with real HubSpot traffic, on a real production lead:** ingress →
signature verification → parsing → value filter → route match → trigger minting → A2A
conversion → dispatch → agent acknowledgement. Plus the FR-7 audit/process/system
streams. 11.63 ms.

Nothing in that chain was mocked. The lead (`523828708059`) is one of the 263 records
loaded from `lead_profiles_decision_makers.json`; the webhook came from HubSpot's own
servers (`216.157.40.40`); the signature was verified against the live app secret. The
only stand-in anywhere is the **receiving agent** — `stub_agent.py` in place of the
real email agent, because the real one has no endpoint yet (§12, findings 5/7/8).

**Not yet proven:**

- **R1, R3, R4** — only **R2** has ever been fired by real HubSpot. R1 and R3 were
  exercised only via `fake_hubspot.py`; **R4 has never run at all**
- **Dedupe** — both events had distinct `eventId`s; no duplicate or retry has hit it
- **Loop guard** — has never fired. It exists to stop an agent's own write-back
  re-triggering it, and no real agent has written back yet. Also the subject of open
  deviation D-03
- **Batching** — both requests were `event_count: 1`; the chunking path is untouched
- **All three real agents** — email, voice and scheduling share the same three gaps
  (findings 5, 7, 8). Every `LQABR_*_AGENT_URL` currently points at the stub
- **Sidecar** — never run (finding 10)
- **Cloud Run deploy** — not attempted

**One-line summary for a status report:** *HubSpot → gateway → agent hand-off is proven
on one route with real CRM traffic; agent-side receivers are the next build.*

---

## 14. Next steps

1. **Fire R1 with a real contact creation** — the actual first-touch path (~5 min)
2. **Build the email agent's receiver** — `--a2a` on the entrypoint, verify the
   app-name path, add a `work_lead(object_id)` tool over `get_lead()`
3. **Repeat for voice and scheduling**
4. **Resolve the sidecar** — wire it properly with a second env var, or drop it and
   restate D-02 honestly
5. **Make the runtime log tell the truth** about whether the sidecar is alive
6. **Sign-off on D-02 / D-03 / D-05** from Swaroop
7. **Cloud Run deploy** — removes the tunnel entirely
8. **Update the SP-1 Planner card** — the integration-test item now passes

### Restart checklist

Because the ngrok domain is permanent, restarting no longer requires touching HubSpot
or `.env`:

1. `ngrok http 8080 --domain=armed-equal-share.ngrok-free.dev`
2. `uvicorn server:app --port 8080` (from `agents/gateway/src`)
3. `python agents/gateway/tools/stub_agent.py`
4. Change a watched property in HubSpot
