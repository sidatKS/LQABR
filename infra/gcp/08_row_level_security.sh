#!/usr/bin/env bash
# 08_row_level_security.sh — multi-tenant isolation. Each client's developers
# may only read rows where client_id = their client. Swap the member/value
# pairs as you onboard more clients. This is what makes one shared project safe.
set -euo pipefail
source ./config.sh

bq --location="${REGION}" query --use_legacy_sql=false <<SQL
-- Example: app-dev group may only see this client's rows.
CREATE OR REPLACE ROW ACCESS POLICY rap_${CLIENT_ID//-/_}_contact
  ON \`${PROJECT_ID}.${DS_CURATED}.dim_contact\`
  GRANT TO ('${GROUP_APPDEV}', '${GROUP_DATAENG}', 'serviceAccount:${SA_EMAIL}')
  FILTER USING (client_id = '${CLIENT_ID}');

CREATE OR REPLACE ROW ACCESS POLICY rap_${CLIENT_ID//-/_}_lead
  ON \`${PROJECT_ID}.${DS_CURATED}.fct_lead\`
  GRANT TO ('${GROUP_APPDEV}', '${GROUP_DATAENG}', 'serviceAccount:${SA_EMAIL}')
  FILTER USING (client_id = '${CLIENT_ID}');

CREATE OR REPLACE ROW ACCESS POLICY rap_${CLIENT_ID//-/_}_company
  ON \`${PROJECT_ID}.${DS_CURATED}.dim_company\`
  GRANT TO ('${GROUP_APPDEV}', '${GROUP_DATAENG}', 'serviceAccount:${SA_EMAIL}')
  FILTER USING (client_id = '${CLIENT_ID}');
SQL

echo "Row-level security applied for client ${CLIENT_ID}."
