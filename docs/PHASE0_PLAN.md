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

**Full decision record:** `docs/adr/001-biglake-architecture.md`

**Upgrade path:** when query latency matters at scale, replace the external tables with `CREATE TABLE AS SELECT FROM ext_*` — one command per table, zero SQL changes downstream.

---

## Architecture

```
data/seeds/b2b/*.csv
       │
       ▼  (infra/gcp/02_upload_to_gcs.sh)
gs://{PROJECT}-lead-qual-lake/
  └── raw/b2b/
        ├── companies/   ← companies_clean_734.csv, companies_noisy_734.csv
        ├── employees/   ← employees_clean_5234.csv, employees_noisy_5234.csv
        └── contacts/    ← employee_contacts_5234.csv
             │
             │  BigLake connection (infra/gcp/03_create_biglake_connection.sh)
             ▼
  curated_b2b dataset (BQ)
    ├── ext_companies_clean   ← external table → GCS
    ├── ext_companies_noisy   ← external table → GCS
    ├── ext_employees_clean   ← external table → GCS
    ├── ext_employees_noisy   ← external table → GCS
    ├── ext_employee_contacts ← external table → GCS  [PII]
    │
    │  infra/gcp/06_curated_models.sh  (SQL source: data/models/curated/)
    ├── dim_company           ← native BQ table  [client_id stamped]
    ├── dim_contact           ← native BQ table  [PII + masked surrogates]
    └── fct_lead              ← native BQ table  [agents read this]
             │
             │  infra/gcp/07_policy_tags.sh  →  name/email/phone columns tagged + restricted
             │  infra/gcp/08_row_level_security.sh  →  client_id isolation
             │  infra/gcp/09_iam_rbac.sh  →  data-eng / app-dev / agent SA
             ▼
  sandbox_b2b dataset (BQ)
    ├── vw_company          ← SQL source: data/models/sandbox/vw_company.sql
    ├── vw_contact_masked   ← SQL source: data/models/sandbox/vw_contact_masked.sql
    └── vw_lead_masked      ← SQL source: data/models/sandbox/vw_lead_masked.sql
```

---

## Repo Locations

| Artefact | Location |
|---|---|
| GCP provisioning scripts | `infra/gcp/00_prereqs.sh` → `10_authorized_views.sh` |
| GCP config (project, region, IAM) | `infra/gcp/config.sh` |
| Source CSV seed files | `data/seeds/b2b/` |
| Curated model SQL (dim_company, dim_contact, fct_lead) | `data/models/curated/` |
| Sandbox view SQL (vw_lead_masked, vw_contact_masked, vw_company) | `data/models/sandbox/` |
| Table JSON schemas | `data/schemas/` |
| Verification queries | `docs/example-queries.sql` |
| Architecture decision record | `docs/adr/001-biglake-architecture.md` |
| Python IAM helpers | `infra/gcp/apply_dataset_iam.py`, `apply_policy_tags.py`, `authorize_views.py` |

---

## Run Order

All scripts live in `infra/gcp/`. Configure `config.sh` first, then run in sequence. Each script is **idempotent** — safe to re-run if a step fails mid-way.

| # | Script | What it does | Who runs it | Prereq |
|---|---|---|---|---|
| 0 | `source ./config.sh` | Load env vars | You | Edit PROJECT_ID + email bindings first |
| 1 | `bash 00_prereqs.sh` | Enable 7 GCP APIs | You | `gcloud auth login` with project owner/editor |
| 2 | `bash 01_create_gcs_bucket.sh` | Create GCS bucket + folder structure | You | APIs enabled |
| 3 | `bash 02_upload_to_gcs.sh` | Upload 5 CSVs from `data/seeds/b2b/` to GCS | You | Bucket exists, CSVs in `data/seeds/b2b/` |
| 4 | `bash 03_create_biglake_connection.sh` | Create BigLake connection + grant SA bucket read | You | Bucket + CSVs exist |
| 5 | `bash 04_create_datasets.sh` | Create 3 BQ datasets (cleansed, curated, sandbox) | You | Connection ready |
| 6 | `bash 05_create_external_tables.sh` | Create 5 BigLake external tables over GCS files | You | Datasets + connection exist |
| 7 | `bash 06_curated_models.sh` | Build dim_company, dim_contact, fct_lead | You | External tables exist |
| 8 | `bash 07_policy_tags.sh` | Create PII taxonomy, tag email/phone/name columns | You | Curated tables exist |
| 9 | `bash 08_row_level_security.sh` | Row-level security: isolate by client_id | You | Policy tags applied |
| 10 | `bash 09_iam_rbac.sh` | Set IAM: data-eng / app-dev / agent SA roles | You | SA created, datasets + bucket exist |
| 11 | `bash 10_authorized_views.sh` | Create masked sandbox views, authorise them | You | Curated tables + sandbox dataset exist |

---

## Config Checklist (`infra/gcp/config.sh`)

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

Run these in the BigQuery console after completing all steps. Also available as runnable file at `docs/example-queries.sql`. Replace `PROJECT` with your `PROJECT_ID`.

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
```

---

## What Phase 0 Does NOT Include

These are Phase 1+ concerns, tracked in Jira under the respective epics:

- Real-time CRM sync (HubSpot / Salesforce → GCS / BQ) — E4
- Consent fields (TCPA/CAN-SPAM `consent_email`, `consent_sms`) — add in `data/models/curated/dim_contact.sql` before any real outreach — E6
- Converting external tables to native BQ tables for production query performance
- Harness CI/CD pipeline for schema migrations (`data/migrations/`)
- Agent service account key rotation schedule

---

## Next Phase Handoff

When Phase 0 is verified, the agent runtime (`lead-agent-runtime@PROJECT.iam.gserviceaccount.com`) has:

- Read access to `curated_b2b.fct_lead` (real PII, gated behind policy tag)
- Read access to `sandbox_b2b.vw_lead_masked` (for agent dev/eval work)
- BigLake connection user rights

The API gateway (E2 / `apps/api-gateway/`) can impersonate this SA to query leads. The qualification scoring agent (E9 / `agents/scoring/`) will write results back to a `lead_scores` table in `curated_b2b` — add the DDL to `data/models/curated/lead_scores.sql` when E9 begins.
