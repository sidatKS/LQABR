# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

1. Project overview
LQABR is an AI lead-qualification and outreach platform: an agent-driven engine moves
each lead through a defined state machine (Stages 0–7), researches and contacts leads
across multiple channels, scores them against client-specific qualification criteria,
and hands qualified leads to a human rep. The goal is a flexible, swappable agent/model
system that can be evaluated against labeled datasets before scaling outreach volume,
with the CRM as the system of record and strict consent/compliance enforcement.
2. Tech stack

Backend API: Node.js (API gateway, auth, RBAC, proxy to the agent layer).
Web console: React (operator/admin UI, WCAG AA component library).
Agent engine: Google ADK (Agent Development Kit) runtime on Cloud Run, triggered
via Pub/Sub. Pluggable AgentProvider and ModelProvider abstractions.
Models: Gemini (Flash / Pro) selectable via config; other models pluggable behind
ModelProvider.
Data: CRM as system of record (HubSpot or Salesforce — TBD with client);
BigQuery for lead profiles and scoring history.
Scheduling: Cloud Scheduler + Pub/Sub for cadence execution.
Infra/runtime: Google Cloud — Cloud Run, Pub/Sub, Secret Manager, BigQuery,
Cloud Scheduler. Containerized services, per-environment configs (dev/stage/prod).
Runtime versions: TBD — pin in .nvmrc / pyproject.toml / Dockerfiles once
scaffolding lands (LQABR-10/12).

3. Project structure
Monorepo with one workspace per service (layout defined in LQABR-10/12):

web/ — React operator console (E3): lead dashboard, pipeline board by stage,
cadence config, agent-swap controls, eval-run views, HITL review queue.
api/ — Node.js API gateway (E2): login/session, JWT issuance/refresh, RBAC
(admin/rep/viewer), authorized proxy into the agent layer.
agents/ — ADK agent engine (E4) + the tool surface (E5). Lead state machine,
provider interfaces/registry, and the 10 tools (see below).
evals/ — Evaluation framework (E8): labeled datasets, metrics, swap-capable runner.
infra/ — Deployment, Cloud Run configs, Secret Manager wiring, per-env config.

Tool surface (in agents/): source_leads, read_crm_leads, write_crm_lead /
update_crm_status, research_lead, generate_outreach, send_email, send_sms,
place_call, classify_reply, notify_rep. Each has a typed contract, auth handling,
and defined failure/retry behavior; each is independently testable with mocks.
4. Commands
The repo is not scaffolded yet, so these are the intended conventions — confirm exact
script names against package.json / workspace config once LQABR-10 lands, and update
this section to match reality rather than the other way around.

Install deps: npm install (root, installs all workspaces).
Run locally: npm run dev (per-workspace: npm run dev -w web / -w api / -w agents).
Tests: npm test (per-workspace with -w <name>).
Build: npm run build.
Lint: npm run lint (and npm run format if configured).
Evals: runnable from CI and from the React console; CLI entry point TBD (LQABR-47/49).

5. Conventions

Keep the provider abstractions clean: never add agent- or model-specific logic into the
engine. New agents/models register via config, not by editing the engine (E4).
CRM is the system of record. The engine holds no canonical lead state; every status
change writes back to the CRM, and on conflict CRM wins.
Tools are typed, mockable, and own their own retry/failure behavior. Do not let a tool
failure silently advance or drop a lead.
Secrets come from Secret Manager only — never hard-code or commit them.
Bad/messy ingest data is flagged, skipped, or queued for review — never crash the run,
never silently drop a lead. Every non-qualified lead gets an explicit reason
(nurture-later / disqualified / bad-data).
Accessibility: console components meet WCAG AA.
File/naming: match the conventions established by the scaffolding (LQABR-10) and the
shared component library (LQABR-22); don't introduce a second style.

6. Git / PR rules

Never push to a remote or open a PR without explicit confirmation from the user.
Stage and commit locally if asked; stop before any git push or PR creation and wait.
Branching strategy: main is the always-deployable trunk and receives only reviewed
Epic merges — never direct commits. Each Epic has a long-lived integration branch
epic/LQABR-<N>-<slug> (e.g. epic/LQABR-1-platform-foundation) cut from main. Story/Task
work branches off its Epic branch as LQABR-<ticket>-<short-slug> (e.g.
LQABR-18-jwt-refresh); Sub-tasks branch off their Story or commit on it. Flow per Epic:
branch from latest main -> Stories PR into the Epic branch -> when all children are Done
and the Epic AC is met, PR the Epic branch into main and tag a release. Merge main into
the active Epic branch whenever main advances so the final Epic->main PR stays clean.
Branch naming: epic/LQABR-<N>-<slug> for Epic integration branches;
LQABR-<ticket>-<short-slug> for Story/Task/Sub-task branches.
Commit messages: LQABR-<ticket>: <imperative summary> (e.g.
LQABR-18: add JWT refresh endpoint). One logical change per commit.
PR description: link the Jira ticket, summarize what changed and why, list how it was
tested, and call out any compliance-sensitive surface (consent, outreach, voice).
Keep PRs scoped to a single Story/Task or Sub-task where possible.

7. Jira workflow

Project key: LQABR (site: techieg.atlassian.net).
Hierarchy: Epic (E1–E9) → Story / Task → Sub-task. Stories and Tasks sit under an
Epic; Sub-tasks sit under a Story or Task.
Workflow statuses: Idea → To Do → In Progress → Testing → Done.
Mapping work to tickets:

Move the ticket to In Progress when you start it.
Branch and commits reference the ticket key (see §6).
When opening a PR (after confirmation), link the PR on the ticket and move it to
Testing.
Only move to Done after the PR is merged and the Definition of Done is met.
Do the same status transitions on the parent when its last child completes — don't
leave a parent Story open with all Sub-tasks done.



8. Definition of done
A ticket is complete (ready for review / Testing) when:

Acceptance criteria on the Jira ticket (and parent Epic) are met.
Code is tested — unit tests for the unit, mocks for external tools/CRM, and evals
updated where behavior is eval-covered.
Lint and build pass.
No secrets committed; config reads from Secret Manager.
Compliance holds where relevant: consent is checked before every send (must be 100%),
CAN-SPAM/TCPA respected, CRM writeback correct, leads never silently dropped.
PR raised and linked to the ticket (only after user confirmation).

9. Things to avoid

Do not push or open PRs without confirmation (restating §6 because it matters).
Do not build or enable the voice stage as a dependency. Voice is the highest-
liability component, phased and off by default, gated behind separate legal review.
Do not send outreach on any channel without stored consent for that channel.
Do not use LinkedIn Sales Navigator for automated outreach — research-only.
Do not auto-flip a lead to Qualified without the human-in-the-loop approval gate
in initial deployments.
Do not put canonical lead state in the engine or let it diverge from the CRM.
Do not commit secrets or read them from anywhere but Secret Manager.
Do not hard-code agent/model specifics into the engine (use the provider interfaces).
Do not edit env-specific config to make something "work locally" in a way that
leaks into stage/prod.
