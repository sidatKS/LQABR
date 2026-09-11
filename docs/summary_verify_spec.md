# LQABR Summary Agent — verify spec

> **§0 · DOCUMENT CONTROL**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `summary_verify_spec` |
> | **Version** | `2.0` (supersedes 1.0, which specified the runner instead of the flow) |
> | **As of** | 2026-08-29 |
> | **Component** | `summary` → service `lqabr-dev-summary` |
> | **Companion** | `docs/summary_deploy_spec.md` owns build/push/deploy; this owns proving it works |
> | **Executable form** | `agents/summary/infra/04_verify.sh` + `infra/verify_client.py` |
>
> **Precedence, highest first:** 1. the code (`agents/summary/**`); 2. this document;
> 3. any script, prompt or recollection. **A script is not evidence.** Every input set in
> §5 is cited to a file and line and was read on 2026-08-29. If `04_verify.sh` and this
> document disagree, this document is right and the script is stale.
>
> **Citation form:** `summary_verify_spec §5.4`.
>
> **Design goal — one run, no iteration.** Every assertion is executed by one command,
> emits `PASS <id>` / `FAIL <id> <reason>`, and has a row in §7 naming its cause and fix.

---

## §1 · Why a green deploy proves nothing here

This service starts successfully while badly wrong. `MCP_STARTUP_CHECK=warn` lets it boot
with tool names that do not exist; `DRY_RUN=1` lets it compute and discard; a malformed
upsert key lets it report a successful write that HubSpot never stored. Each of those is
invisible until a real run, and two of them are invisible even then unless the write is
read back.

Verification therefore has to walk the **whole flow** and assert the input set at each hop.

---

## §2 · Context manifest — read these, do not recall

| Path | What it settles |
| --- | --- |
| `agents/summary/src/schema.py` | the inbound contract and the model contract |
| `agents/summary/src/pipeline.py` | step order, the write gate, what `status` follows |
| `agents/summary/packages/summary_core/types.py` | `SummaryResult`, `as_hubspot_text()` |
| `agents/summary/packages/summary_core/mcp/hubspot.py` | `_iso_published_at`, `_normalise_industry`, the four-arg write |
| `agents/summary/packages/summary_core/mcp/client.py` | the JSON-RPC envelope, retries, obs events |
| `agents/summary/src/service_app.py` | `/health`, `/mcp/tools`, startup events |
| `agents/summary/infra/config.sh` | expected deployed values |
| `docs/summary_deploy_spec.md` | what was deployed |

---

## §3 · Running it

```bash
cd agents/summary
bash infra/04_verify.sh                 # full; makes ONE real HubSpot upsert
RUN_E2E=0 bash infra/04_verify.sh       # control plane + startup + read-only checks
BLOG_URL=… PUBLISHED_AT=… bash infra/04_verify.sh
```

⚠ **Layer C cannot run from a laptop.** `lqabr-dev-summary` and `lqabr-dev-mcp` are both
`ingress=internal`; a curl returns **404 even with a valid ID token** — Cloud Run hides the
service rather than admitting it exists. That is the control working, not a fault. The
checks are shipped into a Cloud Run job on `lqabr-vpc`.

Re-running with the same `PUBLISHED_AT` **updates** that ticket — the upsert is keyed on it
— so full mode is safe to repeat.

---

## §4 · Layers

| Layer | Question | Where | Catches |
| --- | --- | --- | --- |
| **A** | what Cloud Run says the service *is* | host, `gcloud` | wrong SA/ingress, off the VPC, `DRY_RUN=1`, stale env |
| **B** | what the container said as it booted | host, Cloud Logging | `mcp_startup_check_failed` under `warn` |
| **C** | what it actually *does*, hop by hop | in-VPC job | a write that succeeds and stores nothing |

---

## §5 · The flow, and the input set at every hop

Nine hops. For each: the exact input set, where it comes from, what transforms it, the obs
event that proves it happened, and the failure signature.

