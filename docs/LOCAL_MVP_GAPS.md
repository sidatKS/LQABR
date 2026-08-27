# LQABR Local MVP — Gaps, Bugs & Follow-ups

**As of:** 2026-08-22
**Scope:** the local end-to-end chain (Summary → Gateway → Research → Email → Voice),
all agents on one laptop, HubSpot the only public trigger via ngrok.

---

## Legend

| Owner | Meaning |
| --- | --- |
| **MCP owner** | the `tne736/lqabr-mcp-server` image — outside this repo |
| **Dev** | changeable in this repo |
| **Product** | needs a decision or a credential from the team |

---

## A. Blockers — external dependency

| ID | Gap | Owner | Action | Priority |
| --- | --- | --- | --- | --- |
| **B1** | `get_lead_profile` does not return the company **NAME** | MCP owner | Add `"company": "<name>"` to the profile payload. The tool ALREADY resolves the Company — it returns `company_hs_id`, `company_resolved: true`, and reads `industry` off that record — so the name is one field from a walk it already performs. | **P1** |

**Evidence (contact 533963448020, verified 2026-08-22):**

```
HubSpot contact : company=null, industry=null, associatedcompanyid=339372046062
MCP returns     : employee_id, company_id="C0017", decision_maker_flag, job_title,
                  email, phone, firstname, lastname, industry="HEALTHCARE",
                  annual_revenue_m, frequency_of_purchase, lead_context, statuses
                  + contact_hs_id, company_hs_id="339372046062", company_resolved=true
                  -> NO company name anywhere
```

**Impact:** the Research Agent cannot do company-specific research. Falling back to
`company_id` made it search `"C0017"`, which matched the **MITRE ATT&CK campaign**
of that name and an **Austin city procurement dataset** — grounding the note in the
wrong company entirely. The agent now omits the name and researches at industry
level, stating plainly that the company could not be identified.

**No code change needed on our side once shipped** — the Research Agent's
`_first(flat, "company", "company_name")` already reads it.

---

## B. Data gaps

| ID | Gap | Owner | Action | Priority |
| --- | --- | --- | --- | --- |
| **D1** | Contact `company` and `industry` are null; only `associatedcompanyid` is set | Data (resolves via D2) | Backfill after D2 ships, or accept Company-record resolution as the source | P2 |
| **D2** | `lead_profile` does not enforce company **name** as mandatory | Dev | Make company name required on upsert; flag `bad-data:` when absent so a nameless lead is reported, never silently written | **P1** |
| **D3** | `blog_industry` is a HubSpot **enumeration** (`FINANCIAL_SERVICES`, `HEALTHCARE`, `LEGAL_SERVICES`) but the Summary Agent emits free text (e.g. "Software development") | Dev + Product decision | EITHER map the model's industry to the enum inside the Summary Agent, OR widen the HubSpot enum. **Decision needed.** Today the value is hand-passed per request. | **P1** |

**D3 evidence:** a live write with `blog_industry: "Healthcare"` returned
`HTTP 400 INVALID_OPTION — Healthcare was not one of the allowed options`.
Only the exact uppercase enum token is accepted.

---

## C. Bugs found and ALREADY FIXED

| ID | Bug | Where | Note |
| --- | --- | --- | --- |
| F1 | `status:"halted"` read as success — write reported `written` while nothing reached HubSpot | summary + research `mcp/hubspot.py` | The MCP reports systemic failure as a *body*, not by raising. Named regression test added. |
| F2 | `str(result.get("error"))` yields the literal string `"None"` (truthy), so the real `reasons` were never surfaced | summary + research | Every halted write would have reported its reason as `"None"`. Caught by the F1 test. |
| F3 | `blog_published_at` as a bare date silently no-op'd the upsert | summary | The MCP keys the blog store on a full ISO timestamp. Normalizer added. |
| F4 | `mcp/hubspot` had no HTTP surface; no ticket-write support | `mcp/` | Superseded by adopting the official image. |
| F5 | Anthropic SDK 1.0.0 rejects `temperature` on `messages.create()` | research search | Payload now filtered against the installed SDK's signature; dropped names are logged. |
| F6 | Citation text blocks joined with blank lines — shredded prose mid-sentence | research search | Joined with no separator. |
| F7 | `searches` reported 0 on a run with 25 sources | research search | Result blocks now counted. |
| F8 | `company_id` used as the company NAME | research `hubspot.py` | See B1. |
| F9 | `testpaths = .` under `-c` collected the entire repo | research `pytest.ini` | Resolves against the invocation dir, not the ini's dir. |

