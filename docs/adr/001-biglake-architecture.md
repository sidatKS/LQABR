# Phase 0 — Data Platform Project Plan
### Lead Qualification (LQABR) · BigLake Architecture

---

## Objective

Stand up a governed, multi-tenant data platform on GCP that the LQABR agent pipeline can query from day one. Raw data files live in GCS; BigQuery reads them via BigLake external tables without ETL. Curated models are materialised as native BQ tables so policy tags and row-level security can attach. Developers and agents access only masked sandbox views.

**Decision rationale — BigLake over native BQ raw tables:**

| Concern | BigLake (chosen) | Native BQ raw tables |
|---|---|---|
| Setup speed | Drop CSVs → done | ETL pipeline required |
| Storage cost | GCS (~$0.02/GB) | BQ storage (~$0.20/GB) |
| New client onboard | Upload CSV + point ext table | Re-run load pipeline |
| Query performance | Slightly slower (file scan) | Native columnar — faster |
| Policy tags / row-level security | ✅ on curated native tables | ✅ same |
| Production at scale | Migrate ext→native table: `CREATE TABLE AS SELECT` | Already there |

**Upgrade path:** when query latency matters at scale, replace the external tables with `CREATE TABLE AS SELECT FROM ext_*` — one command per table, zero SQL changes downstream.

---

## Architecture

```
../archive/*.csv
       │
       ▼  (02_upload_to_gcs.sh)
gs://{PROJECT}-lead-qual-lake/
  └── raw/b2b/
        ├── companies/   ← companies_clean_734.csv, companies_noisy_734.csv
        ├── employees/   ← employees_clean_5234.csv, employees_noisy_5234.csv
        └── contacts/    ← employee_contacts_5234.csv
             │
             │  BigLake connection (03_create_biglake_connection.sh)
             ▼
  curated_b2b dataset (BQ)
    ├── ext_companies_clean   ← external table → GCS
    ├── ext_companies_noisy   ← external table → GCS
    ├── ext_employees_clean   ← external table → GCS
    ├── ext_employees_noisy   ← external table → GCS
    ├── ext_employee_contacts ← external table → GCS  [PII]
    │
    │  06_curated_models.sh (CTAS from external tables)
    ├── dim_company           ← native BQ table  [client_id stamped]
    ├── dim_contact           ← native BQ table  [PII + masked surrogates]
    └── fct_lead              ← native BQ table  [agents read this]
             │
             │  07_policy_tags.sh  →  name/email/phone columns tagged + restricted
             │  08_row_level_security.sh  →  client_id isolation
             │  09_iam_rbac.sh  →  data-eng / app-dev / agent SA
             ▼
  sandbox_b2b dataset (BQ)
    ├── vw_company          ← all firmographic cols, no PII
    ├── vw_contact_masked   ← masked_name, contact_hash, email_domain only
    └── vw_lead_masked      ← denormalised, masked — what devs consume
```

---

## Run Order

All scripts live in `platform/`. Configure `config.sh` first, then run in sequence. Each script is **idempotent** — safe to re-run if a step fails mid-way.

| # | Script | What it does | Who runs it | Prereq |
|---|---|---|---|---|
| 0 | `source ./config.sh` | Load env vars | You | Edit PROJECT_ID + email bindings first |
| 1 | `bash 00_prereqs.sh` | Enable 7 GCP APIs | You | `gcloud auth login` with project owner/editor |
| 2 | `bash 01_create_gcs_bucket.sh` | Create GCS bucket + folder structure | You | APIs enabled |
| 3 | `bash 02_upload_to_gcs.sh` | Upload 5 CSVs to GCS | You | Bucket exists, CSVs in `../archive/` |
| 4 | `bash 03_create_biglake_connection.sh` | Create BigLake connection + grant SA bucket read | You | Bucket + CSVs exist |
| 5 | `bash 04_create_datasets.sh` | Create 3 BQ datasets (cleansed, curated, sandbox) | You | Connection ready |
| 6 | `bash 05_create_external_tables.sh` | Create 5 BigLake external tables over GCS files | You | Datasets + connection exist |
| 7 | `bash 06_curated_models.sh` | Build dim_company, dim_contact, fct_lead | You | External tables exist |
| 8 | `bash 07_policy_tags.sh` | Create PII taxonomy, tag email/phone/name columns | You | Curated tables exist |
| 9 | `bash 08_row_level_security.sh` | Row-level security: isolate by client_id | You | Policy tags applied |
| 10 | `bash 09_iam_rbac.sh` | Set IAM: data-eng / app-dev / agent SA roles | You | SA created, datasets + bucket exist |
| 11 | `bash 10_authorized_views.sh` | Create masked sandbox views, authorise them | You | Curated tables + sandbox dataset exist |

