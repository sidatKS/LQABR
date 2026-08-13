# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 1. Project Overview

LQABR is an AI lead-qualification and outreach platform. Leads enter from
two sources — manual CSV folder loads or automatic ZoomInfo API pulls
(default batch of 20) — through a single operator-initiated ingestion
trigger. The **Lead Profile Agent** builds a 9-pointer profile per lead and
saves it to **HubSpot, the central lead-profile source and system of
record**. Three outreach agents then raise each lead's probability score by
recording real engagement on the HubSpot contact:

1. **Email Agent (Mailgun)** — contacts leads with listed email IDs; tracks
   delivered / read (opened, last-opened) / clicked internal links.
2. **Text/Voice Agent (Twilio)** — picks up only probability-incremented
   leads (≥ 30). Two flows: (a) no answer → customized voicemail +
   customized SMS; (b) answered → a specific conversational Q&A pattern,
   with the answered-call counter incremented in HubSpot.
3. **Scheduling Agent (Zoom Scheduler)** — leads at ≥ 60 are emailed
   available schedules across EST/CST/PST/IST; a booking pins probability
   at 95 and hands the lead to a human rep.

An **orchestrator agent** routes leads between stages via Google **A2A**.
The full epic map is `docs/EPICS.md` (E0–E10); execution runbooks are
`docs/PHASE0_PLAN.md` … `PHASE5_PLAN.md`.

## 2. Current State (read this first)

Everything is **Python** (Google ADK `google-adk==2.3.0`); there is no
Node/npm toolchain anywhere. What exists and runs today:

- `packages/lqabr_core/` — shared library (installable, `pip install -e`):
  `types` (LeadProfile 9 pointers, stages, events), `probability` (the
  single source of truth for increments/thresholds), `secrets` (Secret
  Manager with env fallback), `crm` (CRMClient interface + HubSpotClient),
  `mailgun`, `timezones`, `profile` (record → profile → upsert). Also `obs`
  (four-stream run/lead observability) and `leadgen` — the Lead Profile
  Agent's **isolated** path: `leadgen.hubspot` (Contact+Company+association
  upsert, `employee_id`/`company_id` dedup, custom `email_id`),
  `leadgen.secrets`, and `leadgen.server` (the MCP front door). It coexists
  with the 9-pointer `types`/`crm` model rather than replacing it.
- `agents/{ingestion, lead_profile, email, text_voice, scheduling,
  orchestrator}/` — six ADK agents, each with `src/` + `tests/`,
  `requirements.txt`, `.env.example`. Email/text_voice/scheduling also ship
  a FastAPI `webhook_app.py` receiving Mailgun/Twilio/Zoom events.
- `infra/gcp/` — idempotent scripts `00`–`06`: APIs, runtime SA/IAM,
  Secret Manager (11 secrets), Pub/Sub, HubSpot property bootstrap,
  Cloud Run build+deploy, Cloud Scheduler.
- `data/seeds/b2b/` — CSV seeds, the manual ingestion source.
- 64 pytest tests, all mockable offline (`python3 -m pytest -q` from root).

The former BigLake/BigQuery data platform was retired — see
`docs/adr/002-hubspot-central-crm.md` (supersedes ADR 001).

## 3. Tech Stack

- **Agents:** Google ADK on Cloud Run; each agent exposes `root_agent` via
  an `agent.py` shim so `adk web/run/api_server agents/<name>/src` works.
  Deterministic tool logic stays typed/mockable and separate from the
  ADK/model wrapper.
- **Orchestration:** Google A2A (JSON-RPC `message/send`) between the
  orchestrator and stage agents; Cloud Scheduler drives dispatch cycles;
  Pub/Sub carries ingestion triggers and engagement-event fan-out.
- **Models (Gemini, config-driven):** per-agent env var
  (`LQABR_<AGENT>_MODEL`, default `gemini-2.0-flash`); swapping model or
  provider is a config change, never a code edit. Auth via AI Studio
  `GOOGLE_API_KEY` or Vertex AI. **The Lead Profile Agent is the exception:**
  it runs **deterministically with no model by default**
  (`LQABR_ORCHESTRATOR=deterministic`); set `LQABR_ORCHESTRATOR=llm` to enable
  its opt-in operator console, whose model comes from `LQABR_AGENT_MODEL`
  (default `anthropic/claude-sonnet-4-6`, routed via LiteLLM).
