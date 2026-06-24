-- fct_lead.sql — denormalised account+contact row; the primary table agents query.
-- Joins dim_contact and dim_company on company_id.
-- Includes PII (email, phone) gated behind the pii-contact policy tag.
-- The lead_scores table (E9 / agents/scoring/) will JOIN to this on employee_id.
-- Executed by: infra/gcp/06_curated_models.sh
-- Security:
--   - Inherits PII policy tag on email, phone from dim_contact
--   - row-level policy on client_id (infra/gcp/08_row_level_security.sh)

CREATE OR REPLACE TABLE `${PROJECT_ID}.${DS_CURATED}.fct_lead` AS
SELECT
  -- Tenancy
  ct.client_id,
  ct.source_system,
  -- Contact identity
  ct.employee_id,
  ct.name,                           -- PII: tagged pii-contact
  ct.job_title,
  ct.seniority_level,
  ct.decision_maker_flag,
  ct.influence_score,
  ct.preferred_contact_method,
  ct.language,
  ct.email,                          -- PII: tagged pii-contact
  ct.phone,                          -- PII: tagged pii-contact
  ct.email_domain,                   -- masked surrogate (safe)
  ct.contact_hash,                   -- masked surrogate (safe)
  -- Company / account
  co.company_id,
  co.industry,
  co.company_size,
  co.region,
  co.contract_status,
  co.conversion_rate_pct,
  co.total_purchases_last_year,
  co.days_since_last_purchase,
  co.payment_behavior,
  co.last_product_1,
  CURRENT_TIMESTAMP() AS loaded_at
FROM `${PROJECT_ID}.${DS_CURATED}.dim_contact` ct
JOIN  `${PROJECT_ID}.${DS_CURATED}.dim_company` co
  USING (company_id);
