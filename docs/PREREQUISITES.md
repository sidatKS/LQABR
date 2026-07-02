# LQABR — Phase 0 Prerequisites

> **Complete every section in order before running any script in `infra/gcp/`.** Each section ends with a verification command so you can confirm the step succeeded before moving on.

---

## 1. Google Cloud Project

### 1.1 Create a GCP project (skip if you already have one)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project picker at the top → **New Project**
3. Name it (e.g. `lqabr-dev`) — GCP will assign a unique **Project ID** (e.g. `lqabr-dev-123456`)
4. Note the **Project ID** — this is what goes in `config.sh`, not the display name

### 1.2 Enable billing

BigQuery, GCS, and Data Catalog all require a billing account.

1. In the Console: **Billing → Link a billing account**
2. If you don't have one: **Manage billing accounts → Create account** and add a payment method

> Without billing enabled, `00_prereqs.sh` will fail when enabling APIs.

### 1.3 Required IAM role on your account

Your personal GCP account needs at minimum **Project Editor** (`roles/editor`) or the following granular roles:

| Role | Why |
|---|---|
| `roles/bigquery.admin` | Create datasets, external tables, views, policies |
| `roles/storage.admin` | Create and write to GCS bucket |
| `roles/iam.serviceAccountAdmin` | Create the agent runtime SA |
| `roles/iam.projectIamAdmin` | Bind roles to SA and groups |
| `roles/datacatalog.admin` | Create policy tag taxonomy |
| `roles/bigquery.connectionAdmin` | Create BigLake connection |

**Simplest for a dev project:** grant yourself `Owner` — it covers all of the above.

To check your current roles:
```bash
gcloud projects get-iam-policy YOUR_PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.members:user:YOUR_EMAIL"
```

---

## 2. Local Tooling

### 2.1 Google Cloud CLI (`gcloud` + `bq` + `gsutil`)

All three come in the same package — the **Google Cloud SDK**.

**macOS (Homebrew):**
```bash
brew install --cask google-cloud-sdk
```

**macOS / Linux (manual):**
```bash
curl https://sdk.cloud.google.com | bash
exec -l $SHELL   # restart shell to pick up PATH
```

**Windows:**
Download and run the installer from [cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install)

**Verify:**
```bash
gcloud --version   # should show Google Cloud SDK 460+
bq version         # should show BigQuery CLI
gsutil version     # should show gsutil
```

### 2.2 Python 3.9+

The three helper scripts (`apply_dataset_iam.py`, `apply_policy_tags.py`, `authorize_views.py`) require Python 3.9 or later.

**Check:**
```bash
python3 --version   # must be 3.9+
```

