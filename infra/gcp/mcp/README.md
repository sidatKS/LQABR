# HubSpot MCP — `lqabr-dev-mcp`

The **only** place the MCP service is built and deployed from. Authority for every
value here is `docs/CloudRun_RunBook.md` (P8a + P9, 2026-08-26).

```bash
source infra/gcp/mcp/config.sh
bash infra/gcp/mcp/00_promote_image.sh   # Docker Hub -> Artifact Registry
bash infra/gcp/mcp/01_deploy.sh          # deploy, internal + on-VPC
bash infra/gcp/mcp/02_probe.sh           # prove the handshake from inside the VPC
```

## Three things that are counter-intuitive

1. **The image is promoted, not built.** `tne736/lqabr-mcp-server:latest` is upstream;
   `mcp/` in this repo did **not** build it. Cloud Run cannot pull from Docker Hub, so
   the tag+push in `00_` is what makes it deployable.
2. **The token is not passed with `--set-secrets`.** `HUBSPOT_AUTH_MODE=private_app` is
   baked into the image and it resolves the token *lazily* through its own
   `LQABR_SECRET_*` layer. Using `--set-secrets=HUBSPOT_PRIVATE_APP_TOKEN=...` is
   accepted at deploy time and then fails on the first real HubSpot write.
3. **`/app/errors` needs an in-memory volume.** The image runs as user `mcp` under a
   root-owned `/app` and writes there at tool-call time.

## Reaching it

`ingress=internal`: a curl from a laptop returns **404 even with a valid token** — Cloud
Run hides the service rather than admitting it exists. Only callers with
`--network/--subnet/--vpc-egress` reach it. `--vpc-egress=all-traffic` is proven safe
here (P8a stage 3); it does not break the private mesh.

## Local development

`bash mcp/run.sh` runs the same upstream image locally on :8091, and `mcp/reauth.sh`
refreshes ADC. Those are unrelated to this directory and remain the local path.
