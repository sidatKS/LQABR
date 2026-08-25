# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Before running anything, read §11 (Session Setup & Environment)** — which surface
you're on, how the repo folder is connected, and the preflight that must pass first.

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
  orchestrator}/` — six core ADK agents, each with `src/` + `tests/`,
  `requirements.txt`, `.env.example`. Email/text_voice/scheduling also ship
  a FastAPI `webhook_app.py` receiving Mailgun/Twilio/Zoom events.
- `agents/gateway/` — the HubSpot-event ingress/routing service (not an
  ADK agent): a single ingress that decides which agent owns a trigger and
  hands off only a trigger id + the contact's record id.
- `infra/gcp/` — idempotent scripts `00`–`06`: APIs, runtime SA/IAM,
  Secret Manager (11 secrets), Pub/Sub, HubSpot property bootstrap,
  Cloud Run build+deploy, Cloud Scheduler.
- `data/seeds/b2b/` — CSV seeds, the manual ingestion source.
- 64 pytest tests, all mockable offline
  (`python3 -m pytest -c tests/pytest.ini -q` from root).

HubSpot is the system of record for lead data — see
`docs/adr/002-hubspot-central-crm.md`.

## 3. Tech Stack

- **Agents:** Google ADK on Cloud Run; each agent exposes `root_agent` via
  an `agent.py` shim so `adk web/run/api_server agents/<name>/src` works.
  **Exception — `text_voice` (2026-08-18):** ADK was removed from it entirely.
  It has no `agent.py`, no `adk_agent.py`, no `root_agent`, and no
  `requirements-dev.txt`. It is a plain FastAPI service (`uvicorn tools:app`);
  the orchestrator's A2A JSON-RPC `message/send` envelope is parsed by
  `tools.py`'s `/voice_agent/lead` directly. Local end-to-end testing uses
  `agents/text_voice/push_test.sh`, not the `adk web` `test <id>` console.
  `infra/gcp/config.sh` has NOT been updated to match — see §10.
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

`docs/READ_PRJSTRC_ME.md` has the full rationale; the short version:

- `agents/` — the six core ADK agents plus the `gateway` routing service
  (see §1). Unit tests co-located.
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
# Config lives in tests/pytest.ini, so -c is required from the root.
python3 -m pytest -c tests/pytest.ini -q

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
- **Branching strategy:** `leadq-dev` is the **default branch on the remote** (changed from `main` on 2026-08-15). It is the shared integration branch: new clones check it out, new PRs target it by default, and it is what `origin/HEAD` resolves to. `main` remains the always-deployable release trunk and receives only reviewed Epic merges — never direct commits. Each Epic has a long-lived integration branch `epic/LQABR-<N>-<slug>` cut from `leadq-dev`. Story/Task work branches off its Epic branch as `LQABR-<ticket>-<short-slug>`; Sub-tasks branch off their Story or commit on it. Flow per Epic: branch from latest `leadq-dev` → Stories PR into the Epic branch → when all children are Done and the Epic AC is met, PR the Epic branch into `leadq-dev`; releases promote `leadq-dev` → `main` and tag. Merge `leadq-dev` into the active Epic branch whenever it advances.
- **After the default-branch change**, each clone must repoint its own pointer once — `git remote set-head origin -a`. This does not propagate.
- **Known gap:** neither `leadq-dev` nor `main` currently has branch protection, so nothing mechanically enforces the PR/review flow above — anyone with push access can commit straight to the default branch. Treat the rules here as convention until a ruleset is added under Settings → Branches.
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
- **Do not run `infra/gcp/05_deploy_agents.sh` for `text_voice` as it stands.**
  `config.sh` still lists text_voice in both `ADK_AGENTS` and `WEBHOOK_AGENTS`,
  so it would deploy `lqabr-text-voice-agent` via `adk api_server` (no
  `root_agent` exists any more) and `lqabr-text-voice-webhook` via
  `uvicorn webhook_app:app` (retired in Rev 5). The shared
  `infra/gcp/cloud-run/entrypoint.sh` `webhook` branch also hardcodes
  `webhook_app:app` and ignores `${APP_MODULE}`. The Rev 5 service that
  actually serves `/voice_agent/lead` and `/voice_agent/vapi_report`
  (`tools:app`) is not deployed by that script at all. Fix the infra before
  the next deploy — see `claude/deleted-files-audit-2026-08-18.md`.

## 11. Session Setup & Environment

**This file cannot attach a folder or open a connection.** Mounting the repo is a
client-side action taken in the Claude app before a session starts. What follows is
what must already be true, and how to verify it in the first minute.

Canonical dev machine: **`desktop-vmn01k1`**. Repo root: `/mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR` (set this
path once and keep it stable — every instruction below assumes it).

### 11.1 Surfaces — what each one can and cannot do

| Surface | Local file access | Use it for |
|---|---|---|
| **Claude Code** (terminal on `desktop-vmn01k1`) | Yes, direct | code changes, tests, git, `infra/gcp/` scripts |
| **Cowork** (desktop project bound to `/mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR`) | Yes, for connected folders, while Claude Desktop is open on `desktop-vmn01k1` | multi-file work, docs, generated deliverables |
| **Web Claude** (cloud sandbox) | **No** — only read-only project-knowledge copies under `/mnt/project/` | design review, planning, Jira edits, architecture reasoning |

**Session boundary rule:** file I/O belongs to Claude Code and Cowork. The web
session has no path to this repo. If a web session is asked to read, edit, or
create a repo file, it must say so plainly rather than simulate the result or
work from a stale pasted copy.

### 11.2 One-time: bind the repo to a desktop project

1. Claude Desktop → sidebar → **New Project** → *use an existing folder on your
   computer* → select `/mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR`.
2. Approve the OS folder-permission prompt.
3. Project → **Folder instructions**: "Read `CLAUDE.md` at the repo root before any
   code change and follow it — especially §7 (never push or open a PR without
   explicit confirmation) and §6 (stage discipline, probability rules)."
4. Start **every** Cowork session from inside that project. The folder mounts
   automatically; there is no per-session re-attach.

Caveat: Cowork sessions run in the cloud and reach local files **only while Claude
Desktop is open on `desktop-vmn01k1`**. A closed app leaves the session running with
no repo underneath it.

### 11.3 Preflight — run before touching anything

```bash
cd /mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR
ls CLAUDE.md packages/lqabr_core agents infra   # is the right root actually mounted?
git status -sb                                  # branch + clean tree before any edit
git branch --show-current                       # must be epic/… or LQABR-<ticket>-… , never main
python3 -m pytest -c tests/pytest.ini -q         # 64 tests green as the baseline
```

**If the `ls` fails, stop.** The folder is not connected. Do not create
`agents/`, `packages/`, or `infra/` from scratch in a scratch directory — that
produces a parallel tree that silently diverges from the real repo. Fix the
connection first (§11.2), then re-run the preflight.

If the baseline test run is already red, report that before making changes so a
pre-existing failure is never attributed to this session's work.

### 11.4 Before making changes

- Move the Jira ticket to **In Progress** (§8) and branch per §7 — never commit on `main`.
- Per-agent config comes from `agents/<name>/.env`, copied from `.env.example` and
  git-ignored. Credentials come from Secret Manager or that `.env` — never from the
  session, never pasted into chat (§3, §6).
- **Local runs hit real GCP.** ADK orchestration runs locally in dev, but Vertex AI,
  BigQuery, Pub/Sub, and Secret Manager calls go to the live authenticated project and
  incur real cost. Prefer mocked tests and `--dry-run` on ingestion before any live run.
- End of session: leave the tree committed locally or explicitly dirty-and-noted —
  never mid-refactor with no record of intent.
