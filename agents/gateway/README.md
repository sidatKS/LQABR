# Agent Gateway — HubSpot Trigger Routing

Single ingress for HubSpot events. Decides which agent owns a trigger, records
why, and hands off **a trigger id and the contact's record id — nothing else**.

Built to `Agent_Gateway_Rev3.pdf` (System Architecture · Rev 3 · Functional
Requirements), approved for development 30-Jul-2026. Planner: *SP-1: Agent
Gateway — Build gateway routing service (MVP)*.

The one sentence worth remembering: **no lead-profile data crosses this
service.** The agent receives a record id and fetches the profile itself, direct
from HubSpot. Everything below is in service of that.

---

## Where the code is

```
agents/gateway/
├── src/
│   ├── server.py       HTTP ingress, entry point           Step 2 (transport)
│   ├── router.py       filter value, resolve endpoint,
│   │                   mint trigger_id                     Step 2 (decision)
│   ├── audit.py        run id + trigger id + the decision   Steps 3 & 7
│   └── dispatch.py     hand off to the agent, light         Step 4
├── lib/
│   └── soloai/         co-located runtime adapter (D-02)
│       ├── config.py
│       ├── audit_hooks.py      the four FR-7 log streams
│       └── protocols/
│           ├── http.py         signature, batch, concurrency
│           ├── a2a.py          gateway -> agent, trigger only
│           └── mcp.py          agent -> HubSpot; refuses to proxy profiles
├── config/
│   ├── config.yaml             protocols, audit level, chunk size, timeouts
│   ├── agents_registry.yaml    property + value -> owning agent
│   ├── agentgateway.yaml       sidecar static config
│   └── .env.example            secrets, endpoints — never committed
├── tests/                      154 tests
├── Dockerfile                  gateway + sidecar in one image
└── docker-entrypoint.sh
```

Rev 3 draws this tree as `agent_gateway/` at the repo root with
`gateway/*.py`. It lives at `agents/gateway/` with `src/*.py` instead, to match
the convention every other agent in this repo already follows
(`docs/READ_PRJSTRC_ME.md`). Same files, same responsibilities, one directory level
different.

---

## The flow

```
HubSpot property change
        │  HTTPS POST, JSON array, <= 100 events, trigger_id absent
        ▼
server.py     verify v3 signature · validate envelope · bound concurrency at 10
        ▼
router.py     filter on propertyValue  ─── not the routing condition ──> discard (logged)
              dedupe on eventId (attemptNumber > 0) ── seen ──────────> discard (logged)
              loop guard: never an agent's own write-back ───────────> discard (logged)
              match agents_registry.yaml  ─── no route ──────────────> discard (logged)
              mint trigger_id
              resolve endpoint ─── missing / disabled ───────────────> ROUTING ERROR (503)
        ▼
audit.py      run_id + trigger_id + the property and value behind the decision
        ▼
dispatch.py   A2A message/send  ·  trigger_id + object_id  ·  retries, latency, status
        ▼
Email / Voice agent
        │  GET /crm/v3/objects/contacts/<object_id>
        ▼
HubSpot       ← direct. Bypasses this gateway entirely.
```

**Discard vs routing error.** A discard is normal: HubSpot's free tier fires a
subscription on *every* value of a watched property, so most events are simply
not the one we act on (D-01). A routing error is a failure: the event matched,
but the owning agent has no usable endpoint. Discards return `200`; routing
errors return `503` so HubSpot redelivers. That is how "a lead is never
silently dropped" holds at the HTTP layer.

---

## Routing table

Edit `config/agents_registry.yaml` to change any of this — never the code.

| Route | Trigger | Value acted on | Owning agent |
|---|---|---|---|
| `R2-lead-context` | `lead_context` changed | *(non-empty)* | Email |
| `R3-email-opened` | `email_status` changed | `OPENED` | Voice |
| `R-blog-summary` | `blog_summary` changed (Ticket) | *(non-empty)* | Research |

`R1-contact-created` (Contact created -> Email) was disabled 25-Aug-2026;
email now triggers off `lead_context` only. `R2-decision-maker` was replaced
by `R2-lead-context` earlier -- see `config/agents_registry.yaml` for the
full history in comments.

Onboarding a third agent is: add it under `agents:`, add a route, set its
`endpoint_env`. No code change, no redeploy of routing logic.

---

## Run it

```bash
# from the repo root
pip install -r agents/gateway/requirements.txt
cp agents/gateway/config/.env.example agents/gateway/config/.env   # then fill in

cd agents/gateway/src
uvicorn server:app --port 8080 --reload
```

### Debug mode vs normal mode

Same scheme as the research and summary agents' own `log_mode`: three tiers,
`terse | normal | debug`, set the same way (an env var; no separate "debug"
command since the gateway has no CLI entrypoint of its own, just `uvicorn`):

