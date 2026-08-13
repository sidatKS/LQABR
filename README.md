# LQABR — AI Lead Qualification & Outreach

Leads flow from **CSV (manual)** or **ZoomInfo (automatic, batches of 20)**
into **HubSpot** — the central lead-profile source — as 9-pointer profiles,
then three agents work each lead's probability up:

**Email Agent** (Mailgun: delivered/opened/clicked) → ≥ 30 →
**Text/Voice Agent** (Twilio: voicemail+SMS, or live Q&A call) → ≥ 60 →
**Scheduling Agent** (Zoom Scheduler invite in EST/CST/PST/IST) →
meeting booked (95) → human rep.

Google ADK agents on Cloud Run, orchestrated via A2A, credentials in
Secret Manager. Full map: `docs/EPICS.md` (E0–E10).

## Quick start

```bash
# 1. Dev setup
pip install -e packages/lqabr_core
pip install -r agents/lead_profile/requirements.txt
pip install pytest fastapi httpx python-multipart

# 2. Prove the code works (no credentials needed — everything mocked)
python3 -m pytest -q          # 64 tests

# 3. Dry-run the pipeline entry against the bundled CSV seeds
python agents/ingestion/src/ingestion_agent.py --source csv --dry-run
```

## Going live

1. `docs/PREREQUISITES.md` — accounts + credentials (HubSpot, Mailgun,
   Twilio, ZoomInfo, Zoom, GCP).
2. `infra/gcp/README.md` — provision & deploy (`config.sh`, then scripts
   `00` → `06`).
3. `docs/PHASE1_PLAN.md` … `PHASE5_PLAN.md` — verified rollout, phase by
   phase.

## Where things live

Agents in `agents/<name>/` (six of them), shared logic in
`packages/lqabr_core/`, provisioning in `infra/gcp/`, seed CSVs in
`data/seeds/b2b/`. Folder rationale: `READ_PRJSTRC_ME.md`.
