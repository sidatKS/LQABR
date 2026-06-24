#!/usr/bin/env bash
# 06_curated_models.sh — build curated TABLES from the BigLake external tables.
# BQ queries external tables with identical SQL to native tables — no syntax change.
# We materialise as native tables here so row-level + column-level security can
# attach (policy tags don't apply to external tables directly).
# CREATE OR REPLACE makes reruns idempotent.
set -euo pipefail
source ./config.sh

bq --location="${REGION}" query --use_legacy_sql=false <<SQL

-- dim_company: one row per account, stamped with tenancy fields
CREATE OR REPLACE TABLE \`${PROJECT_ID}.${DS_CURATED}.dim_company\` AS
SELECT
  '${CLIENT_ID}'     AS client_id,
  '${SOURCE_SYSTEM}' AS source_system,
  company_id, industry, company_size, annual_revenue_m, marketing_spend_k,
  campaign_type, leads_generated, conversion_rate_pct, region, district,
  last_product_1, last_product_2, frequency_of_purchase, days_since_last_purchase,
  contract_status, total_purchases_last_year, payment_behavior, preferred_channel,
  sales_rep,
  CURRENT_TIMESTAMP() AS loaded_at
FROM \`${PROJECT_ID}.${DS_CURATED}.ext_companies_clean\`;

-- dim_contact: person + firmographic join + pre-computed masked surrogates
CREATE OR REPLACE TABLE \`${PROJECT_ID}.${DS_CURATED}.dim_contact\` AS
SELECT
  '${CLIENT_ID}'     AS client_id,
  '${SOURCE_SYSTEM}' AS source_system,
  e.employee_id, e.company_id, e.name, e.job_title, e.department,
  e.seniority_level, e.decision_maker_flag, e.influence_score,
  e.campaign_response_rate_pct, e.event_attendance, e.newsletter_subscription,
  e.preferred_contact_method, e.language, e.tenure_years, e.education_level,
  e.owner_rep, e.active_flag,
  c.email, c.phone,
  -- masked surrogates (PII-free) for the developer sandbox views
  REGEXP_EXTRACT(c.email, r'@(.+)\$') AS email_domain,
  TO_HEX(SHA256(COALESCE(c.email, ''))) AS contact_hash,
  CURRENT_TIMESTAMP() AS loaded_at
FROM \`${PROJECT_ID}.${DS_CURATED}.ext_employees_clean\` e
LEFT JOIN \`${PROJECT_ID}.${DS_CURATED}.ext_employee_contacts\` c USING (employee_id);

-- fct_lead: denormalised account+contact row the agents consume directly
CREATE OR REPLACE TABLE \`${PROJECT_ID}.${DS_CURATED}.fct_lead\` AS
SELECT
  ct.client_id, ct.source_system,
  ct.employee_id, ct.name, ct.job_title, ct.seniority_level,
  ct.decision_maker_flag, ct.influence_score, ct.preferred_contact_method,
  ct.language, ct.email, ct.phone, ct.email_domain, ct.contact_hash,
  co.company_id, co.industry, co.company_size, co.region,
  co.contract_status, co.conversion_rate_pct, co.total_purchases_last_year,
  co.days_since_last_purchase, co.payment_behavior, co.last_product_1,
  CURRENT_TIMESTAMP() AS loaded_at
FROM \`${PROJECT_ID}.${DS_CURATED}.dim_contact\` ct
JOIN \`${PROJECT_ID}.${DS_CURATED}.dim_company\` co USING (company_id);

SQL

echo "Curated tables built: dim_company, dim_contact, fct_lead."
