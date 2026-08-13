# ADR 003 — Agentic tool-call standard, MCP boundary, and mandatory audit/process/system logging

**Status:** Proposed (2026-07) — for team review before Accepted

## Context

Engineering leadership set three requirements for every LQABR agent:

1. Strict agentic design: no bespoke Python/Node scripts calling external
   services directly; every external call must be a tool call (or MCP).
2. Clear, written design decisions for how this is implemented.
3. Every agent must emit audit, process, and system logs — a PR that adds
   or changes a tool without logging is rejected in review.

Auditing the current codebase against this:

- All six agents (`ingestion`, `lead_profile`, `email`, `text_voice`,
  `scheduling`, `orchestrator`) already follow the ADK pattern: external
  services (HubSpot, Mailgun, Twilio, Zoom, ZoomInfo) are wrapped in typed
  clients under `lqabr_core` or an agent's own `src/`, and exposed to the
  model only via functions passed to `Agent(tools=[...])`. This already
  satisfies "tool calls, not scripts" for agent-internal work.
- One clear violation exists: `data/seeds/b2b/output/push_leads.py`, a
  standalone script that re-implements HubSpot contact/company upsert
  logic outside `lqabr_core.crm.HubSpotClient`, with its own (already
  drifted) property mapping, `print()` output instead of logging, and no
  test coverage.
- No agent currently emits structured, correlated logs. The single
  exception is one `logging.getLogger` call in the Mailgun webhook. There
  is no audit trail of stage/probability changes, sends, or dispatch
  decisions beyond what HubSpot itself records.
- No MCP server exists anywhere in the repo today; all agent-internal
  tool exposure goes through ADK's own `tools=[...]` mechanism, and
  agent-to-agent calls go through Google A2A (`agents/orchestrator/src/a2a_dispatch.py`).

## Decision

1. **Tool-call standard.** Every external API integration is a typed,
   mockable client (already the pattern: `HubSpotClient`, `MailgunClient`,
   `TwilioClient`, `ZoomClient`, `ZoomInfoClient`), and the only way an
   agent's model can invoke it is through a plain function passed to
   `Agent(tools=[...])`. No agent, script, or notebook may call an
   external API directly. `push_leads.py` is retired; anything it still
   needs to do goes through `lqabr_core.crm` or a proper ingestion tool
   function, so there is exactly one HubSpot code path and schema.
2. **MCP boundary.** ADK's `tools=[...]` mechanism *is* the tool-call form
   for agent-internal use and already meets the mandate — it is not
   replaced with MCP. MCP is adopted only at the point a caller outside
   the ADK/A2A world (e.g. an internal chat assistant or support tool)
   needs the same HubSpot/Mailgun/Twilio/Zoom/ZoomInfo operations. If/when
   that need is real, one internal MCP server wraps the existing
   `lqabr_core` clients rather than re-implementing them, so ADK agents
   and any external caller share one code path and one audit trail.
   Agent-to-agent orchestration stays on A2A. No MCP server is being built
   as part of this ADR, since no such external caller exists yet.
3. **Mandatory logging.** Every agent uses a new shared module,
   `lqabr_core.logging`, providing three correlated log streams per
   process:
   - **system** — exceptions, retries, startup/config problems; plain
     Python `logging`, JSON-formatted, to stderr → Cloud Logging.
   - **process** — one line per tool call (tool name, redacted arguments,
     duration, outcome), applied via the `@log_tool_call` decorator on
     every function passed to `Agent(tools=[...])`.
   - **audit** — a durable record of state-changing business actions only
     (lead stage/probability changed, an email/SMS/call actually sent, a
     dispatch decision made), emitted via `@log_tool_call(..., audit=True)`
     on the specific tools that cause those actions.

   All three streams carry `trace_id` and `hubspot_contact_id` when
   available (set via `correlation_scope(...)`), so one lead's path across
   services can be reconstructed from log search alone.
4. **PR gate.** `CLAUDE.md` §9 (Definition of Done) and §10 (Things to
   Avoid) are updated: a new or modified tool function without
   `@log_tool_call` fails review. This operationalizes "no logging → the
   code is rejected."

## Consequences

- One new file (`packages/lqabr_core/lqabr_core/logging.py`) and a
  decorator applied to existing tool functions across six agents — no new
  third-party dependency, no change to `requirements.txt`.
- `push_leads.py`'s removal means any future one-off bulk load must go
  through the same `lqabr_core.crm` path, so the HubSpot property schema
  can't silently drift again.
- Audit-log lines become the reviewable trail for "did we actually email/
  call/schedule this lead and when" independent of HubSpot's own activity
  timeline — useful for debugging and for satisfying the logging mandate
  without depending on a third-party UI.
- Slight overhead per tool call (one wrapping function call); negligible
  next to the network latency of the external API calls themselves.
- MCP is deliberately deferred, not rejected — this ADR names the trigger
  condition (an external, non-ADK caller) so the decision doesn't need to
  be relitigated when that need shows up.
