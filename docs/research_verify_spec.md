# LQABR Research Agent — verify spec

> **§0 · DOCUMENT CONTROL**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `research_verify_spec` |
> | **Version** | `1.0` |
> | **As of** | 2026-08-29 |
> | **Component** | `research` → service `lqabr-dev-research` |
> | **Companion** | `docs/research_deploy_spec.md` owns build/push/deploy |
> | **Executable form** | `agents/research/infra/04_verify.sh` + `infra/verify_client.py` |
>
> **Precedence:** the code wins; then this document; then any script. Derived from a read of
> `agents/research/**` on 2026-08-29. **Citation form:** `research_verify_spec §5.4`.

---

## §1 · ⚠ Why this agent is the dangerous one to verify

Summary writes **one ticket**. The gateway routes **one trigger**. A research *campaign*
writes `lead_context` onto **every contact in an industry** — and each of those writes trips
the gateway's `R2-lead-context` route, which hands the lead to the **Email agent**.

So a single careless verification run can start real outreach to an entire industry.

**Therefore: this spec's default mode never runs a campaign.** Layers A, B and the
read-only C checks prove the service is correctly deployed and can reach everything it
needs. A campaign is opt-in via `RUN_CAMPAIGN=1`, and §6.4 states what it costs.

---

## §2 · Context manifest

| Path | What it settles |
| --- | --- |
| `agents/research/src/service_app.py` | routes, `_health_payload()`, `/mcp/tools`, startup events, the accept guards |
| `agents/research/src/pipeline.py` | `run_campaign` / `run_research`, every step and its obs event |
| `agents/research/packages/research_core/mcp/hubspot.py` | `list_leads_by_industry`, `write_context` |
| `agents/research/packages/research_core/secrets.py` | `_env_name` — the `env` source mapping |
| `agents/research/infra/config.sh` | expected deployed values |
| `docs/research_deploy_spec.md` | what was deployed |

---

## §3 · Running it

```bash
cd agents/research
bash infra/04_verify.sh                    # safe: no campaign, no writes
RUN_CAMPAIGN=1 TICKET_ID=<post> bash infra/04_verify.sh   # ⚠ writes to real leads
```

⚠ Layer C needs an **in-VPC Cloud Run job**: `lqabr-dev-research` is `ingress=internal`, so
a laptop curl returns 404 even with a valid ID token. That is the control working.

---

## §4 · Layers

| Layer | Question | Where |
| --- | --- | --- |
| **A** | what Cloud Run says the service *is* | host, `gcloud` |
| **B** | what it said as it booted | host, Cloud Logging |
| **C** | what it can actually reach and do | in-VPC job |

---

## §5 · The flow, and the input set at every hop

The **only** route the gateway drives is the campaign one. Its
`agents_registry.yaml` has exactly one entry for this agent — `R-blog-summary`,
`ticket.propertyChange` on `blog_summary`.

### §5.1 · Hop 1 — gateway → `POST /research/campaign/a2a`

Body is an `A2AEnvelope` (JSON-RPC `message/send`, or a plain object). `campaign_target()`
resolves:

| Field | Meaning |
| --- | --- |
| `objectId` | ⚠ a **published post / Ticket**, never a contact |
| `limit` | max leads to fan out to |

Two guards, both returning a *rejection* rather than failing later:

| Condition | Response |
| --- | --- |
| no `objectId` | rejected: `payload carries no objectId` |
| the event's record kind ≠ `post` | rejected: ``bad-data: this route takes a post, but the hand-off is a HubSpot <type> — id <n> is a <kind>`` |

The second exists because *"the alternative is a read that fails three steps later with a
CRM error that reads like a record went missing."*

### §5.2 · Hop 2 — ⚠ accept is not completion

The route emits `http_in`, schedules the pipeline as a **FastAPI `BackgroundTask`**, and
returns immediately:

```json
{"status": "accepted", "objectId": "...", "run_id": "...", "mode": "campaign"}
```

⚠ **A 200 here means the work was queued, not done.** Every real outcome is in the obs
stream. `_guarded()` wraps the runner so that anything uncaught still emits
`run_crashed{route, run_id, objectId, reason}` — otherwise *"the run simply never
happened"*. Verification must therefore read the log, not the HTTP response.

### §5.3 · Hop 3 — `read_blog` (via MCP)

`get_blog_summary` on the ticket → the post and its `blog_industry`.
⚠ No industry ⇒ `bad-data: the post carries no blog_industry, so there is no industry to
select leads by`. **This is the direct coupling to the summary agent**: if summary wrote the
ticket without a valid `blog_industry`, every campaign off that post refuses.