### §5.1 · Hop 1 — caller → HTTP

`POST /summary/run`, body is `SummaryRequest` (`src/schema.py:34`).

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `source` | object \| string | **yes** | a bare string is read as a URL if it looks like one, else as text |
| `source.kind` | `url` \| `api` \| `json` \| `text` | — | |
| `hubspot` | object \| omitted | no | omit entirely to summarise only |
| `hubspot.blog_published_at` | string | **yes in `blog_summary` style** | ⚠ the upsert KEY (`schema.py:28`) |
| `hubspot.subject` | string | no | defaults to the model's title |
| `hubspot.industry` | string | no | defaults to the model's industry |
| `hubspot.object_id` | string | **not used** in `blog_summary` style | required only in `patch` style |
| `hubspot.object_type` | string | no | defaults to `LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE` = `ticket` |
| `hubspot.properties` | object | no | extra properties, `patch` style only |
| `options` | object | no | |

**Verified input set (C3):**

```json
{"source": {"kind": "url", "url": "https://aidefinitive.com/hipaa-rag-claude"},
 "hubspot": {"object_type": "ticket", "blog_published_at": "2026-08-28T20:12:00.000Z"}}
```

### §5.2 · Hop 2 — normalise

`request.to_spec()` → `SourceSpec{kind, reference}` (`pipeline.py:52`).
**Event:** `run_start{source_kind, source_ref, object_id, model, dry_run}` (`pipeline.py:56`).
⚠ `dry_run` is on this line — it is the earliest place the run tells you writes are live.

### §5.3 · Hop 3 — fetch

**In:** `SourceSpec`. **Out:** `Document{title, char_count, truncated}`.
**Step:** `fetch`, closing `title/chars/truncated`.
**Failure:** `SourceError` → HTTP **200** with `status:"failed"`, `error` set,
`run_failed{step:"fetch"}`. A refused source is a reported outcome, not a 5xx.

### §5.4 · Hop 4 — model

**In:** the document plus the instruction built from `SUMMARY_FIELDS` (`schema.py:93`) — the
prompt reads that constant so the two cannot drift.

