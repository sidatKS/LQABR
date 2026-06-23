-- dim_company.sql — one row per B2B account, stamped with tenancy fields.
-- Source: BigLake external table ext_companies_clean (data/seeds/b2b/companies_clean_734.csv)
-- Executed by: infra/gcp/06_curated_models.sh
-- Security: row-level policy (client_id) applied by infra/gcp/08_row_level_security.sh

CREATE OR REPLACE TABLE `${PROJECT_ID}.${DS_CURATED}.dim_company` AS
SELECT
  '${CLIENT_ID}'     AS client_id,
  '${SOURCE_SYSTEM}' AS source_system,
  company_id,
  industry,
  company_size,
  annual_revenue_m,
  marketing_spend_k,
  campaign_type,
  leads_generated,
  conversion_rate_pct,
  region,
  district,
  last_product_1,
  last_product_2,
  frequency_of_purchase,
  days_since_last_purchase,
  contract_status,
  total_purchases_last_year,
  payment_behavior,
  preferred_channel,
  sales_rep,
  CURRENT_TIMESTAMP() AS loaded_at
FROM `${PROJECT_ID}.${DS_CURATED}.ext_companies_clean`;