### §5.4 · Hop 4 — `list_leads` ⚠ bypasses the MCP

`list_leads_by_industry` (`mcp/hubspot.py:167`). With `use_direct_lead_lookup=true` (the
default) this calls **HubSpot directly** via `hubspot_direct.py`, *"the one read that
bypasses the MCP, because the MCP has no lead-listing tool."*

That is why research holds `LQABR_HUBSPOT_ACCESS_TOKEN` — a documented exemption from "the
MCP is the only door", enforced read-only by its tests. It is also why
`mcp_tool_list_leads` shows under `/mcp/tools` `missing` and is *not* asserted at startup.

⚠ **Two returns that must not be confused**, and the code is explicit:

| Return | Meaning | Outcome |
| --- | --- | --- |
| `None` | "we could not ask" | ⚠ campaign **refuses**: `crm-error: … the campaign is not run rather than run against an unknown set of leads` |
| `[]` | "asked, nobody matched" | a valid completed campaign, `leads_found: 0` |

### §5.5 · Hop 5 — per lead

For each id: `read_lead` (MCP) → `research` (Anthropic + web search) → `write_context`
(MCP), writing `settings.hubspot_context_property`, default **`lead_context`**.
Events: `campaign_lead_start` / `campaign_lead_done{status, chars, error}`.

The blog is read once and passed down — *"a campaign over N leads must not fetch the same
post N times."*

### §5.6 · Hop 6 — outcome

`campaign_complete{objectId, status, industry, leads_found, written, failed, skipped}`,
where `status` is `completed` (no failures) · `failed` (nothing written or skipped) ·
`partial`.

⚠ Every `lead_context` written here fires the gateway's `R2-lead-context` route → Email
agent. **The campaign is the trigger for outreach**, which is what §1 is about.

---

## §6 · Assertions

### §6.1 · Layer A — control plane

| id | Asserts | Fails when |
| --- | --- | --- |
| A1 | the service exists | never deployed |
| A2 | latest revision `Ready=True` | container never became healthy |
| A3 | one revision serves 100% | a split left an old revision live |
| A4 | `ingress=internal` | the agent is internet-reachable |
| A5 | SA is `lqabr-agent-dev` | fell back to the default compute SA |
| A6 | on the VPC, `all-traffic` egress | ⚠ cannot reach the internal MCP at all |
| A7 | a volume mounted at `/app/logs` | `config.yaml` logs to a path absent from the image |
| A8 | `LQABR_RESEARCH_SECRETS_SOURCE=env` | ⚠ `secret_manager` raises at import — research does **not** ship `google-cloud-secret-manager` |
| A9 | both secrets bound: `LQABR_ANTHROPIC_API_KEY`, `LQABR_HUBSPOT_ACCESS_TOKEN` | the `env` source reads exactly those names (`secrets.py:_env_name`) |
| A10 | `LQABR_RESEARCH_DRY_RUN=0` | writes suppressed |
| A11 | `LQABR_RESEARCH_MCP_BASE_URL` points at the live MCP | a guessed hostname resolves to nothing |

### §6.2 · Layer B — startup

Read `jsonPayload.event` on the serving revision. ⚠ Not `textPayload`.

| id | Event | Note |
| --- | --- | --- |
| `B-service_start` | `service_start` | |
| `B-mcp_startup_check_ok` | `mcp_startup_check_ok` | asserts only read_lead / read_blog / write |
| `B-no_check_failed` | `mcp_startup_check_failed` **absent** | under `warn` the service starts anyway |
| `B-no_unreachable` | `mcp_startup_check_unreachable` **absent** | distinct event — the MCP could not be reached at all, usually A6 |

### §6.3 · Layer C — read-only (default)

| id | Check | Pass criterion |
| --- | --- | --- |
| C1 | `GET /health` | `status: UP`, `mcp.reachable: true`, `dry_run: false` |
| C1b | `logging.degraded` | empty |
| C2 | `GET /mcp/tools` | the three **called** tools present; `list_leads` absent is `WARN C2b` (§5.4) |
| C3 | `GET /` | identity + route index, incl. the campaign route |
| C4 | `POST` campaign route with **no `objectId`** | rejected with `payload carries no objectId` — proves the guard, writes nothing |

### §6.4 · Layer C — campaign, opt-in only

Enabled by `RUN_CAMPAIGN=1 TICKET_ID=<post objectId>`.

| id | Check | Pass criterion |
| --- | --- | --- |
| C5 | `POST` the campaign route with a real ticket | `{"status":"accepted"}` |
| C6 | the obs stream for that `run_id` | terminates in `campaign_complete`, not `run_crashed` |