**Out:** raw text → `extract_json` (recovers ```json fences and prose-prefixed JSON, but
never guesses) → `parse_summary` → `SummaryResult` (`types.py:135`):

| Field | Type | Required |
| --- | --- | --- |
| `summary` | string | **yes** — `REQUIRED_FIELDS = ("summary",)` (`schema.py:103`) |
| `title` `topic` `industry` | string | no |
| `key_points` `concepts` `technologies` `takeaways` | list[string] | no |
| `source_kind` `source_ref` `model` `raw` | string | set by the agent; `raw` keeps the model's own words for audit |

**Failures, distinguished by name:** `SummaryValidationError` → `run_failed{step:"summarize"}`
with the reason; a provider fault → `the model call failed (<Type>): …`, so an operator can
tell a bad answer from a dead provider.

### §5.5 · Hop 5 — the write gate

Three ways to be legitimately skipped (`pipeline.py:100-115`), each with its own message:

| Condition | `hubspot.status` | `error` |
| --- | --- | --- |
| `hubspot` omitted | `skipped` | `no hubspot target was supplied, so nothing was written` |
| `blog_summary` style, blank `blog_published_at` | `skipped` | `no hubspot.blog_published_at was supplied, so nothing was written` |
| `patch` style, blank `object_id` | `skipped` | `no hubspot.object_id was supplied, so nothing was written` |

⚠ **`skipped` is not a failure and `status` still reads `completed`.** A verify that only
checks `status` passes here having written nothing. C3 therefore asserts
`hubspot.status == "written"`, not `status == "completed"`.

### §5.6 · Hop 6 — the MCP argument set  ← **the input set that matters**

`_write_blog_summary` (`mcp/hubspot.py:152`). Exactly four arguments, **all required**:

| Argument | Built from | Transform |
| --- | --- | --- |
| `subject` | `hubspot.subject` or `summary.title` | `.strip()` |
| `blog_summary` | `summary.as_hubspot_text()` | title, blank line, summary, `Key points:` bullets, `Technologies: …`; capped at **60 000** chars against HubSpot's 65 536 limit (`types.py:154`) |
| `blog_published_at` | `hubspot.blog_published_at` | `_iso_published_at()` — **the upsert key** |
| `blog_industry` | `hubspot.industry` or `summary.industry` | `_normalise_industry()` |

**`_iso_published_at` (`mcp/hubspot.py:25`) — the sharpest trap in this agent:**

```python
if not v or "T" in v: return v          # passed through UNVALIDATED
if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v): return v + "T00:00:00.000Z"
return v
```

| Input | Output | Outcome |
| --- | --- | --- |
| `2026-08-28` | `2026-08-28T00:00:00.000Z` | expanded, works |
| `2026-08-28T20:12:00.000Z` | unchanged | correct |
| `08:28:2026T20:12:00` | **unchanged** | ⚠ malformed reaches the datetime-keyed upsert and **silently no-ops** |

**`_normalise_industry` (`mcp/hubspot.py:39`)** upper-cases and collapses `[\s\-/]+` to `_`,
returns the portal's exact spelling on a match, and otherwise returns the normalised value
**for the MCP to reject** — deliberately not fuzzy-matched, because a near-miss selects zero
rows and raises nothing. Options default to `FINANCIAL_SERVICES, LEGAL_SERVICES, HEALTHCARE`
(`LQABR_SUMMARY_HUBSPOT_INDUSTRY_OPTIONS`).

**If any of the four is blank:** `hubspot_write_skipped{tool, missing:[…]}`, status
`skipped`, nothing sent — never a blank write.

**If `dry_run`:** `hubspot_write_dry_run{tool, args}` where `args.blog_summary` is replaced
by `<N chars>`. **This event is the complete argument set** and is the best artefact to read
when you want the input set without writing.

### §5.7 · Hop 7 — the JSON-RPC call

`client.call_tool` (`mcp/client.py:256`). Emits
`mcp_tool_call{tool, url, arguments, timeout_s}` **before** the call — summarised normally,
full arguments under debug. Wire payload:

```json
{"jsonrpc":"2.0","id":N,"method":"tools/call",
 "params":{"name":"upsert_blog_summary","arguments":{"subject":"…","blog_summary":"…",
           "blog_published_at":"2026-08-28T20:12:00.000Z","blog_industry":"HEALTHCARE"}}}
