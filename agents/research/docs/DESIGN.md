# Research Agent — design

## The job

One lead, one published post, one grounded paragraph on the contact.

The Email Agent writes first-touch outreach. Outreach that reads identically
for every company in an industry does not get replies. `lead_context` is the
field that makes it specific — and this agent is what fills it.

## Where it sits

```
Summary Agent ──► blog_summary on a Ticket
                     │  HubSpot trigger 1  (ticket.propertyChange)
                     ▼
                  gateway  R-blog-summary
                     │  audience resolution: ticket ──► N leads
                     ▼
                  RESEARCH AGENT  (one dispatch per lead)
                     │  writes lead_context on the Contact
                     ▼
                  HubSpot trigger 2  (contact.propertyChange)
                     │
                  gateway  R2-lead-context ──► Email Agent
```

The agent is one hop in a chain where **the CRM is the bus**. It is not called
by the Email Agent and does not call it; it writes a property, and HubSpot's
own event does the rest. That is the design's load-bearing idea: agents are
decoupled through the system of record, never through each other.

## The two ids, and why both are needed

A dispatch carries:

| Metadata | Is | Used for |
| --- | --- | --- |
| `objectId` | the **Contact** record id | `get_lead_profile` → industry, company |
| `blog_published_at` | the post's publication timestamp | `get_blog_summary` → the summary |
| `summary_objectId` | the blog **Ticket** id | correlation in the audit trail |

`blog_published_at` was **added to the gateway on 2026-08-22** for this agent.
The reason is a contract mismatch worth remembering: the gateway knows the
ticket *id*, but the central MCP indexes the blog store by publication
*timestamp* (`get_blog_summary(blog_published_at)`). There is no
read-by-ticket-id tool. Without the timestamp the agent would hold a ticket id
it could not resolve — so the gateway now reads both properties in the one
ticket GET it was already making, and puts the timestamp on the wire.

A timestamp is an identifier, not lead-profile data, so it passes the gateway's
payload guard on the same grounds as `summary_objectId`. The guard still refuses
anything that looks like profile data — that is verified by test.

## HubSpot access: the MCP is the only door

The agent holds no HubSpot credential and never calls `api.hubapi.com`.

This is not ceremony. A second path to the CRM would mean a second copy of the
token, writes that skip the MCP's schema validation, and an audit trail with
holes in it. One door means one place where authentication, validation and
logging happen. `tests/test_standalone.py` enforces it by scanning the source
for the hostname.

The cost is a real constraint the design has to live with: the agent can only
read what the MCP exposes. That is what forced the `blog_published_at` change
above rather than "just fetch the ticket".

## Grounding

Web search runs through Anthropic's server-side tool. Two reasons:

- **No new vendor, no new secret.** The agent already holds an Anthropic
  credential for the model. A dedicated search API would add a key to
  provision, rotate and leak.
- **Citations come back attached.** The URLs the model actually used are
  captured and appended to the note, so a claim in an outreach email can be
  traced to a source.

`search/base.py` keeps the contract one method wide, so swapping the provider
is a new module plus a branch in `build_provider` — never a change in the
pipeline.

**The prompt forbids ungrounded claims** (`src/prompts/research.md`). A note
that invents a funding round or a customer name is worse than no note: it
becomes a false statement in an email to a prospect. When the search finds
little, the prompt instructs the model to say so and stay at the industry
level rather than pad.

## Failure taxonomy

The pipeline never raises for bad input. Every failure comes back as
`status="failed"` with **the step named**, because an operator needs to know
which hop broke:

| Step | Failure | Reason prefix |
| --- | --- | --- |
| `input` | no `objectId` / no `blog_published_at` | `bad-data:` |
| `read_lead` | the MCP has no such lead | `crm-error:` |
| `read_blog` | no summary at that timestamp | `crm-error:` |
| `research` | search or model failed | the provider's message |
| write | the MCP rejected it | the MCP's reason |

Two rules this encodes:

- **Flag, never drop.** A lead missing `employee_id` / `company_id` /
  `decision_maker_flag` is reported as `bad-data` with the missing names — it
  is not silently skipped, and nothing is sent.
- **A non-write never reads as written.** The central MCP reports a systemic
  failure as a *body* (`{"status": "halted", "reasons": [...]}`), not by
  raising. Code that only checks for an `error` key reads that as success. It
  did, once, on the Summary Agent — a write reported `written` for an hour
  while nothing reached HubSpot. `write_context` checks `status`,
  `failure_kind` and `error`, and there is a named regression test.

`status` follows the **write**, not the research: a beautifully researched note
that could not be landed is a failed run. The note still comes back in the
response so the work is not lost.

## Config, not code

Model, MCP URL, all three tool names, the target property name, search depth,
note length, log path — every one is a config-map value with an env override.
A rename on the MCP or in the HubSpot schema is a config change.

The tool names are additionally **discovered** at startup (`tools/list`) and
asserted. A mismatch between configuration and the live server becomes "the
service refused to start and named the missing tool" rather than "the write
quietly did nothing in production".

## Why the A2A route answers immediately

`/research/a2a` acknowledges and runs the work in a background task. HubSpot
gives the gateway roughly five seconds to respond; a research pass is a search,
a model call and two CRM hops — far longer. The gateway's `run_id` travels into
the background task, so the logs still tie back to the routing decision.

`/research/run` is synchronous by contrast: a human driving it wants the
outcome, not an acknowledgement.