```bash
# normal mode -- LQABR_GATEWAY_LOG_MODE unset, defaults to "normal"
uvicorn server:app --port 8080 --reload

# debug mode -- unlocks per-event discard detail on the process stream
LQABR_GATEWAY_LOG_MODE=debug uvicorn server:app --port 8080 --reload

# terse mode -- audit stream only
LQABR_GATEWAY_LOG_MODE=terse uvicorn server:app --port 8080 --reload
```

The old `LQABR_GATEWAY_LOG_LEVEL=minimal|standard|verbose` still works if set
(mapped onto the names above), with a one-time `audit_mode_deprecated` notice
on the system stream. Use `LQABR_GATEWAY_LOG_MODE` in new setups.

`logs/agents/gateway/gateway_process.log`, `gateway_audit.log` and
`gateway_system.log` are written the same in every mode -- like the research
agent's `logs/agents/research/agent.log`, the file is always full JSON,
whatever the console shows. That directory is configurable too
(`LQABR_GATEWAY_LOG_DIR`, relative paths resolve against the repo root,
same rule research uses for its own `log_dir`), and every file -- the main
sink and the three per-stream ones -- rotates at `LQABR_GATEWAY_LOG_MAX_BYTES`
(default ~50 MB, same as research) keeping `LQABR_GATEWAY_LOG_BACKUPS`
backups (default 5).

Console shape (`LQABR_GATEWAY_CONSOLE_FORMAT=auto|text|json|off`) only
matters when `audit.sink: file` -- `auto` (the default) echoes a line to the
terminal when one is actually attached, and stays quiet otherwise (Cloud Run,
a pipe); the file itself never changes. That line is coloured and
glyph-marked, the gateway's own counterpart to the research agent's console
output (`▸`/`◂` for a hop in/out, `✓`/`✗`/`!` for outcome, ASCII fallback
`>`/`<`/`+`/`x`/`!` on a non-UTF-8 console) -- a routing decision, an ingress
or dispatch hop, and a run's closing summary each get their own readable
shape; everything else still gets a coloured, aligned line rather than
printing unstyled:

```text
13:40:01 ▸ hubspot   hubspot_ingress_received POST /webhooks/hubspot events=3
13:40:01 ✓ routing_decision           agent=research property_name=hs_lead_status property_value=NEW route_id=r-hs-industry
13:40:01 ◂ research  agent_dispatch http://research:8080/ 200 812ms
13:40:02 ✗ run_summary events_received=5 routed=4 discarded=1 dispatched_ok=3 dispatched_failed=1 1893ms
```

`debug` mode also widens this line the same way it widens the file: every
field is shown in full on indented continuation lines instead of being
summarised or cut.

For local runs without a HubSpot signature, set
`gateway.ingress.signature.enabled: false` in `config.yaml` — do not ship that.

```bash
# tests
python -m pytest -c tests/pytest.ini agents/gateway/tests -q

# container (gateway + agentgateway sidecar, one image)
docker build -f agents/gateway/Dockerfile -t lqabr-agent-gateway .
docker run -p 8080:8080 --env-file agents/gateway/config/.env lqabr-agent-gateway
```

### Endpoints

| | |
|---|---|
| `POST /hubspot/events` | the ingress. The only entry point. |
| `GET /healthz` | liveness |
| `GET /readyz` | 503 unless every enabled agent endpoint resolves **and** the signature config can actually verify a webhook |
| `GET /metrics` | handoff counters |
| `GET /` | what this service carries, and what it refuses to |

### Response codes

| | |
|---|---|
| `200` | every event reached a terminal outcome (dispatched, or discarded) |
| `401` | signature missing, stale, or wrong |
| `400` / `413` | malformed envelope / batch over 100 |
| `503` | something matched but did not land — **HubSpot should retry** |

---

## Observability (FR-7)

Four streams, one JSON object per line to stdout, every record carrying
`run_id` and `trigger_id`.

| Stream | Scope | The gateway writes |
|---|---|---|
| `audit` | network | ingress source/time/endpoint, dispatch destination, status, latency, retry count |
| `process` | gateway | **the routing decision and the property and value it was based on**, discards and why, protocol conversion, payload size |
| `system` | container | memory, ingress concurrency vs the limit of 10, config problems, exceptions |
| `token_model` | model | **N/A** — no model calls. Written once at startup as an explicit exclusion, not omitted. |

The agentgateway sidecar already emits access logs, Prometheus metrics and OTel
spans for every hop. What a proxy cannot produce is *which agent was chosen and
on what basis* — that is what `audit.py` exists for, and why the process stream
always carries `property_name` and `property_value` beside the chosen agent.

Trace one lead:

```
jq 'select(.trigger_id=="trg-…")' < logs   # or the same filter in Cloud Logging
```

### The trigger-only guarantee is enforced, not promised

