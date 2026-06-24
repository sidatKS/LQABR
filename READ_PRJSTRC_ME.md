# LQABR — Project Folder Structure Rationale

> **Who this is for:** Any developer onboarding to this repo, or contributing to any layer of the LQABR platform — from the React console to the GCP data layer.
> **What it explains:** Why each directory exists, how it maps to the system design, and how to decide where new code belongs.

---

## Mental Model

LQABR is a **multi-service monorepo**. The folder structure follows a strict layered separation that mirrors how data and control flow through the system:

```
User / Agent request
        │
        ▼
apps/console          ← operator sees the world here (React)
        │
        ▼
apps/api-gateway      ← single ingress; auth, rate-limiting, routing (Node.js)
        │
        ▼
agents/               ← Google ADK agents orchestrate the qualification pipeline
        │
        ▼
integrations/         ← CRM read/write (HubSpot or Salesforce)
        │
        ▼
data/                 ← BigQuery curated models and sandbox views
        │
        ▼
infra/                ← GCP provisioning (BigLake, Cloud Run, Pub/Sub, IAM)
```

Shared code that any layer needs (TypeScript types, compliance rules, utility functions) lives in `packages/` and is imported by name — never by relative path across layer boundaries.

---

## Top-Level Directory Reference

### `apps/`

Deployable, user-facing applications. Each sub-directory is a self-contained project with its own `package.json`, build config, and Dockerfile (where applicable). This is the standard pattern for monorepos using Turborepo, Nx, or similar — the `apps/` name signals "these things run in production and face users or external callers."

| Directory | What it is | Maps to epic |
|---|---|---|
| `apps/console/` | React 18 operator console — KPI dashboard, eight-stage kanban, human-in-the-loop review queue, cadence config, agent/model swapper | E7 |
| `apps/api-gateway/` | Node.js API gateway — single ingress for the console and agent service accounts; handles JWT auth, rate-limiting, request routing to Cloud Run agent endpoints | E2 |

**Why separate from `agents/`?** The gateway is an HTTP server with middleware concerns (auth, CORS, logging, circuit-breaking). Agents are ADK processes with tool registrations and prompt chains. They scale, deploy, and fail independently.

**`apps/console/src/` sub-structure** follows the standard React feature-folder pattern:
- `components/` — reusable UI atoms and molecules
- `pages/` — route-level components (one file per route)
- `hooks/` — custom React hooks (data fetching, state sync)
- `store/` — global state (Zustand / Redux slice — TBD at implementation)
- `styles/` — global CSS, design tokens

**`apps/api-gateway/src/` sub-structure** follows the standard Express/Fastify MVC-ish layout:
- `controllers/` — request/response handlers, one file per resource
- `routes/` — route definitions that wire URLs to controllers
- `middleware/` — auth, validation, logging, error handling
- `services/` — business logic that controllers call (no HTTP context here)
- `config/` — environment-aware configuration loading

---

### `agents/`

All Google ADK agent definitions live here. Each agent is a Cloud Run service with its own tool registrations, system prompts, and evaluation harness. Grouping by agent role (not by API endpoint or data entity) makes the pipeline readable as a sequence: `orchestrator → enrichment → scoring → outreach → handoff`.

| Directory | Responsibility | Maps to epic |
|---|---|---|
| `agents/orchestrator/` | Master agent; owns the eight-stage state machine. Routes leads between agents, enforces stage gates, emits Pub/Sub events. | E3 |
| `agents/enrichment/` | Pulls firmographic and contact data; deduplicates; writes enriched records back to `curated_b2b`. | E3 |
| `agents/scoring/` | Qualification scoring logic — the E9 model. Reads `fct_lead`, writes `lead_scores`. | E9 |
| `agents/outreach/` | Multi-channel outreach, split by channel so each can be independently deployed, rate-limited, and compliance-gated. `email/`, `sms/`, `voice/` each represent a distinct integration surface. | E5 |
| `agents/handoff/` | Prepares and delivers the qualified-lead package to the sales rep; triggers the human-in-the-loop approval flow. | E8 |

Each agent directory contains a `src/` (implementation) and `tests/` (unit tests for tool functions and prompt outputs). Integration tests that span multiple agents live in `tests/integration/`.

