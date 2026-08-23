# ngrok — public ingress for the Agent Gateway

Gives the locally-running gateway a permanent public HTTPS URL so HubSpot
webhooks can reach it. **This is the only public endpoint in the local setup** —
every gateway→agent hop is loopback on this laptop.

    HubSpot ──► https://<NGROK_DOMAIN>/hubspot/events ──► 127.0.0.1:8080 (gateway)
                                                              ├─► 127.0.0.1:8083 email
                                                              └─► 127.0.0.1:8084 voice

## Files

| File | Purpose |
|---|---|
| `ngrok.config` | domain, port, log path (env vars override) |
| `.env`         | `NGROK_AUTHTOKEN` — git-ignored, never committed |
| `run.sh`       | authenticates + starts the tunnel, logs to `logs/ngrok/ngrok.log` |

## Run

    bash ngrok/run.sh

## The two places the URL must match EXACTLY

The gateway rebuilds the signed URI from `LQABR_GATEWAY_PUBLIC_URL`. A mismatch
with what HubSpot signed changes the HMAC and every request returns **401**.

1. `agents/gateway/config/.env` -> `LQABR_GATEWAY_PUBLIC_URL=https://<NGROK_DOMAIN>`
2. HubSpot -> Settings -> Integrations -> Private Apps -> LQABR -> Webhooks -> Target URL
   -> `https://<NGROK_DOMAIN>/hubspot/events`

HubSpot's Target URL field silently reverts on first save — save it, save it
again, reload, confirm it stuck. `.env` is read once at boot: restart the
gateway after changing it.

## Checks

    curl https://<NGROK_DOMAIN>/healthz     # tunnel reaches the gateway
    http://127.0.0.1:4040                   # request inspector (replayable)

## Known errors

| Error | Cause | Fix |
|---|---|---|
| `ERR_NGROK_121` | agent older than 3.20 | `ngrok update` (WSL: apt install ngrok) |
| `ERR_NGROK_320` | the domain is reserved on a DIFFERENT account than the authtoken | reserve the name on this account (Dashboard -> Domains), or use that account's token |
| `ERR_NGROK_6024` | free-tier browser interstitial | harmless — only served to browser User-Agents; HubSpot POSTs pass through |
| `401` on every webhook | `LQABR_GATEWAY_PUBLIC_URL` != HubSpot Target URL | make them identical, restart the gateway |
