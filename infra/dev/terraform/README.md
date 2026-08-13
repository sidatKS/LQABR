# terraform/ — infra SETUP (IaC)

Full working Terraform for the LQABR **setup** layer of THIS environment —
the IaC equivalent of `../gcp/` (00–03 + IAM). It provisions: project APIs,
the runtime service account + runtime roles, the developer-group deploy roles
+ `actAs`, the Artifact Registry repo, Secret Manager containers, and Pub/Sub
topics + subscription.

It does **not** deploy Cloud Run (that's `../deploy/`), does **not** build
images (CI does), and does **not** set secret VALUES (add those out-of-band,
e.g. `gcloud secrets versions add`). Use gcloud **or** Terraform for a given
resource, not both.

## Usage

```bash
# 1) Configure remote state (edit backend.tf — bucket must exist), then:
terraform init
terraform plan  -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

`terraform.tfvars` holds this environment's values (project, SA name, group,
secret names). `backend.tf` points at this environment's state prefix — dev and
prod use separate state so there is no overlap.

## Files

| File | Purpose |
|---|---|
| `providers.tf` | provider + version pins |
| `variables.tf` | input variables + role/API/secret defaults |
| `main.tf` | all resources (APIs, SA+IAM, AR, secrets, Pub/Sub) |
| `outputs.tf` | SA email, AR repo path, secret ids, topics |
| `backend.tf` | GCS remote state (per-env prefix) |
| `terraform.tfvars` | this environment's values |

## After apply

1. Populate secret values (`../gcp/setup/*-secrets-commands.md`).
2. Run `python ../gcp/04_hubspot_properties.py` (HubSpot properties are not a
   GCP resource, so they stay a script).
3. Once CI has pushed images, deploy with `../deploy/deploy.sh`.