**Install if missing:**
- macOS: `brew install python@3.11`
- Linux: `sudo apt install python3 python3-pip` (Ubuntu/Debian)
- Windows: [python.org/downloads](https://python.org/downloads)

### 2.3 Python dependency: `google-cloud-bigquery`

```bash
pip install google-cloud-bigquery --break-system-packages
```

**Verify:**
```bash
python3 -c "from google.cloud import bigquery; print('OK')"
```

---

## 3. Authentication

You need **two separate auth contexts** — one for `gcloud`/`bq`/`gsutil`, one for the Python scripts (Application Default Credentials).

### 3.1 gcloud user auth (for CLI tools)

```bash
gcloud auth login
```

A browser window opens. Sign in with the Google account that has the IAM roles from section 1.3. When complete:

```bash
gcloud config set project YOUR_PROJECT_ID
```

**Verify:**
```bash
gcloud auth list           # your account should show as ACTIVE
gcloud config get project  # should print your project ID
```

### 3.2 Application Default Credentials (for Python scripts)

```bash
gcloud auth application-default login
```

Another browser window opens — sign in with the same account. This writes credentials to `~/.config/gcloud/application_default_credentials.json` which the Python scripts pick up automatically.

**Verify:**
```bash
gcloud auth application-default print-access-token
# prints a long token string — that means it's working
```

> **Why two logins?** `gcloud`/`bq`/`gsutil` use user auth. The Python `google-cloud-bigquery` library uses ADC. They're separate credential stores.

---

## 4. Configure `infra/gcp/config.sh`

Open `infra/gcp/config.sh` and fill in the **CHANGE ME** values. All other defaults are fine for a first run.

### 4.1 Required changes

```bash
# --- GCP target ---
export PROJECT_ID="lqabr-dev-123456"     # ← your actual Project ID from step 1.1
export REGION="US"                        # US | EU | asia-south1 | us-central1 ...
export TAXONOMY_REGION="us"              # lowercase match of REGION (us | eu | us-central1)

# --- GCS data lake ---
export BUCKET_LOCATION="US"              # match REGION
export BIGLAKE_REGION="us"              # lowercase match of REGION

# --- Access principals ---
# Use "user:email" for personal accounts, "group:email" for Google Groups
export GROUP_DATAENG="user:your-email@gmail.com"      # ← your email
export GROUP_APPDEV="user:your-email@gmail.com"       # ← your email (same for solo dev)
export GROUP_PII_VIEWERS="user:your-email@gmail.com"  # ← your email
```

### 4.2 Region quick-reference

| Your location | REGION | TAXONOMY_REGION / BIGLAKE_REGION |
|---|---|---|
| United States (any) | `US` | `us` |
| Europe (any) | `EU` | `eu` |
| Mumbai / India | `asia-south1` | `asia-south1` |
| Iowa (single-region) | `us-central1` | `us-central1` |

> `REGION` and `TAXONOMY_REGION` / `BIGLAKE_REGION` must match — mismatches cause "location mismatch" errors that are annoying to debug.

### 4.3 Load the config and verify

```bash
cd infra/gcp
source ./config.sh
```

You should see:
```
Config loaded for project: lqabr-dev-123456 | bucket: gs://lqabr-dev-123456-lead-qual-lake | region: US
```

Then spot-check a few vars:
```bash
echo $PROJECT_ID        # your project ID
echo $BUCKET_NAME       # {PROJECT_ID}-lead-qual-lake
echo $GROUP_DATAENG     # user:your-email
echo $DATA_DIR          # ../../data/seeds/b2b
```

---

## 5. Verify Source Data Files

The upload script expects exactly these 5 files in `data/seeds/b2b/`:

```bash
ls ../../data/seeds/b2b/
```

Expected output:
```
companies_clean_734.csv
companies_noisy_734.csv
employee_contacts_5234.csv
employees_clean_5234.csv
employees_noisy_5234.csv
```

> If any file is missing, Phase 0 will skip it with a WARNING (not crash), but the curated models will fail downstream. Make sure all 5 are present before proceeding.

---

## 6. Full Smoke Test

Run these in sequence from the `infra/gcp/` directory after sourcing `config.sh`. They make no changes — they just confirm your environment is ready:

```bash
# 1. GCP project is accessible
gcloud projects describe "${PROJECT_ID}" --format="value(projectId)"
# Expected: prints your project ID

# 2. Billing is enabled
gcloud beta billing projects describe "${PROJECT_ID}" --format="value(billingEnabled)"
# Expected: True

# 3. Your account has BigQuery access
bq ls --project_id="${PROJECT_ID}"
# Expected: empty list (no datasets yet) or existing datasets — NOT an auth error

# 4. GCS is accessible
gsutil ls -p "${PROJECT_ID}"
# Expected: empty list or existing buckets — NOT a permission error

# 5. Python ADC works against BigQuery
python3 -c "
from google.cloud import bigquery
c = bigquery.Client(project='${PROJECT_ID}')
print('BQ client OK — project:', c.project)
"
# Expected: BQ client OK — project: your-project-id

# 6. Seed files are in place
ls ../../data/seeds/b2b/*.csv | wc -l
# Expected: 5
```

All 6 passing? You're ready. Move to `docs/PHASE0_PLAN.md` and run the scripts.

---

## 7. Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `ERROR: (gcloud) You do not currently have an active account selected` | Not logged in | Run `gcloud auth login` |
| `AccessDeniedException: 403` from gsutil | Missing `storage.admin` role or billing not enabled | Check IAM roles and billing |
| `google.auth.exceptions.DefaultCredentialsError` from Python | ADC not set up | Run `gcloud auth application-default login` |
| `KeyError: 'PROJECT_ID'` from Python | `config.sh` not sourced in this shell session | Run `source ./config.sh` and retry |
| `Location mismatch` on BigQuery operation | `REGION` and `BIGLAKE_REGION` don't match | Make sure they're consistent (e.g. `US` and `us`) |
| `Billing account not configured` on API enablement | Billing not linked | Link a billing account in GCP Console |
| `ALREADY_EXISTS` on bucket or dataset creation | Script run before — safe to ignore | All scripts are idempotent — just continue |
| `Permission denied on taxonomy` in `07_policy_tags.sh` | `datacatalog.admin` role missing | Grant `roles/datacatalog.admin` to your account |
| `pip install` fails with "externally managed environment" | System Python on Linux | Add `--break-system-packages` flag |

---

## 8. What You'll Have After Phase 0

Once all 11 scripts complete successfully, your GCP project will have:

```
GCS bucket:   gs://{PROJECT_ID}-lead-qual-lake/raw/b2b/   (5 CSV files)

BQ datasets:
  cleansed_b2b   — reserved for future ETL
  curated_b2b    — dim_company, dim_contact, fct_lead + 5 BigLake external tables
  sandbox_b2b    — vw_company, vw_contact_masked, vw_lead_masked (masked views)

IAM:
  lead-agent-runtime SA  — reads curated_b2b + sandbox_b2b, scans GCS bucket
  Your account           — full write access to all datasets

Security:
  PII policy tag on email, phone, name columns
  Row-level security isolating client_id = 'demo-client-01'
```

The agent runtime SA is ready for the API gateway (E2) to impersonate.

---

*Next step: `docs/PHASE0_PLAN.md` → Run order*
