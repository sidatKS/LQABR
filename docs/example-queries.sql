-- example_queries.sql — proof the shared source works for the two demo use cases.
-- Run as an app-dev against the SANDBOX views (no PII exposed).
-- Replace PROJECT / sandbox_b2b if you changed config.sh.

-- 1) Companies ranked by closing probability (simple transparent score;
--    your scoring agent can replace this with model logic).
SELECT
  company_id, industry, company_size, region, contract_status,
  ROUND(
      0.40 * conversion_rate_pct
    + 0.30 * LEAST(total_purchases_last_year, 50) / 50 * 100
    + 0.20 * (CASE contract_status WHEN 'Active' THEN 100
                                    WHEN 'Pending' THEN 60 ELSE 20 END)
    + 0.10 * GREATEST(0, 100 - days_since_last_purchase/3.65)
  , 1) AS closing_score
FROM `PROJECT.sandbox_b2b.vw_lead_masked`
GROUP BY company_id, industry, company_size, region, contract_status,
         conversion_rate_pct, total_purchases_last_year, days_since_last_purchase
QUALIFY ROW_NUMBER() OVER (ORDER BY closing_score DESC) <= 25
ORDER BY closing_score DESC;

-- 2) The deal-closer inside each company (highest authority + influence).
SELECT company_id, masked_name, job_title, seniority_level,
       decision_maker_flag, influence_score, preferred_contact_method, email_domain
FROM (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY company_id
      ORDER BY (decision_maker_flag = 'Yes') DESC,
               CASE seniority_level WHEN 'C-Level' THEN 5 WHEN 'Director' THEN 4
                                    WHEN 'Senior' THEN 3 WHEN 'Mid' THEN 2 ELSE 1 END DESC,
               influence_score DESC
    ) AS rn
  FROM `PROJECT.sandbox_b2b.vw_lead_masked`
)
WHERE rn = 1
ORDER BY influence_score DESC
LIMIT 25;
