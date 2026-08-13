# LQABR Epics — E0 to E10

The delivery map for the HubSpot-central architecture (see
`docs/adr/002-hubspot-central-crm.md`). Each epic maps to a Jira Epic in
project **LQABR** (techieg.atlassian.net) and to the phases in
`docs/PHASE0_PLAN.md` … `PHASE5_PLAN.md`.

## Pipeline at a glance

```
       manual (CSV folder)            automatic (ZoomInfo API, batch of 20)
              └────────────┬────────────────┘
                    Ingestion trigger (E1)
                            │  normalized raw records
                    Lead Profile Agent (E2)
                            │  9-pointer profile
                  ┌──────── HubSpot (E3) ────────┐   central lead-profile source
                  │   lqabr_stage / lqabr_probability / counters
                  ▼
        Email Agent (E4, Mailgun)     probability +2/+5/+10 (delivered/opened/clicked)
                  │  probability >= 30
                  ▼
      Text/Voice Agent (E5, Twilio)   +3 SMS, +2 voicemail, +15 answered, +15 engaged
                  │  probability >= 60
                  ▼
     Scheduling Agent (E6, Zoom)      booking pins probability at 95
                  │
                  ▼
          meeting_scheduled — human rep handoff
```

Orchestration between stages is Google A2A (E7); everything runs as Google
ADK agents on Cloud Run (E8).

## Epic table

| Epic | Name | Scope | Primary directory |
|---|---|---|---|
| **E0** | Foundation & governance | GCP project, Secret Manager (all service keys), runtime SA + least-privilege IAM, repo tooling | `infra/gcp/` (00–02), `packages/lqabr_core/` (secrets) |
| **E1** | Dual-source ingestion | Operator-initiated trigger with `--source csv` (manual folder load) or `--source zoominfo` (automatic API pull, default batch 20); normalization to the raw-record contract | `agents/ingestion/` |
| **E2** | Lead Profile Agent | Build the 9-pointer profile per lead (name, title, company, email, phone, industry, size/revenue, location+timezone, LinkedIn); flag bad-data records, never drop | `agents/lead_profile/`, `packages/lqabr_core/` (profile) |
| **E3** | HubSpot CRM integration | HubSpot as central lead-profile source & system of record: contact upsert, `lqabr_*` custom properties, engagement counters, stage + probability writeback | `packages/lqabr_core/crm/`, `infra/gcp/04_hubspot_properties.py` |
| **E4** | Email Agent (Mailgun) | Personalized outreach to leads with listed email IDs; track delivered / read (opened, last-opened) / clicked internal links via Mailgun webhooks; increment probability | `agents/email/` |
| **E5** | Text/Voice Agent (Twilio) | Works leads past the text/voice threshold. Flow A: no answer → customized voicemail + customized SMS. Flow B: answered → conversational Q&A pattern; answered-call counter incremented in HubSpot | `agents/text_voice/` |
| **E6** | Scheduling Agent (Zoom Scheduler) | Emails leads past the scheduling threshold a Zoom Scheduler booking link offering EST/CST/PST/IST; records booked meetings | `agents/scheduling/` |
| **E7** | Orchestration (A2A) | Orchestrator agent reads stage queues from HubSpot and dispatches to stage agents via A2A `message/send`; Cloud Scheduler drives the cycle | `agents/orchestrator/`, `infra/gcp/06_cloud_scheduler.sh` |
| **E8** | Runtime & operations | ADK web/dev harness, Cloud Run (managed sandbox) hosting for agents + webhook receivers, logging/monitoring, security posture scanning (e.g. Wiz) | `infra/gcp/` (05, cloud-run/) |
| **E9** | Evaluation & scoring model | Probability model tuning against labeled outcomes; eval harness before scaling outreach volume | `packages/lqabr_core/probability.py`, `tests/` |
| **E10** | Production hardening & scale | Rate limits, retries/backoff review, CI/CD, multi-client isolation, cost controls | cross-cutting |

## Probability model (single source of truth: `lqabr_core/probability.py`)

| Event | Change | Recorded by |
|---|---|---|
| Profile saved to HubSpot | starts at **10** | Lead Profile Agent |
| Email delivered | **+2** | Mailgun webhook |
| Email opened (read / last-opened) | **+5** | Mailgun webhook |
| Email internal link clicked | **+10** | Mailgun webhook |
| SMS delivered | **+3** | Twilio status callback |
| Voicemail left | **+2** | Twilio answer webhook |
| Call answered | **+15** | Twilio answer webhook |
| Call Q&A completed with interest | **+15** | Twilio gather webhook |
| Meeting booked | pinned to **95** | Zoom event webhook |

Thresholds: **30** promotes to Text/Voice, **60** promotes to Scheduling.
