#!/usr/bin/env bash
# 05_create_external_tables.sh — create BigLake external tables over GCS CSVs.
# These replace the old raw_b2b BQ tables. BQ queries them with standard SQL;
# policy tags and row-level security from later steps apply transparently.
# CREATE OR REPLACE makes reruns idempotent.
set -euo pipefail
source ./config.sh

BASE_URI="gs://${BUCKET_NAME}/${GCS_RAW_PREFIX}"
CONN_ID="${PROJECT_ID}.${BIGLAKE_REGION}.${BIGLAKE_CONNECTION}"

bq --location="${REGION}" query --use_legacy_sql=false <<SQL

-- companies_clean: one row per account (clean version)
CREATE OR REPLACE EXTERNAL TABLE \`${PROJECT_ID}.${DS_CURATED}.ext_companies_clean\`
WITH CONNECTION \`${CONN_ID}\`
OPTIONS (
  format = 'CSV',
  uris = ['${BASE_URI}/companies/companies_clean_734.csv'],
  skip_leading_rows = 1,
  schema = [
    STRUCT('company_id' AS name, 'STRING' AS type),
    STRUCT('industry' AS name, 'STRING' AS type),
    STRUCT('company_size' AS name, 'STRING' AS type),
    STRUCT('annual_revenue_m' AS name, 'FLOAT64' AS type),
    STRUCT('marketing_spend_k' AS name, 'FLOAT64' AS type),
    STRUCT('campaign_type' AS name, 'STRING' AS type),
    STRUCT('leads_generated' AS name, 'INT64' AS type),
    STRUCT('conversion_rate_pct' AS name, 'FLOAT64' AS type),
    STRUCT('region' AS name, 'STRING' AS type),
    STRUCT('district' AS name, 'STRING' AS type),
    STRUCT('last_product_1' AS name, 'STRING' AS type),
    STRUCT('last_product_2' AS name, 'STRING' AS type),
    STRUCT('frequency_of_purchase' AS name, 'STRING' AS type),
    STRUCT('days_since_last_purchase' AS name, 'INT64' AS type),
    STRUCT('contract_status' AS name, 'STRING' AS type),
    STRUCT('total_purchases_last_year' AS name, 'INT64' AS type),
    STRUCT('payment_behavior' AS name, 'STRING' AS type),
    STRUCT('preferred_channel' AS name, 'STRING' AS type),
    STRUCT('sales_rep' AS name, 'STRING' AS type)
  ]
);

-- companies_noisy: same schema, noisy variant for agent evals
CREATE OR REPLACE EXTERNAL TABLE \`${PROJECT_ID}.${DS_CURATED}.ext_companies_noisy\`
WITH CONNECTION \`${CONN_ID}\`
OPTIONS (
  format = 'CSV',
  uris = ['${BASE_URI}/companies/companies_noisy_734.csv'],
  skip_leading_rows = 1,
  schema = [
    STRUCT('company_id' AS name, 'STRING' AS type),
    STRUCT('industry' AS name, 'STRING' AS type),
    STRUCT('company_size' AS name, 'STRING' AS type),
    STRUCT('annual_revenue_m' AS name, 'FLOAT64' AS type),
    STRUCT('marketing_spend_k' AS name, 'FLOAT64' AS type),
    STRUCT('campaign_type' AS name, 'STRING' AS type),
    STRUCT('leads_generated' AS name, 'INT64' AS type),
    STRUCT('conversion_rate_pct' AS name, 'FLOAT64' AS type),
    STRUCT('region' AS name, 'STRING' AS type),
    STRUCT('district' AS name, 'STRING' AS type),
    STRUCT('last_product_1' AS name, 'STRING' AS type),
    STRUCT('last_product_2' AS name, 'STRING' AS type),
    STRUCT('frequency_of_purchase' AS name, 'STRING' AS type),
    STRUCT('days_since_last_purchase' AS name, 'INT64' AS type),
    STRUCT('contract_status' AS name, 'STRING' AS type),
    STRUCT('total_purchases_last_year' AS name, 'INT64' AS type),
    STRUCT('payment_behavior' AS name, 'STRING' AS type),
    STRUCT('preferred_channel' AS name, 'STRING' AS type),
    STRUCT('sales_rep' AS name, 'STRING' AS type)
  ]
);

-- employees_clean
CREATE OR REPLACE EXTERNAL TABLE \`${PROJECT_ID}.${DS_CURATED}.ext_employees_clean\`
WITH CONNECTION \`${CONN_ID}\`
OPTIONS (
  format = 'CSV',
  uris = ['${BASE_URI}/employees/employees_clean_5234.csv'],
  skip_leading_rows = 1,
  schema = [
    STRUCT('employee_id' AS name, 'STRING' AS type),
    STRUCT('name' AS name, 'STRING' AS type),
    STRUCT('department' AS name, 'STRING' AS type),
    STRUCT('company_id' AS name, 'STRING' AS type),
    STRUCT('job_title' AS name, 'STRING' AS type),
    STRUCT('seniority_level' AS name, 'STRING' AS type),
    STRUCT('education_level' AS name, 'STRING' AS type),
    STRUCT('work_location' AS name, 'STRING' AS type),
    STRUCT('newsletter_subscription' AS name, 'STRING' AS type),
    STRUCT('campaign_response_rate_pct' AS name, 'FLOAT64' AS type),
    STRUCT('tenure_years' AS name, 'INT64' AS type),
    STRUCT('event_attendance' AS name, 'INT64' AS type),
    STRUCT('influence_score' AS name, 'INT64' AS type),
    STRUCT('decision_maker_flag' AS name, 'STRING' AS type),
    STRUCT('preferred_contact_method' AS name, 'STRING' AS type),
    STRUCT('language' AS name, 'STRING' AS type),
    STRUCT('last_contact_date' AS name, 'DATE' AS type),
    STRUCT('next_followup_date' AS name, 'DATE' AS type),
    STRUCT('owner_rep' AS name, 'STRING' AS type),
    STRUCT('active_flag' AS name, 'STRING' AS type),
    STRUCT('data_source' AS name, 'STRING' AS type)
  ]
);

-- employees_noisy
CREATE OR REPLACE EXTERNAL TABLE \`${PROJECT_ID}.${DS_CURATED}.ext_employees_noisy\`
WITH CONNECTION \`${CONN_ID}\`
OPTIONS (
  format = 'CSV',
  uris = ['${BASE_URI}/employees/employees_noisy_5234.csv'],
  skip_leading_rows = 1,
  schema = [
    STRUCT('employee_id' AS name, 'STRING' AS type),
    STRUCT('name' AS name, 'STRING' AS type),
    STRUCT('department' AS name, 'STRING' AS type),
    STRUCT('company_id' AS name, 'STRING' AS type),
    STRUCT('job_title' AS name, 'STRING' AS type),
    STRUCT('seniority_level' AS name, 'STRING' AS type),
    STRUCT('education_level' AS name, 'STRING' AS type),
    STRUCT('work_location' AS name, 'STRING' AS type),
    STRUCT('newsletter_subscription' AS name, 'STRING' AS type),
    STRUCT('campaign_response_rate_pct' AS name, 'FLOAT64' AS type),
    STRUCT('tenure_years' AS name, 'INT64' AS type),
    STRUCT('event_attendance' AS name, 'INT64' AS type),
    STRUCT('influence_score' AS name, 'INT64' AS type),
    STRUCT('decision_maker_flag' AS name, 'STRING' AS type),
    STRUCT('preferred_contact_method' AS name, 'STRING' AS type),
    STRUCT('language' AS name, 'STRING' AS type),
    STRUCT('last_contact_date' AS name, 'DATE' AS type),
    STRUCT('next_followup_date' AS name, 'DATE' AS type),
    STRUCT('owner_rep' AS name, 'STRING' AS type),
    STRUCT('active_flag' AS name, 'STRING' AS type),
    STRUCT('data_source' AS name, 'STRING' AS type)
  ]
);

-- employee_contacts: PII table — email + phone
CREATE OR REPLACE EXTERNAL TABLE \`${PROJECT_ID}.${DS_CURATED}.ext_employee_contacts\`
WITH CONNECTION \`${CONN_ID}\`
OPTIONS (
  format = 'CSV',
  uris = ['${BASE_URI}/contacts/employee_contacts_5234.csv'],
  skip_leading_rows = 1,
  schema = [
    STRUCT('employee_id' AS name, 'STRING' AS type),
    STRUCT('company_id' AS name, 'STRING' AS type),
    STRUCT('name' AS name, 'STRING' AS type),
    STRUCT('job_title' AS name, 'STRING' AS type),
    STRUCT('email' AS name, 'STRING' AS type),
    STRUCT('phone' AS name, 'STRING' AS type)
  ]
);

SQL

echo "BigLake external tables created in ${DS_CURATED}:"
echo "  ext_companies_clean, ext_companies_noisy"
echo "  ext_employees_clean, ext_employees_noisy"
echo "  ext_employee_contacts"