---

### `packages/`

Internal shared libraries. Nothing here is deployed on its own — it is imported by apps and agents via the monorepo workspace protocol (e.g., `import { Lead } from '@lqabr/types'`). This prevents type drift and logic duplication across services.

| Directory | What it holds | Why here, not in the consuming service |
|---|---|---|
| `packages/types/` | Shared TypeScript interfaces and enums: `Lead`, `Company`, `Contact`, `PipelineStage`, `OutreachChannel`, etc. | A type defined once and imported everywhere guarantees that the console, the gateway, and every agent agree on the shape of data at compile time. |
| `packages/compliance/` | TCPA / CAN-SPAM rule engine — consent checks, opt-out lookups, send-time windows, channel suppression logic. | Compliance rules are a cross-cutting concern. Embedding them in `agents/outreach/` would mean the gateway and console couldn't enforce the same rules without duplicating code. A shared package ensures a single source of truth. |
| `packages/utils/` | Pure utility functions: date helpers, BigQuery row mappers, hashing utilities for PII surrogate keys, retry wrappers. | Any helper used by more than one service belongs here, not in either service. |

---

### `integrations/`

Third-party CRM connector adapters. The CRM vendor is an open architectural decision (HubSpot vs. Salesforce). By isolating each vendor behind its own directory — with a common interface defined in `packages/types/` — the rest of the system calls `integrations/hubspot` or `integrations/salesforce` without knowing which is active. Swapping vendor is a config change, not a code rewrite.

| Directory | Status |
|---|---|
| `integrations/hubspot/` | Placeholder — awaiting CRM vendor decision |
| `integrations/salesforce/` | Placeholder — awaiting CRM vendor decision |

---

### `infra/`

Infrastructure provisioning code. Nothing here runs in the application critical path — this directory is for operators and platform engineers, not app developers.

| Directory | What it holds |
|---|---|
| `infra/gcp/bigquery/` | BigQuery dataset, table, view, and policy tag DDL scripts |
| `infra/gcp/cloud-run/` | Cloud Run service YAML / `gcloud run deploy` wrappers |
| `infra/gcp/pubsub/` | Pub/Sub topic and subscription provisioning |
| `infra/gcp/iam/` | IAM role bindings and service account creation scripts |
| `infra/terraform/` | Reserved for future Terraform IaC when manual scripts need to be replaced by state-managed provisioning |

> **Run order:** Scripts are numbered `00` through `10` and must be run in sequence. Start with `source ./config.sh`, then `bash 00_prereqs.sh` through `bash 10_authorized_views.sh`. Every script is idempotent — safe to re-run. See `infra/gcp/README.md` for the full run guide.

---

### `data/`

The data platform layer — SQL and schema artefacts for BigQuery. Application code queries BigQuery but never owns the DDL for tables or views; that lives here so data engineers can iterate on the data model without touching application source trees.

| Directory | What it holds |
|---|---|
| `data/seeds/b2b/` | Bootstrap CSV and metadata files — the source datasets uploaded to GCS (`raw/b2b/`) and read via BigLake external tables. Static reference data; never modified in-place. Directory name mirrors the GCS prefix so the path intent is self-evident. |
| `data/models/curated/` | SQL definitions for `dim_company`, `dim_contact`, `fct_lead`, `lead_scores` — the native BigQuery tables that PII policy tags and row-level security attach to |
| `data/models/sandbox/` | SQL definitions for `vw_lead_masked`, `vw_contact_masked`, `vw_company` — the authorized views developers and agents consume |
| `data/schemas/` | JSON schema files describing table column types, nullable flags, and descriptions; used for validation and documentation generation |
| `data/migrations/` | Ordered, idempotent DDL migration scripts; numbered sequentially (e.g., `001_add_consent_fields.sql`). Run by the CI/CD pipeline against the target dataset. |

---

### `tests/`

Cross-service test suites that cannot live inside a single service's own `tests/` folder because they exercise boundaries between services.

