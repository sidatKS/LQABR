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
pip install pytest fastapi htt