⚠ **What C5/C6 cost:** one Anthropic call per lead, one `lead_context` write per lead, and
one gateway `R2-lead-context` trigger per lead — i.e. **real outreach to every contact in
that post's industry**. Use `limit` to bound it, and only run it against a portal you are
willing to contact.

---

## §7 · Failure decode

| id | Cause | Fix |
| --- | --- | --- |
| A6 | no VPC | cannot reach the internal MCP; redeploy with the network flags |
| A8 | `secret_manager` | ⚠ runtime `ImportError` — research has no `google-cloud-secret-manager`. Must be `env` |
| A9 | a secret unbound | the `env` source reads the uppercased, underscored secret name — nothing else |
| A11 | wrong MCP URL | was a guessed hostname once; must be the live service |
| `B-mcp_startup_check_unreachable` | the MCP could not be reached | almost always A6, or the MCP is down — `bash infra/gcp/mcp/02_probe.sh` |
| `B-mcp_startup_check_failed` | tool names absent | the service still starts under `warn`; it fails at read time |
| C1 `mcp.reachable:false` | same as above | |
| C2 blocking | a called tool is missing | align read_lead/read_blog/write to the MCP's four |
| C4 accepted | ⚠ the no-objectId guard is gone | a malformed hand-off would run a campaign against nothing |
| C6 `run_crashed` | uncaught error in the background task | the event names the reason; the HTTP 200 told you nothing |
| C6 `crm-error: could not list leads` | the industry lookup failed | `None` ≠ empty — the campaign correctly refused (§5.4) |
| C6 `bad-data: no blog_industry` | the post has no industry | fix the ticket via the summary agent first (§5.3) |

---

## §8 · Negative constraints

| # | Forbidden | Consequence |
| --- | --- | --- |
| N1 | Running a campaign to "check it works" | ⚠ real outreach to an entire industry |
| N2 | Reading success from the HTTP 200 | accept ≠ completion (§5.2) |
| N3 | Treating `leads_found: 0` as a failure | it is a valid answer; `None` is the failure |
| N4 | `--freshness` instead of a revision/run_id filter | stale entries read as current |
| N5 | Reading `textPayload` | the obs stream is `jsonPayload` |
| N6 | Setting `LQABR_RESEARCH_GCP_PROJECT` | applies only to the `secret_manager` source (A8) |

---

## §9 · Out of scope / open items

- **The gateway→research hop is not exercised end to end.** C5 posts the envelope directly;
  it does not prove the gateway can reach this service. That needs `WIRE_AGENTS=1` on the
  gateway plus a real HubSpot trigger.
- ⚠ **Portal coupling.** `LQABR_HUBSPOT_ACCESS_TOKEN` decides which portal research reads
  and writes. If the gateway verifies webhooks from a different portal, every `objectId`
  handed over will be unresolvable here — and it will present as a CRM error, not as a
  portal mismatch.
- **`use_direct_lead_lookup` can only be turned off once the MCP grows a lead-listing tool**
  (§5.4); until then it is load-bearing for campaign mode.

---

## §10 · Live findings, 2026-08-29 — first run of this spec

Revision `lqabr-dev-research-00002-lqm`, image `lqabr-dev-research:0.1.0`, default mode
(`RUN_CAMPAIGN=0`) — **no campaign, no writes, no outreach.**

**A1–A11 PASS · B PASS · C1, C1b, C2, C3, C4 PASS · WARN C2b · exit 0.**

Of the three components verified in this session, research was the only one whose live
deployment already matched its spec with no corrections needed: `ingress=internal`, on
`lqabr-vpc/lqabr-run-uscentral1` with `all-traffic` egress, SA `lqabr-agent-dev`,
`SECRETS_SOURCE=env`, both secrets bound, `/app/logs` mounted, `DRY_RUN=0`, and
`MCP_BASE_URL` pointing at the live MCP.

`WARN C2b` is the expected steady state, not a defect: `list_leads_by_industry` is absent
from the MCP and `use_direct_lead_lookup=true` routes that one read to HubSpot directly
(§5.4). It becomes a real failure only if `use_direct_lead_lookup` is ever set false before
the MCP grows a lead-listing tool.

**Not proven by this run**, and deliberately so: hops 3-6 (§5.3-§5.6). C4 exercises the
reject guard; nothing exercises `read_blog` → `list_leads` → per-lead write, because doing
so contacts real leads. The first honest end-to-end proof will be a real HubSpot trigger
with the gateway wired (`WIRE_AGENTS=1`), against a portal you are willing to contact.