| Directory | What goes here |
|---|---|
| `tests/e2e/` | Full end-to-end scenarios: a lead enters the pipeline, travels through all eight stages, and surfaces in the operator console. Runs against a staging environment. |
| `tests/integration/` | Service-pair tests: API gateway → orchestrator agent, enrichment agent → BigQuery, scoring agent → `lead_scores` write-back, outreach agent → compliance package. |

Unit tests stay co-located with their service (e.g., `agents/scoring/tests/`) because they test implementation details that only that service owns.

---

### `docs/`

Human-authored project documentation. Not auto-generated.

| Directory | What goes here |
|---|---|
| `docs/adr/` | Architecture Decision Records — one Markdown file per decision. `001-biglake-architecture.md` documents the BigLake-over-native-BQ decision. Format: context → decision → consequences. |
| `docs/api/` | OpenAPI 3.x specifications for the API gateway's public surface. Source of truth for contract testing and SDK generation. |
| `docs/PHASE0_PLAN.md` | Phase 0 project plan — objective, BigLake architecture diagram, run order table, config checklist, verification queries, and next-phase handoff. Start here before running `infra/gcp/` scripts. |
| `docs/example-queries.sql` | Reference SQL queries demonstrating how agents and developers consume the sandbox views (`vw_lead_masked`, `vw_company`). Not executed by any application — documentation only. |

---

### `.github/workflows/`

GitHub Actions CI/CD pipeline definitions. Kept at repo root per GitHub convention. Typical files:

- `ci.yml` — lint, type-check, unit test on every pull request
- `deploy-staging.yml` — deploy to staging on merge to `main`
- `deploy-prod.yml` — deploy to production on release tag

---

## Where Does New Code Go?

Use this decision tree when you're not sure where to put something new:

```
Is it a deployable service (has its own HTTP port or Cloud Run entrypoint)?
  YES → apps/ (user-facing) or agents/ (ADK pipeline agent)
  NO  ↓

Is it shared across two or more services?
  YES → packages/
  NO  ↓

Is it a third-party API adapter (CRM, messaging vendor)?
  YES → integrations/
  NO  ↓

Is it SQL, schema, or migration DDL for BigQuery?
  YES → data/
  NO  ↓

Is it GCP provisioning (bucket, dataset, IAM, topic)?
  YES → infra/gcp/
  NO  ↓

Is it a cross-service test?
  YES → tests/e2e/ or tests/integration/
  NO  ↓

Is it a decision record or API contract?
  YES → docs/adr/ or docs/api/
```

---

## Key Conventions

**Monorepo tooling:** All `apps/`, `agents/`, and `packages/` directories are workspace members. Root `package.json` (to be created) declares `workspaces: ["apps/*", "agents/*", "packages/*", "integrations/*"]`. Internal packages are imported as `@lqabr/<name>` — configure in each package's `package.json` under `"name"`.

**No cross-layer relative imports:** `apps/console` must not `import ../../agents/orchestrator/src/...`. Cross-layer dependencies flow through `packages/` only.

**Idempotent infrastructure:** All scripts in `infra/gcp/` must be safe to re-run. Use `--if-exists` / `CREATE OR REPLACE` / `IF NOT EXISTS` guards consistently.

**Agent unit tests co-located, integration tests centralised:** Unit tests live in the agent's own `tests/` folder. Tests that cross service boundaries live in `tests/integration/`.

**ADRs are immutable once accepted:** Create a new ADR to supersede an old one; never edit a closed ADR in place.

---

## Mapping to LQABR Epics

| Epic | Description | Primary directory |
|---|---|---|
| E1 | Data platform (GCS, BigLake, BigQuery, IAM) | `infra/gcp/`, `data/` |
| E2 | API gateway | `apps/api-gateway/` |
| E3 | Multi-agent orchestration (ADK) | `agents/orchestrator/`, `agents/enrichment/` |
| E4 | CRM integration | `integrations/hubspot/` or `integrations/salesforce/` |
| E5 | Multi-channel outreach | `agents/outreach/` |
| E6 | Compliance (TCPA / CAN-SPAM) | `packages/compliance/` |
| E7 | Operator console | `apps/console/` |
| E8 | Human-in-the-loop approval | `agents/handoff/` |
| E9 | Lead qualification scoring | `agents/scoring/` |

---

*Last updated: 2026-06-18*