- **CRM:** HubSpot (private-app token). All lead state lives on the contact
  (`lqabr_*` properties). The adapter is `lqabr_core.crm.HubSpotClient`
  behind the vendor-neutral `CRMClient` interface.
- **Services:** Mailgun (email + event webhooks), Twilio (SMS + voice with
  answering-machine detection), ZoomInfo (contact search), Zoom Scheduler
  (booking links + event webhook).
- **Secrets:** Google Secret Manager only (`lqabr-*` names, see
  `infra/gcp/config.sh`); local dev uses git-ignored `.env` files with the
  same names upper-cased (`lqabr_core.secrets`).
- **Hosting:** Cloud Run (managed sandbox, containers run as `nobody`);
  images built from `infra/gcp/cloud-run/Dockerfile` via Cloud Build.

## 4. Repository Structure

`READ_PRJSTRC_ME.md` has the full rationale; the short version:

- `agents/` — the six ADK agents (see §1). Unit tests co-located.
- `packages/lqabr_core/` — the only shared code path; agents never import
  from each other.
- `infra/gcp/` — numbered provisioning/deploy scripts + `cloud-run/`
  build assets; `infra/terraform/` reserved for future IaC.
- `data/seeds/b2b/` — manual-source CSVs (never modified in place).
- `docs/` — `EPICS.md`, `PHASE0..5_PLAN.md`, `PREREQUISITES.md`, `adr/`
  (immutable once accepted; supersede, never edit), `api/` (OpenAPI),
  `archive/` (historical diagrams).
- `tests/` — reserved for cross-service e2e/integration suites.

## 5. Commands

```bash
# One-time dev setup
pip install -e packages/lqabr_core
pip install -r agents/lead_profile/requirements.txt   # google-adk etc.
pip install pytest fastapi httpx python-multipart

# Tests (all external services mocked; no credentials needed)
python3 -m pytest -q

# Run any agent locally (copy .env.example -> .env first)
adk web agents/<name>/src          # browser dev UI
adk run agents/<name>/src          # terminal
adk api_server agents/<name>/src   # what Cloud Run serves

# Ingestion trigger CLI (dual-source)
python agents/ingestion/src/ingestion_agent.py --source csv [--csv-folder DIR] [--dry-run]
python agents/ingestion/src/ingestion_agent.py --source zoominfo --batch-size 20 [--criteria JSON]

# Webhook receivers locally
uvicorn webhook_app:app --port 8081   # from agents/email/src (8082 text_voice, 8083 scheduling)

# Provision + deploy (from infra/gcp/, after editing config.sh)
source ./config.sh && bash 00_enable_apis.sh   # ... through 06, see infra/gcp/README.md
```

## 6. Conventions

- **HubSpot is the system of record.** Agents hold no canonical lead
  state; every stage/probability change writes back via
  `lqabr_core.crm`, and on conflict CRM wins.
- **Probability rules live only in `lqabr_core/probability.py`** —
  increments (+2 delivered, +5 opened, +10 clicked, +3 SMS, +2 voicemail,
  +15 answered, +15 engaged, 95 booked) and thresholds (30 → text/voice,
  60 → scheduling). Agents import them; never redefine.
- **No cross-agent imports:** shared code flows through
  `packages/lqabr_core` — never `import ../../agents/...`.
- **Tools are typed, mockable, and own their retry/failure behavior**
  (3 tries, exponential backoff on 429/5xx). Never let a tool failure
  silently advance or drop a lead.
- **Bad/messy records are flagged, never dropped:** every non-workable
  lead gets an explicit reason (`bad-data: ...`, `crm-error: ...`) in
  `unresolved`/`upsert_failures`/`failures` lists.
