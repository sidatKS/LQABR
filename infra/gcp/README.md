# Lead-Qual Data Platform — Phase 0 (BigLake Edition)

Govern B2B source data in GCS and expose it to agents via BigQuery — no ETL, no raw BQ tables, clients onboarded by dropping a CSV.

## Architecture at a glance

```
CSVs in GCS  →  BigLake external tables  →  Curated BQ tables  →  Masked sandbox views
(raw source)      (schema-on-read)           (PII tagged, RLS)      (what agents/devs see)
```

**Why BigLake instead of loading into BQ raw tables:** files stay in GCS (10× cheaper storage), new clients onboard with a file upload + one DDL statement, and curated models materialise as native tables so all security controls (policy tags, row-level security) work identically to a fully-loaded warehouse.

## What you get

- **GCS data lake** — `gs://{PROJECT}-lead-qual-lake/raw/b2b/{companies,employees,contacts}/`
- **BigLake connection** — bridges BQ external tables to GCS with a managed service account
- **3 BQ datasets** — `curated_b2b` (ext tables + native models), `cleansed_b2b` (future ETL), `sandbox_b2b` (masked views)
- **PII protection** — `name`, `email`, `phone` tagged with Data Catalog policy tag; only `pii-approved` principal can SELECT them
- **Row-level security** — `client_id` filter isolates tenants in the shared curated dataset
- **RBAC** — data-eng (curated write + GCS read), app-dev (sandbox read only), agent SA (curated + sandbox read + GCS read)
- **Masked sandbox views** — `vw_lead_masked`, `vw_contact_masked`, `vw_company` — safe for developer and agent use

## Prerequisites

- `gcloud` + `bq` + `gsutil` installed and authenticated (`gcloud auth login`) with project Owner or Editor
- Python + `pip install google-cloud-bigquery --break-system-packages` (for helper scripts)
- CSVs already in `../archive/` (5 files — companies clean/noisy, employees clean/noisy, employee_contacts)
- **Edit `config.sh`** — set `PROJECT_ID`, region, and IAM principal emails before running anything

## Run order (from this `platform/` folder)

```bash
# 0. configure — EDIT config.sh first, then:
source ./config.sh

bash 00_prereqs.sh                  # enable 7 GCP APIs
bash 01_create_gcs_bucket.sh        # create GCS bucket + folder structure
bash 02_upload_to_gcs.sh            # upload 5 CSVs to GCS
bash 03_create_biglake_connection.sh # create BigLake connection + grant SA bucket read
bash 04_create_datasets.sh          # create cleansed / curated / sandbox BQ datasets
bash 05_create_external_tables.sh   # create 5 BigLake external tables (schema-on-read)
bash 06_curated_models.sh           # materialise dim_company, dim_contact, fct_lead
bash 07_policy_tags.sh              # PII taxonomy + column-level security
bash 08_row_level_security.sh       # client_id row-level isolation
bash 09_iam_rbac.sh                 # IAM for data-eng / app-dev / agent SA
bash 10_authorized_views.sh         # masked sandbox views devs and agents consume
```

Every script is **idempotent** — safe to re-run if a step fails.

Verify with the queries in `PHASE0_PLAN.md` (replace `PROJECT` with your project ID).

## How a developer uses it

They get READER on `sandbox_b2b` only. They build and test agents against `vw_lead_masked` / `vw_contact_masked` / `vw_company` — full firmographic and scoring fields, masked contact PII. `example_queries.sql` shows the two headline use cases (company closing-probability ranking, decision-maker pick).

## How an agent uses it

The `lead-agent-runtime` SA has READ on `curated_b2b.fct_lead` (real PII, via fine-grained reader grant) and `sandbox_b2b` (masked views). The API gateway impersonates this SA. Qualification scores write back to `curated_b2b.lead_scores` (Phase 1+).

## Onboarding the next client

1. Upload their CSVs to `gs://{BUCKET}/raw/b2b/{companies,employees,contacts}/`
2. Set `CLIENT_ID` in `config.sh`
3. Re-run `05_create_external_tables.sh` (new ext table pointing to new files) then `06_curated_models.sh` + `08_row_level_security.sh`

Same schema, isolated rows. Done in under 5 minutes.

## Upgrade to native BQ tables (when query latency matters)

```sql
-- Run once per table when you're ready to cut over
CREATE OR REPLACE TABLE `PROJECT.curated_b2b.dim_company_native`
AS SELECT * FROM `PROJECT.curated_b2b.ext_companies_clean`;
-- Then update 06_curated_models.sh to read from the native table
```

No downstream SQL changes — agents and views use the curated models, not the ext tables directly.

## Notes

- **Consent fields (TCPA/CAN-SPAM):** add `consent_email` / `consent_sms` to `06_curated_models.sh` before any real outreach
- **Dynamic data masking:** this kit uses authorized views for masking; upgrade to BQ Data Policy masking rules later if app-devs need to query curated directly
- **Real PII for production agent sends:** the agent SA already has curated read access; grant `datacatalog.categoryFineGrainedReader` on the PII policy tag so it can SELECT the real email/phone columns
