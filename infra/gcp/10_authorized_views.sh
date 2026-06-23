#!/usr/bin/env bash
# 10_authorized_views.sh — what developers actually consume.
# Masked AUTHORIZED VIEWS in the sandbox dataset: they expose every useful
# column but NO raw name/email/phone (only domain + hash surrogates).
# App-devs have READER on sandbox only, never on curated, so PII never leaks.
set -euo pipefail
source ./config.sh

# 1. create the masked views in the sandbox dataset
bq --location="${REGION}" query --use_legacy_sql=false <<SQL
CREATE OR REPLACE VIEW \`${PROJECT_ID}.${DS_SANDBOX}.vw_company\` AS
SELECT * FROM \`${PROJECT_ID}.${DS_CURATED}.dim_company\`;

CREATE OR REPLACE VIEW \`${PROJECT_ID}.${DS_SANDBOX}.vw_contact_masked\` AS
SELECT
  client_id, source_system, employee_id, company_id,
  CONCAT('Contact-', SUBSTR(contact_hash, 1, 8)) AS masked_name,
  job_title, department, seniority_level, decision_maker_flag, influence_score,
  campaign_response_rate_pct, event_attendance, newsletter_subscription,
  preferred_contact_method, language, tenure_years, education_level, owner_rep,
  active_flag, email_domain, contact_hash   -- masked surrogates only
FROM \`${PROJECT_ID}.${DS_CURATED}.dim_contact\`;

CREATE OR REPLACE VIEW \`${PROJECT_ID}.${DS_SANDBOX}.vw_lead_masked\` AS
SELECT
  client_id, source_system, employee_id, company_id,
  CONCAT('Contact-', SUBSTR(contact_hash, 1, 8)) AS masked_name,
  job_title, seniority_level, decision_maker_flag, influence_score,
  preferred_contact_method, language, email_domain, contact_hash,
  industry, company_size, region, contract_status, conversion_rate_pct,
  total_purchases_last_year, days_since_last_purchase, payment_behavior, last_product_1
FROM \`${PROJECT_ID}.${DS_CURATED}.fct_lead\`;
SQL

# 2. authorize the sandbox views to read the curated dataset
#    (so app-devs querying the views need no curated access of their own)
python3 authorize_views.py

echo "Sandbox authorized views created."