- **Stage discipline:** the Text/Voice Agent only works leads ≥ 30; the
  Scheduling Agent only leads ≥ 60. Promotion happens in CRM writeback
  when thresholds are crossed — never force a stage manually.
- **Webhook authenticity:** every receiver verifies provider signatures
  (Mailgun HMAC, Twilio X-Twilio-Signature, Zoom v0 HMAC). Bypass flags
  (`LQABR_SKIP_TWILIO_SIGNATURE`) are local-dev only.
- **Secrets** come from Secret Manager only — never hard-code or commit
  them; `.env` files are git-ignored templates of the same names.
- **Idempotent infra:** every `infra/gcp/` script is safe to re-run.
- **Provider/model abstractions stay clean:** config-driven models
  (`LQABR_<AGENT>_MODEL`), config-driven agent endpoints
  (`LQABR_<AGENT>_AGENT_URL`) — never hard-code either.
- **SMS copy always includes an opt-out** (Reply STOP) and messages stay
  short, honest, and personalized from real profile data — engagement
  results are recorded by webhooks, never fabricated.

## 7. Git / PR Rules

- Never push to a remote or open a PR without explicit confirmation from the user.
- Stage and commit locally if asked; stop before any `git push` or PR creation and wait.
- **Branching strategy:** `main` is the always-deployable trunk and receives only reviewed Epic merges — never direct commits. Each Epic has a long-lived integration branch `epic/LQABR-<N>-<slug>` cut from `main`. Story/Task work branches off its Epic branch as `LQABR-<ticket>-<short-slug>`; Sub-tasks branch off their Story or commit on it. Flow per Epic: branch from latest `main` → Stories PR into the Epic branch → when all children are Done and the Epic AC is met, PR the Epic branch into `main` and tag a release. Merge `main` into the active Epic branch whenever `main` advances.
- **Commit messages:** `LQABR-<ticket>: <imperative summary>`. One logical change per commit.
- **PR description:** link the Jira ticket, summarize what changed and why, list how it was tested, and call out any outreach-content or webhook-security surface.
- Keep PRs scoped to a single Story/Task or Sub-task where possible.

## 8. Jira Workflow

- **Project key:** LQABR (site: techieg.atlassian.net).
- **Hierarchy:** Epic (E0–E10, see docs/EPICS.md) → Story / Task → Sub-task.
- **Workflow statuses:** Idea → To Do → In Progress → Testing → Done.
- Move the ticket to **In Progress** when you start it; branch and commits
  reference the ticket key; when opening a PR (after confirmation), link it
  and move to **Testing**; **Done** only after merge + Definition of Done.
  Mirror status transitions on the parent when its last child completes.

## 9. Definition of Done

A ticket is complete (ready for review / Testing) when:

- Acceptance criteria on the Jira ticket (and parent Epic) are met.
- Code is tested — unit tests with mocks for external services (HubSpot,
  Mailgun, Twilio, ZoomInfo, Zoom); the full suite passes from the root.
- Lint and build pass; Cloud Run deploy verified for runtime changes.
- No secrets committed; config reads from Secret Manager.
- Webhook signature verification intact; counters/probability writeback
  correct; leads never silently dropped (unresolved reasons present).
- PR raised and linked to the ticket (only after user confirmation).

## 10. Things to Avoid

- Do not push or open PRs without confirmation (restating §7 because it matters).
- Do not let the Text/Voice Agent touch leads below the 30 threshold, or
  the Scheduling Agent below 60 — stage discipline is the product design.
- Do not fabricate engagement: delivered/opened/clicked/answered/booked
  events come only from provider webhooks.
- Do not use LinkedIn Sales Navigator for automated outreach — the
  `linkedin_url` pointer is research-only.
- Do not put canonical lead state in an agent or let it diverge from HubSpot.
- Do not commit secrets or read them from anywhere but Secret Manager /
  git-ignored `.env`.
- Do not hard-code models, agent URLs, or provider specifics — everything
  swappable is env/config-driven.
- Do not redefine probability increments or thresholds outside
  `lqabr_core/probability.py`.
- Do not disable webhook signature checks outside local development.