```

Retries: `max_retries` attempts; **404 → `mcp_session_lost` → re-`initialize` and retry**
(the MCP scales to zero and forgets the session); `mcp_retryable_statuses`
(429,500,502,503,504) with exponential backoff.

### §5.8 · Hop 8 — interpreting the tool's answer

**Event:** `hubspot_write_raw_result{tool, result_type, result_keys, result_preview,
sent_published_at}` (`mcp/hubspot.py:182`). ⚠ `sent_published_at` is the **authoritative
record of the key actually sent** — the one field to read when C4 fails.

A rejected write arrives as a **body, not an exception**. All of these are failures:

| Shape | Meaning |
| --- | --- |
| `{"error": …}` | classic rejection |
| `{"failure_kind": …}` | |
| `{"status": "halted"\|"failed"\|"error"}` | systemic — e.g. the MCP could not read its HubSpot token |

### §5.9 · Hop 9 — the response, and the readback

`SummaryResponse`: `run_id`, `status` (**follows the write** — a failed write yields
`failed` and still returns the summary so the work is not lost), `source`, `summary`,
`hubspot{status, object_id, object_type, properties, tool, error}`, `model`, `error`.

**The readback is a separate call and the only real proof:** `get_blog_summary`
`{blog_published_at: <same key>}` on the MCP. `found: false` means the upsert did not land,
regardless of what hop 9 reported.

---

## §6 · Assertions

### §6.1 · Layer A — control plane

One `gcloud run services describe --format=json`, parsed in a single pass: a mistyped
per-field `--format` returns empty and reads as a pass.

| id | Assertion | Fails when |
| --- | --- | --- |
| A1 | service exists in `us-central1`/`ldqfingsrv-dev` | never deployed |
| A2 | latest revision `Ready=True` | container never became healthy |
| A3 | one revision serves 100% | a traffic split left an old revision live |
| A4 | `ingress=internal` | agent is internet-reachable |
| A5 | SA starts `lqabr-agent-dev@` | fell back to the default compute SA |
| A6 | `network-interfaces` set **and** `vpc-access-egress=all-traffic` | ⚠ off the VPC — cannot reach the MCP |
| A7 | a volume mounted at `/app/logs` | file logging errors on a read-only path |
| A8 | env exact: `MCP_TOOL_READ=get_blog_summary`, `MCP_TOOL_WRITE=upsert_blog_summary`, `MCP_WRITE_STYLE=blog_summary`, `SECRETS_SOURCE=secret_manager`, `HUBSPOT_OBJECT_TYPE=ticket`, `DRY_RUN=0` | any drift; each mismatch named |

### §6.2 · Layer B — startup

Filtered on `resource.labels.revision_name` of the **serving** revision, ordered ascending.

⚠ **Read `jsonPayload.event`, never `textPayload`.** Summary emits *structured* obs events,
so `--format='value(textPayload)'` returns empty for every one of them and all four
assertions below report a false FAIL against a perfectly healthy service. This cost the
first real run of this spec four false negatives (2026-08-29). `textPayload` on this service
carries only platform lines and library warnings.

⚠ **Never `--freshness`** — stale entries inside the window have twice been mistaken for
current results.

| id | Event | Proves |
| --- | --- | --- |
| `B-service_start` | `service_start` (`service_app.py:114`) | the app booted |
| `B-mcp_initialized` | `mcp_initialized` (`client.py:213`) | session handshake |
| `B-mcp_tools_discovered` | `mcp_tools_discovered` (`client.py:230`) | `tools/list` returned |
| `B-mcp_startup_check_ok` | `mcp_startup_check_ok` (`service_app.py:98`) | **configured names exist on the live MCP** |
| `B-startup_check` | `mcp_startup_check_failed` **absent** | under `warn` its presence does not stop the service — it fails later, at write time |

### §6.3 · Layer C — data plane

| id | Check | Pass criterion | Hop |
| --- | --- | --- | --- |
| C1 | `GET /health` | `status:"UP"` and `mcp.reachable:true` | — |
| C1b | `logging.degraded` | empty | — |
| C1c | `dry_run` | `false` | §5.2 |
| C2 | `GET /mcp/tools` | the **called** tools (`read`, `write`) are present | §5.6 |
| C2b | other configured-but-absent tools | `WARN`, not `FAIL` — see below | §6.4 |
| C3 | `POST /summary/run` | `status:"completed"` **and** `hubspot.status:"written"` | §5.1–5.9 |
| — | input set printed | tool, properties, subject, key, industry, `blog_summary` length + first 300 chars | §5.6 |
| C4 | `get_blog_summary` on the same key | ticket returned; `found:false` is a **failure** | §5.9 |

### §6.4 · Configured ≠ called — why C2 warns instead of failing

`/mcp/tools` reports `configured = {read, write, list_leads}` and lists every one not on the
MCP under `missing`. But `list_leads` (`LQABR_SUMMARY_MCP_TOOL_LIST_LEADS`, default
`list_trigger_leads`) is **not on the deployed MCP and has no caller in summary** —
`HubSpotMCP.list_trigger_leads()` exists at `mcp/hubspot.py:239` and nothing invokes it.

`ensure_ready()` asserts only read and write, for the stated reason that *"asserting it
would refuse to start over a tool nothing calls"* — which is why `mcp_startup_check_ok` is
emitted while `/mcp/tools` still reports `missing: ['list_trigger_leads']`. Those two are
not in conflict; they answer different questions.

Asserting `missing == []` therefore makes a healthy service report FAIL. C2 fails only on
`read`/`write`; anything else absent is `WARN C2b`, because it is a real latent defect — the
day a caller appears, it breaks — but not a reason to fail a green deployment today.

---

## §7 · Failure decode — id → cause → fix

| id | Cause | Fix |
| --- | --- | --- |
| A1 | not deployed / wrong project or region | `bash infra/03_deploy_run.sh` |
| A2 | died at startup | ⚠ a bare `failed to start and listen on PORT=8080` was once an `IndexError` at **import**; read the revision's stderr, not Cloud Run's message |
| A4–A7 | deployed without `03_deploy_run.sh` | redeploy with the script; never hand-roll the flags |
| A8 `SECRETS_SOURCE` | set to `env` | must be `secret_manager` — summary ships `google-cloud-secret-manager`, research does not |
| A8 `DRY_RUN` | `1` | writes suppressed |
| A8 tool names | `summary_core` defaults leaked | `get_lead_profile_details`/`post_patch_crm` do not exist on this MCP |
| `B-mcp_initialized` missing | cannot reach the MCP | almost always A6 |
| `B-startup_check` | wrong tool names | same as A8; the service still starts — that is the trap |
| C1 `mcp.reachable:false` | MCP down or wrong `MCP_BASE_URL` | verify the MCP first: `bash infra/gcp/mcp/02_probe.sh` |
| C2 `missing:[…]` | configured names absent | the MCP exposes exactly four tools |
| C3 `skipped` | §5.5 gate, or a blank one of the four args | read the `error` text — it names which |
| C3 `error` + `blog_industry … is not one of` | model prose vs portal enum | pass `hubspot.industry` explicitly |
| C3 `error` + `status:"halted"` | MCP-side systemic fault, e.g. its token | `infra/gcp/mcp/01_deploy.sh` |
| **C4 `found:false` while C3 passed** | ⚠ **malformed `blog_published_at`** (§5.6) | read `sent_published_at` in `hubspot_write_raw_result`; it must be full ISO 8601 |
| job exits with no output | killed early | task timeout is 900s; cold start + model call is 1-3 min |

---

## §8 · Negative constraints

| # | Forbidden | Consequence |
| --- | --- | --- |
| N1 | `--freshness` instead of a revision/timestamp filter | stale entries read as current |
| N2 | Concluding success from `status:"completed"` alone | `skipped` also yields `completed` (§5.5) |
| N3 | Concluding success from C3 alone | only C4 proves HubSpot has it |
| N4 | Leaving `--ingress all` after using it to test | a private agent left internet-reachable |
| N5 | Deleting the job before it finishes | one deleted 27s in captured nothing |
| N6 | Treating a laptop 404 as a fault | that is `ingress=internal` working |
| N7 | Trusting a script over §5 | scripts drift; §5 is cited to the code |

---

## §9 · Out of scope

The gateway path (`/summary/a2a`, `R-blog-summary`); summary quality — C3 asserts a write
happened, never that the text is good; the MCP itself (`infra/gcp/mcp/02_probe.sh`); model
cost — each full run makes one real Anthropic call.

---

## §10 · Open items

- **`_iso_published_at` does not validate**, so C4 is the *only* defence against a malformed
  key. It should reject what it cannot parse rather than forward it.
- **C4 cannot distinguish created from updated.** It proves a ticket exists for the key, not
  that this run wrote it. Comparing the returned `blog_summary` against C3's response would
  close that.
- **`hubspot.properties` and `object_id` are silently ignored** in `blog_summary` style; a
  caller supplying them gets no warning.
- **`list_trigger_leads` is configured but does not exist and is never called** (§6.4).
  Either add it to the MCP or drop `LQABR_SUMMARY_MCP_TOOL_LIST_LEADS` from the deployment,
  so `/mcp/tools` can return a clean `missing: []`.