---

## Config Checklist (edit `config.sh` before running anything)

- [ ] `PROJECT_ID` — your GCP project ID
- [ ] `REGION` — BigQuery multi-region (US / EU) or single region (us-central1, asia-south1 …)
- [ ] `TAXONOMY_REGION` — lowercase version of region (us, eu, us-central1 …)
- [ ] `BUCKET_LOCATION` — match REGION
- [ ] `BIGLAKE_REGION` — lowercase, match REGION
- [ ] `GROUP_DATAENG` — `user:you@domain.com` or `group:data-eng@yourco.com`
- [ ] `GROUP_APPDEV` — same format
- [ ] `GROUP_PII_VIEWERS` — same format
- [ ] `CLIENT_ID` — slug for this demo dataset (default `demo-client-01` is fine)

---

## Verification Queries

Run these against your project after completing all steps. Replace `PROJECT` with your `PROJECT_ID`.

```sql
-- 1. Check curated tables exist and have rows
SELECT 'dim_company' AS tbl, COUNT(*) AS rows FROM `PROJECT.curated_b2b.dim_company`
UNION ALL
SELECT 'dim_contact', COUNT(*) FROM `PROJECT.curated_b2b.dim_contact`
UNION ALL
SELECT 'fct_lead', COUNT(*) FROM `PROJECT.curated_b2b.fct_lead`;

-- 2. Confirm PII blocked for non-approved users (run as app-dev user — should error)
SELECT email FROM `PROJECT.curated_b2b.dim_contact` LIMIT 1;

-- 3. Confirm sandbox views work (run as app-dev user — should succeed)
SELECT masked_name, email_domain, industry, seniority_level
FROM `PROJECT.sandbox_b2b.vw_lead_masked`
WHERE decision_maker_flag = 'Yes'
ORDER BY influence_score DESC
LIMIT 10;

-- 4. Confirm row-level isolation (only demo-client-01 rows visible)
SELECT DISTINCT client_id FROM `PROJECT.curated_b2b.fct_lead`;

-- 5. Top companies by closing probability proxy (agent use-case)
SELECT company_id, industry, region, conversion_rate_pct, contract_status
FROM `PROJECT.sandbox_b2b.vw_company`
ORDER BY conversion_rate_pct DESC
LIMIT 10;
```

---

## What Phase 0 Does NOT Include

These are Phase 1+ concerns:

- Real-time CRM sync (HubSpot / Salesforce → GCS / BQ)
- Consent fields (TCPA/CAN-SPAM `consent_email`, `consent_sms`) — add in `06_curated_models.sh` before any real outreach
- Converting external tables to native BQ tables for production query performance
- Harness CI/CD pipeline for schema migrations
- Agent service account key rotation schedule

---

## Next Phase Handoff

When Phase 0 is verified, the agent runtime (`lead-agent-runtime@PROJECT.iam.gserviceaccount.com`) has:
- Read access to `curated_b2b.fct_lead` (real PII, gated behind policy tag)
- Read access to `sandbox_b2b.vw_lead_masked` (for agent dev/eval work)
- BigLake connection user rights

The API gateway (Phase 1 / E2) can impersonate this SA to query leads. The qualification scoring logic (E9) will write results back to a `lead_scores` table in `curated_b2b`.
