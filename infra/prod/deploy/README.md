# deploy/ — DEPLOYMENT (pre-built images → Cloud Run)

Deploys the SP-2 LQABR services for THIS environment. **Images are built and
pushed to Artifact Registry by CI** (separate pipeline) — this step never builds;
it deploys `${IMAGE_BASE}/<service>:${IMAGE_TAG}` and wires the services together.

Self-contained: every script sources the local `./config.sh`.

## Prerequisites

1. Infra setup complete for this env (`../gcp/` scripts **or** `../terraform/`).
2. CI has pushed an image per service to Artifact Registry, tagged `${IMAGE_TAG}`.
3. Secret values populated in Secret Manager (see `../gcp/setup/*-secrets-commands.md`).

## Run

```bash
source ./config.sh
bash deploy.sh        # deploy MCP → agents → Gateway, then wire URLs + allUsers
bash verify.sh        # read-only smoke checks
bash scheduler.sh     # optional heartbeat (LQABR_ENABLE_SCHEDULER=1)
```

## Order & posture (handled by deploy.sh)

1. `mcp` (internal) — deployed first; its URL is injected into every agent.
2. `ldpf`, `email`, `voice` (internal, OIDC only) — `--no-allow-unauthenticated --ingress internal`.
3. `gateway` (public) — deployed last with the agent URLs; `--allow-unauthenticated --ingress all`, then an explicit `allUsers` invoker binding (needs the project DRS allowAll exception).

Set `IMAGE_TAG` in `config.sh` to the tag CI produced (default `latest`). After
deploy, point HubSpot / Mailgun / Vapi webhooks at the printed Gateway URL.
