# ADR 002 — HubSpot as the central lead-profile source; retire the BigLake data platform

**Status:** Accepted (2026-07) — supersedes [ADR 001](001-biglake-architecture.md)

## Context

ADR 001 established a GCS → BigLake → BigQuery governance stack (policy
tags, row-level security, masked views) as the lead data platform, with the
CRM decision (HubSpot vs Salesforce) deferred.

The product direction changed:

- **HubSpot is decided** as the CRM and becomes the *central lead-profile
  source* — every agent reads and writes lead state there.
- Lead acquisition is **dual-source**: manual CSV folder loads, or automatic
  **ZoomInfo API** pulls (default batch of 20), both behind one
  operator-initiated trigger.
- The qualification signal is a **probability score** built up by three
  outreach agents — Email (Mailgun), Text/Voice (Twilio), Scheduling
  (Zoom Scheduler) — with engagement counters incremented directly on the
  HubSpot contact.

Running a parallel BigQuery copy of lead state alongside HubSpot would
recreate the dual-source-of-truth problem the CRM-is-system-of-record rule
exists to prevent, and the BigLake governance surface (policy tags, RLS,
authorized views) duplicated what HubSpot provides natively (field-level
permissions, teams, audit).

## Decision

1. **HubSpot is the system of record and the only canonical lead store.**
   Lead profiles (the 9 pointers), pipeline stage, probability, and
   engagement counters live on the HubSpot contact (`lqabr_*` properties).
2. **Retire the Phase 0 BigLake/BigQuery stack** — provisioning scripts,
   SQL models, schemas, and masked views are removed. The CSV seeds remain
   in `data/seeds/b2b/` as the manual ingestion source.
3. **Agents are Google ADK services on Cloud Run**, orchestrated with
   Google A2A; all service credentials (HubSpot, Mailgun, Twilio, ZoomInfo,
   Zoom) live in Google Secret Manager only.
4. **Pub/Sub remains** for triggers and engagement-event fan-out; a future
   analytics warehouse (if ever needed) subscribes to those events instead
   of holding lead state.

## Consequences

- One canonical store: no CRM↔warehouse sync drift; on conflict CRM wins is
  trivially true.
- Query/reporting over leads happens in HubSpot (lists, reports) rather
  than SQL; complex analytics would need the Pub/Sub event stream to be
  materialized later (deliberately out of scope now).
- PII governance shifts from BigQuery policy tags to HubSpot permissions —
  operators grant field access in HubSpot, not IAM.
- ADR 001 remains in the repo for history but no longer describes the
  system; its infra scripts were deleted with this change.
