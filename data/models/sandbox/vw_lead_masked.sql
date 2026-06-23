-- vw_lead_masked.sql — sandbox view: denormalised lead row, all PII masked.
-- This is the primary table agents/scoring/ and agents/outreach/ use in dev/eval mode.
-- In production, the agent SA reads fct_lead directly (it has pii-contact fine-grained read).
-- Executed by: infra/gcp/10_authorized_views.sh (as an authorised view)
-- Access: GROUP_APPDEV + lead-agent-runtime SA (READER on sandbox_b2b dataset)
-- Reference queries: docs/example-queries.sql

CREATE OR REPLACE VIEW `${PROJECT_ID}.${DS_SANDBOX}.vw_lead_masked` AS
SELECT
  -- Tenancy
  client_id,
  source_system,
  -- Contact (masked)
  employee_id,
  CONCAT('Contact-', SUBSTR(contact_hash, 1, 8)) AS masked_name,  -- replaces name
  job_title,
  seniority_level,
  decision_maker_flag,
  influence_score,
  preferred_contact_method,
  language,
  email_domain,    -- safe surrogate
  contact_hash,    -- safe surrogate
  -- Account / firmographic (no PII)
  company_id,
  industry,
  company_size,
  region,
  contract_status,
  conversion_rate_pct,
  total_purchases_last_year,
  days_since_last_purchase,
  payment_behavior,
  last_product_1
  -- email, phone intentionally omitted
FROM `${PROJECT_ID}.${DS_CURATED}.fct_lead`;
