# Phase 5 — Orchestration, Operations & Scale (E7, E8, E9, E10)

**Objective:** the pipeline runs itself: A2A orchestration dispatches every
stage queue on a schedule, everything is deployed to Cloud Run, and the
probability model is evaluated before outreach volume scales.

## Build/verify order

1. **Deploy everything** (E8):

   ```bash
   cd infra/gcp && source ./config.sh
   bash 05_deploy_agents.sh     # 6 ADK agents + 3 webhook receivers
   bash 06_cloud_scheduler.sh   # dispatch every 30 min; ZoomInfo pull daily
   ```

   Paste the printed Mailgun/Zoom webhook URLs into their dashboards.

2. **Orchestration** (E7) — in `adk web agents/orchestrator/src`:
   - `pipeline_status` reports per-stage counts and average probability;
   - `dispatch_cycle` with `dry_run=True` shows the routing plan
     (profiled/email → Email Agent, ≥30 → Text/Voice, ≥60 → Scheduling);
   - a live cycle dispatches A2A `message/send` tasks; failures are listed
     with reasons, never dropped.

3. **Evaluation** (E9) — before raising volume:
   - export outcomes (booked / engaged / dead) per lead from HubSpot;
   - check the increments/thresholds in `lqabr_core/probability.py` against
     conversion data; tune there only (single source of truth);
   - keep a labeled snapshot in `data/seeds/` for regression evals.

4. **Hardening** (E10):
   - per-channel rate limits & send windows in agent config;
   - CI (GitHub Actions in `.github/workflows/`): pytest + lint on PR,
     deploy on release tags;
   - security posture scanning of the project/images (e.g. Wiz or Security
     Command Center);
   - cost & quota alerts (Mailgun/Twilio/ZoomInfo consumption, Cloud Run).

## Definition of Done

- A lead ingested from ZoomInfo with zero manual touches reaches
  `meeting_scheduled` purely via scheduled dispatch cycles.
- Orchestrator failures alert (Cloud Logging-based alerting) and are
  re-dispatched the next cycle.
- Full suite green: `python3 -m pytest -q` (64 tests) + e2e checklist from
  Phases 1–4 rerun against the deployed services.
