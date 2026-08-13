# LQABR — Project Folder Structure Rationale

> **Who this is for:** any developer onboarding to this repo.
> **What it explains:** why each directory exists, how it maps to the
> HubSpot-central pipeline design, and where new code belongs.

---

## Mental Model

LQABR is a **Python multi-agent monorepo**. The structure mirrors how a
lead flows through the system:

```
CSV folder (manual)      ZoomInfo API (auto, 20/pull)
        └──────────┬──────────┘
        agents/ingestion         ← operator-initiated trigger, --source csv|zoominfo
                   │  normalized raw records
        agents/lead_profile      ← builds the 9-pointer profile
                   │
        packages/lqabr_core/crm  ← HubSpot upsert (system of record)
                   │
        agents/email             ← Mailgun outreach + engagement webhook
                   │  probability ≥ 30
        agents/text_voice        ← Twilio SMS/voice + TwiML webhooks
                   │  probability ≥ 60
        agents/scheduling        ← Zoom Scheduler invites + booking webhook
                   │
        agents/orchestrator      ← A2A routing across all of the above
```

Shared code lives in `packages/lqabr_core` and is imported by name
(`from lqabr_core import ...`) — never by relative path across agents.

---

## Top-Level Directory Reference

### `agents/`

One directory per Google ADK agent. Each contains `src/` (implementation),
`tests/` (unit tests, co-located), `requirements.txt`, `.env.example`.
Every `src/` has an `agent.py` shim re-exporting `root_agent` so the ADK
loader discovers it (`adk web agents/<name>/src`).

| Directory | Responsibility | Epic |
|---|---|---|
| `agents/ingestion/` | Dual-source trigger: CSV folder load or ZoomInfo pull (batch 20); normalization to the raw-record contract | E1 |
| `agents/lead_profile/` | 9-pointer profile build + HubSpot save (via lqabr_core) | E2 |
| `agents/email/` | Mailgun outreach; `webhook_app.py` records delivered/opened/clicked | E4 |
| `agents/text_voice/` | Twilio SMS + calls; `conversation.py` (Q&A TwiML state machine); `webhook_app.py` (answer/gather/status) | E5 |
| `agents/scheduling/` | Zoom Scheduler invites (EST/CST/PST/IST); `webhook_app.py` records bookings | E6 |
| `agents/orchestrator/` | Pipeline state machine; A2A dispatch to stage agents | E7 |

**Pattern in every agent:** deterministic, typed, mockable logic
(clients, state machines) in plain modules; the ADK/model wrapper
(`<name>_agent.py` with `root_agent`) only exposes those as tools.

### `packages/`

| Directory | What it holds |
|---|---|
| `packages/lqabr_core/` | The only shared-code path: `types` (LeadProfile/stages/events), `probability` (increments + thresholds — single source of truth), `secrets` (Secret Manager + env fallback), `crm` (CRMClient interface, HubSpotClient), `mailgun` (shared by email + scheduling), `timezones` (EST/CST/PST/IST), `profile` (record → profile → upsert) |

If two agents need it, it goes here — never a cross-agent import.

### `integrations/` — *(removed)*

HubSpot was decided; the adapter lives in `lqabr_core/crm/` behind the
vendor-neutral `CRMClient` interface. A future second CRM would be a new
module there, selected by config.

### `infra/`

| Directory | What it holds |
|---|---|
| `infra/gcp/` | Idempotent scripts `00`–`06` (APIs, SA/IAM, Secret Manager, Pub/Sub, HubSpot properties, Cloud Run deploy, Cloud Scheduler) + `config.sh` |
| `infra/gcp/cloud-run/` | Parametrized `Dockerfile`, `entrypoint.sh` (agent vs webhook), `cloudbuild.yaml` |
| `infra/terraform/` | Reserved for future state-managed IaC |

### `data/`

| Directory | What it holds |
|---|---|
| `data/seeds/b2b/` | The manual-source CSVs (+ generated `output/`). Static reference data; never modified in place. Drop replacement exports here (or any folder passed via `--csv-folder`) to ingest manually. |

### `tests/`

Cross-service suites only (`e2e/`, `integration/`). Unit tests stay
co-located with their agent/package.

### `docs/`

| File/Dir | What it is |
|---|---|
| `docs/EPICS.md` | E0–E10 epic map + probability model table |
| `docs/PHASE0_PLAN.md` … `PHASE5_PLAN.md` | Phase runbooks with Definition of Done |
| `docs/PREREQUISITES.md` | Accounts, credentials, tooling — **start here** |
| `docs/adr/` | Architecture Decision Records — immutable once accepted; `002-hubspot-central-crm.md` supersedes `001` |
| `docs/api/` | OpenAPI specs for the webhook surfaces (as they stabilize) |
| `docs/archive/` | Historical architecture diagrams (pre-ADR-002 design) |

### `.github/workflows/`

CI/CD (Phase 5): pytest + lint on PR; deploy on release tags.

---

## Where Does New Code Go?

```
Is it a new pipeline stage or channel (has its own root_agent)?
  YES → agents/<new_name>/            NO ↓
Is it used by two or more agents?
  YES → packages/lqabr_core/          NO ↓
Is it a third-party client used by ONE agent?
  YES → that agent's src/ (typed, session-injectable)   NO ↓
Is it GCP provisioning or deploy tooling?
  YES → infra/gcp/                    NO ↓
Is it a cross-service test?
  YES → tests/e2e|integration/        NO ↓
Decision record or API contract? → docs/adr/ or docs/api/
```

---

## Key Conventions (enforced in review)

- **HubSpot is the system of record** — no canonical lead state anywhere else.
- **Probability rules only in `lqabr_core/probability.py`.**
- **No cross-agent imports**; shared code flows through `packages/`.
- **Typed, mockable clients** that accept an injected `requests.Session`.
- **Flag, never drop**: unresolved/failed records always carry a reason.
- **Idempotent infra scripts**; **secrets only in Secret Manager**.
- **ADRs are immutable once accepted** — supersede, never edit.

---

*Last updated: 2026-07-15 (HubSpot-central redesign, ADR 002)*