---

## D. Known-broken — not yet fixed

| ID | Gap | Owner | Action | Priority |
| --- | --- | --- | --- | --- |
| **K1** | `agents/summary/tests/pytest.ini` has the same `testpaths = .` bug as F9 | Dev | One-line fix; the summary suite cannot currently run | **P1** |
| **K2** | Root `tests/pytest.ini` — duplicate test basenames collide under prepend import mode | Dev | Add `--import-mode=importlib`; the repo-wide suite cannot currently run | **P1** |
| **K3** | `/voice_agent/vapi_report`, `/voice_agent/lead`, `/hubspot/campaign` are unauthenticated | Dev (deferred by decision) | Only reachable on loopback today. MUST be fixed before `:8083`/`:8084` are ever exposed, or before moving off polling back to provider webhooks. | P3 (P1 if exposed) |
| **K4** | Gateway HubSpot signature verification is OFF (`HUBSPOT_APP_SECRET` unset) | Product → Dev | Fetch the private app's client secret, then flip `ingress.signature.enabled: true` | **P1 before prod** |
| **K5** | `docs/ngrok_setup.md` records the domain but not the owning ngrok ACCOUNT | Product → Dev | Three accounts were tried before the right token was found. Record the owning account beside the domain. | P2 |
| **K6** | `/engagement/sync` referenced in `agents/email/.env.example` but does not exist in the code | Dev | Remove the stale reference | P3 |
| **K7** | Google ADC expires hourly, halting every MCP write | Accepted | Run `bash mcp/reauth.sh` (re-login + container recreate; the read-only `adc.json` bind-mount pins the old file, so a restart is required) | Accepted |

---

## E. Remaining build tasks

| Task | State | Blocked by |
| --- | --- | --- |
| T10 — Research Agent | Built; 47 tests pass; gateway routes to it; **live run not yet fully verified** | B1 for company-specific output |
| T1 — email / text_voice `.env` | Pending | — |
| T7 — HubSpot webhook target + subscriptions | Pending | — |
| T11 — lead_profile `:8085` + audience seed | Pending | D2 |
| T12 — Email Agent `:8083` + Mailgun send | Pending | T1 |
| T13 — Poll Mailgun Events → `email_status=OPENED` | Pending (new code) | T12 |
| T14 — text_voice `:8084` + Vapi call + poll | Pending (new code) | T1 |
| T15 — Full E2E trace | Pending | T12–T14 |
| T16 — `START_LOCAL.md` + one-command start | Pending | K5 |

---

## F. What is proven working

| Component | Address | Evidence |
| --- | --- | --- |
| HubSpot MCP (official image) | container `:8091` | 4 tools bound; live reads and writes confirmed |
| Summary Agent | `:8082` | Ticket `329444635358` created live via `upsert_blog_summary`, verified in the HubSpot UI |
| ngrok | `armed-equal-share.ngrok-free.dev` → `:8080` | Static domain; `/healthz` 200 through the tunnel |
| Gateway | `:8080` | All three agents report `ready`; audit sink writing `logs/gateway/gateway.jsonl` |
| Research Agent | `:8086` | 47 tests pass; MCP reachable; full pipeline ran (read → search → compose → dry-run write) |
| Gateway `blog_published_at` | — | On the wire; payload guard still refuses lead-profile data |

---

## G. Ownership split

**Dev can start immediately:** D2, D3, K1, K2, K5, K6

**Needs the team:**
- **B1** — raise with the MCP image owner (P1, in flight while T12–T14 proceed)
- **K4** — the HubSpot private-app client secret
- **D3** — decide: map the industry in the agent, or widen the HubSpot enum
