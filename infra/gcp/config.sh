# ============================================================
# config.sh — single source of configuration for the whole kit
# Edit the CHANGE ME values, then: source ./config.sh
# ============================================================

# --- GCP target ---
export PROJECT_ID="your-gcp-project-id"          # CHANGE ME
export REGION="US"                                # BigQuery location: US | EU | asia-south1 ...
export TAXONOMY_REGION="us"                       # Data Catalog region (lowercase): us | eu | ...

# --- GCS data lake (BigLake architecture: files live here, never loaded into BQ raw tables) ---
export BUCKET_NAME="${PROJECT_ID}-lead-qual-lake" # CHANGE ME if you want a custom name
export BUCKET_LOCATION="US"                       # match REGION above
export GCS_RAW_PREFIX="raw/b2b"                   # gs://${BUCKET_NAME}/${GCS_RAW_PREFIX}/...

# --- BigLake connection (bridges BQ external tables → GCS) ---
export BIGLAKE_CONNECTION="lead-qual-biglake"     # connection display name
export BIGLAKE_REGION="us"                        # lowercase; matches REGION

# --- Layered BQ datasets (raw is now GCS — only 3 BQ datasets needed) ---
export DS_CLEANSED="cleansed_b2b"                  # reserved for future ETL transforms
export DS_CURATED="curated_b2b"                    # modeled source-of-truth the agents read
export DS_SANDBOX="sandbox_b2b"                    # masked authorized views devs consume
# External tables (pointing at GCS) are created in DS_CURATED as staging inputs
export DS_EXTERNAL="curated_b2b"                   # same dataset — external + curated live together

# --- Tenancy stamp applied to every curated row ---
export CLIENT_ID="demo-client-01"                 # this dataset's owning client
export SOURCE_SYSTEM="kaggle_synthetic_b2b"       # where it originated

# --- Local data location (seed CSVs in data/seeds/b2b — mirrors GCS raw/b2b/ structure) ---
export DATA_DIR="../../data/seeds/b2b"

# --- Access principals — swap group: for user: bindings if you skip Google Groups ---
# Example user binding: "user:you@gmail.com"
# Example group binding: "group:data-eng@yourco.com"
export GROUP_DATAENG="user:your-email@example.com"      # CHANGE ME — write access to curated
export GROUP_APPDEV="user:your-email@example.com"       # CHANGE ME — read masked sandbox only
export GROUP_PII_VIEWERS="user:your-email@example.com"  # CHANGE ME — may read unmasked PII

# --- Agent runtime identity (NOT a person) ---
export SA_NAME="lead-agent-runtime"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# --- Policy tag taxonomy ---
export TAXONOMY_NAME="data-governance"
export POLICY_TAG_PII="pii-contact"               # tag guarding Email/Phone/Name

echo "Config loaded for project: ${PROJECT_ID} | bucket: gs://${BUCKET_NAME} | region: ${REGION}"
