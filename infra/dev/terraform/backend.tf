# DEV remote state — separate from prod (no overlap).
# Create the bucket once (owner):
#   gsutil mb -p ldqfingsrv-dev -l us-central1 gs://ldqfingsrv-dev-tfstate
#   gsutil versioning set on gs://ldqfingsrv-dev-tfstate
terraform {
  backend "gcs" {
    bucket = "ldqfingsrv-dev-tfstate"   # CHANGE ME if you use a different bucket
    prefix = "lqabr/dev"
  }
}
