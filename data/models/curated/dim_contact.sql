-- dim_contact.sql — one row per person, joining employee data with PII contact fields.
-- Pre-computes masked surrogates (email_domain, contact_hash) so sandbox views
-- never need to touch the raw PII columns.
-- Source: ext_employees_clean LEFT JOIN ext_employee_contacts
-- Executed by: infra/gcp/06_curated_models.sh
-- Security:
--   - email / phone / name → PII policy tag (infra/gcp/07_policy_tags.sh)
--   - row-level policy on client_id (infra/gcp/08_row_level_security.sh)
-- NOTE: add consent_email / consent_sms columns here before any real outreach (E6)

CREATE OR REPLACE TABLE `${PROJECT_ID}.${DS_CURATED}.dim_contact` AS
SELECT
  '${CLIENT_ID}'     AS client_id,
  '${SOURCE_SYSTEM}' AS source_system,
  e.employee_id,
  e.company_id,
  e.name,                          -- PII: tagged pii-contact
  e.job_title,
  e.department,
  e.seniority_level,
  e.decision_maker_flag,
  e.influence_score,
  e.campaign_response_rate_pct,
  e.event_attendance,
  e.newsletter_subscription,
  e.preferred_contact_method,
  e.language,
  e.tenure_years,
  e.education_level,
  e.owner_rep,
  e.active_flag,
  c.email,                         -- PII: tagged pii-contact
  c.phone,                         -- PII: tagged pii-contact
  -- Masked surrogates — safe for sandbox views and developer use
  REGEXP_EXTRACT(c.email, r'@(.+)$') AS email_domain,
  TO_HEX(SHA256(COALESCE(c.email, ''))) AS contact_hash,
  CURRENT_TIMESTAMP() AS loaded_at
FROM `${PROJECT_ID}.${DS_CURATED}.ext_employees_clean` e
LEFT JOIN `${PROJECT_ID}.${DS_CURATED}.ext_employee_contacts` c
  USING (employee_id);