Three guards, all tested:

* `audit_hooks` **raises** rather than writing a record containing any of the
  nine lead parameters. A log line is the easiest way for profile data to
  escape a service that swore it carried none.
* `protocols/a2a` **raises** if anything outside a small allow-list is attached
  to a dispatch — correlation ids plus `object_id`, and nothing else. So
  `propertyName` and `propertyValue` are in the log but never on the wire.
* values are **redacted**, not just keys. A property *value* is
  config-controlled: subscribe to a profile property in the portal and
  `propertyValue` arrives holding an email address, under a key FR-7 requires us
  to log. Email- and phone-shaped values are scrubbed on the way in, and the
  record says so (`redacted_values`). Redacted rather than raised, because a
  portal misconfiguration must not take the ingress down.

---

## Deviation register

`D-01` is from Rev 3. `D-02` and `D-03` were found while building and need
Swaroop's sign-off.

### D-01 — No custom trigger on the free tier *(from Rev 3)*
HubSpot's free tier cannot filter by property value, select which fields are
sent, or set records per trigger. **Consequence:** value filtering lives in
`router.py`; profile chunking lives in the agent.
**Status:** implemented as specified.

### D-02 — "Solo AI" is a process, not an in-process library
Rev 3 describes Solo AI as a co-located orchestration **library**, imported
in-process from `lib/soloai`, ~500 MB. The product
([agentgateway](https://agentgateway.dev), Solo.io, now Linux Foundation) is a
**Rust data plane** — run as `agentgateway -f config.yaml`, configured by static
YAML, with MCP/A2A backends, Prometheus metrics, OTel tracing and a UI on
`:15000`. There is no Python library to import.

**What we built instead:** agentgateway runs as a **sidecar inside this
gateway's own container** — one image, one deploy, no hop off-box — and
`lib/soloai` is the Python adapter over it, keeping exactly the module tree
Rev 3 specifies. Everything else in the design is untouched: single ingress,
trigger-only payload, HubSpot as system of record, four log streams.

The business logic depends only on `lib/soloai` interfaces, so if a real
in-process library ever ships, it is a change inside that package alone.
`docker-entrypoint.sh` degrades to dispatching direct at the agents if the
sidecar cannot start.

### D-03 — A global "ignore `changeSource=API`" would disable two of the three routes
Rev 3 Step 2 says to *ignore events whose `changeSource` is an agent
write-back (`API`)*. Taken literally that removes:

* `R1` — the Contact is **created by the lead-profile agent** via the API
* `R3` — `email_status=OPENED` is **written by the Email agent**

Only `R2` (a human setting `decision_maker`) would survive, so the pipeline
would stop after the first hop. Rev 3 also states the purpose of the guard:
*so the system does not trigger itself.*

**What we built:** `loop_guard.mode: self_trigger` (default) — an event is
dropped only when the agent that owns the matched route is the agent that
writes that property (declared per agent as `writes:` in the registry). That
prevents self-triggering without severing the three agent-driven hops.
`loop_guard.mode: all_api` preserves the literal reading behind one config
line if review prefers it.

**Needs a decision.** Default stays `self_trigger` until then.

### D-05 — the contact record id travels with the trigger
Rev 3 Step 4 says "send trigger_id only", and Step 6 has the agent resolve a
*chunk* of ~5 profiles by criteria. The problem: **neither the `trigger_id` nor
HubSpot's `eventId` is stored anywhere in the CRM**, so an agent holding one has
no way to reach the lead that actually fired the trigger — Rev 3 acknowledges
this in D-01 ("HubSpot cannot resolve a chunk by trigger ID"). The chunk the
agent gets back may not even contain that lead.

Alternatives considered and rejected: writing the trigger id onto the contacts as
a new property (needs `crm.objects.contacts.write`, makes the gateway a CRM
writer on the hot path, and the lookup is an *eventually consistent* search that
returns zero results for several seconds after the write); and buffering leads in
the gateway until five accumulate (leads sit unworked at low volume, and an
in-memory buffer loses them on a Cloud Run restart).

**What we built:** `objectId` — already present in every webhook payload, and the
contact's real record id — travels in the A2A metadata. The agent does a direct
`GET /crm/v3/objects/contacts/<object_id>`: exact, immediate, strongly
consistent, no new property, no write scope, no buffering. `trigger_id` still
travels as the correlation handle that ties the agent's logs to the routing
decision.

Consequence: an event that matches a route but carries no `objectId` is now a
**routing error** (503, HubSpot redelivers), not a discard — a trigger the agent
cannot act on must not be reported as success.

**Needs sign-off:** a record id is profile-adjacent, so this is a real departure
from "trigger-only". It is still not profile *data* — no name, email, phone or
revenue crosses the gateway, and the log guards are unchanged.

Worth stating plainly for that review: with the registry as shipped, no route
targets an agent that writes the property firing it, so **the `self_trigger`
guard cannot fire and no loop protection is actually active**. That is correct —
there is no loop to protect against, because no agent is triggered by its own
write-back — but it means the guard is a safety net for a future
misconfiguration, not something doing work today.

---

## Hardened after an adversarial review

The first working version was reviewed line-by-line against the spec before it
was called done. Eighteen findings; the ones that changed behaviour, each now
covered by a regression test:

| | |
|---|---|
| **The event loop was blocked.** The handler was `async def` but called blocking `requests.post` and `time.sleep`, so ten concurrent deliveries serialised — measured **30s** for work that takes 3s, with `/healthz` starved for 29s and the container's own 3s HEALTHCHECK killing it mid-batch. Now the pipeline runs in a worker thread behind a semaphore that actually holds the bound of 10. |
| **Discards polluted the dedupe store.** Filtering ran *after* de-duplication and every discard was remembered, so the high-volume "not the routing condition" stream evicted real dispatch records from the bounded LRU — and an evicted record means a **second outreach to a lead already contacted**. Filtering now runs first, per Rev 3's ordering, and only a successful hand-off is ever remembered. |
| **`dedupe or DedupeStore()`** silently replaced the caller's store, because `DedupeStore` defines `__len__` and an empty one is falsy — which made the TTL and cap in `config.yaml` dead config. |
| **The wire carried more than a trigger id** (`objectId`, `propertyName`, `propertyValue`) and the allow-list permitted it. Now correlation ids only; the routing basis stays in the audit trail where FR-7 wants it. |
| **A missing `objectId` was discarded** and answered `200`, so HubSpot never redelivered — a silently dropped lead. `objectId` is audit metadata, not a routing input; those events route normally now. |
| **Profile data could slip past the log guard** three ways: values were never inspected, container-key suppression propagated into lists, and any nested key reusing a container name re-suppressed the check. Values are now scrubbed for email/phone shapes and the suppression is one level deep, non-propagating. |
| **A non-ASCII signature header returned 500** instead of 401 (`hmac.compare_digest` raises `TypeError` on non-ASCII `str`), letting unauthenticated traffic fill the system stream with exceptions. |
| **The replay window was double the intended width** — `abs(age)` accepted 300s of *future* skew too. Now 300s past, 60s future. |
| **Signature verification failed closed in the documented deployment**: `LQABR_GATEWAY_PUBLIC_URL` blank plus uvicorn without `--proxy-headers` meant every real webhook 401'd. Fixed in the entrypoint, and both fail-closed conditions now show up at startup and on `/readyz`. |
| **The dedupe TTL was 15 minutes** against HubSpot's 24-hour retry window. |
| Smaller: routing errors carried `trigger_id: null`; a duplicate `eventId` inside one batch dispatched twice; a missing `eventId` minted a fresh random trigger id per redelivery; payload size measured Python's `repr`; the sidecar's log-correlation headers were never sent; `dispatch_all`'s exception isolation was bypassed; inert config keys removed. |

## Known limits (MVP, deliberate)

**The dedupe store is in-memory, so it is per-instance.** Two Cloud Run
instances do not share it. Mitigated by `trigger_id` being *deterministic* per
HubSpot event — the same event always mints the same id, even when HubSpot sends
no `eventId` — so a duplicate is detectable downstream, plus agents being
expected to be idempotent on trigger id. A shared store (Redis / Firestore) is
the follow-up if instances scale past one.

**Dispatch is sequential within a batch.** Batches run concurrently with each
other (up to the bound of 10), but the events inside one batch are handed off in
order. Fan-out within a batch would multiply load on the agents for no deadline
reason. Revisit if p95 dispatch latency approaches HubSpot's request timeout —
worst case today is 100 events × 3 attempts × the 30s A2A timeout in one
request.

**`/metrics` is JSON, not Prometheus.** The sidecar exposes real Prometheus
metrics for network-level counters; these are the decision-level ones. Worth
merging into one exposition format when the dashboards land.

**Signature verification needs `LQABR_GATEWAY_PUBLIC_URL`.** Cloud Run rewrites
the Host header, so the URI HubSpot signed has to be pinned explicitly, or
verification fails on correct requests.

**No integration test against a live agent yet.** The suite fakes the A2A
transport. The real contract test is with `agents/text_voice` (SP-1, Rao) once
its endpoint is deployable.

**Neither the audit file sink nor the pooled HTTP session is closed on a hard
kill.** The shutdown hook closes the sink on a graceful stop; `SIGKILL` loses
whatever was buffered. Only matters for `sink: file`, which is local dev.

---

## What this service will never do

* hold, cache, log or forward lead-profile data (a record id is not profile data)
* call a model
* let one agent talk to another
* be the system of record — HubSpot is, and on conflict HubSpot wins
