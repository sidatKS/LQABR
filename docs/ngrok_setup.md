# ngrok setup — Agent Gateway (SP-1)

**Purpose:** give the locally-running gateway a permanent public HTTPS URL so HubSpot
webhooks can reach it.

Static domain in use: **`armed-equal-share.ngrok-free.dev`**
Gateway listens on: **`127.0.0.1:8080`**

---

## 1. Install

```powershell
winget install ngrok.ngrok
```

## 2. Refresh PATH without restarting the shell

winget updates PATH, but the shell that's already open still has the old copy.

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
ngrok version
```

## 3. Update — required

winget ships **3.3.1**. The ngrok account requires **3.20.0 or newer**, so the first
connection fails with `ERR_NGROK_121` until this runs.

```powershell
ngrok update
ngrok version        # → 3.39.10
```

## 4. Authenticate

Token comes from https://dashboard.ngrok.com → **Your Authtoken**.

```powershell
ngrok config add-authtoken <your-authtoken>
```

Written once to `%USERPROFILE%\AppData\Local\ngrok\ngrok.yml` — not needed again on this machine.

## 5. Reserve the static domain

Dashboard → **Domains** → **Create Domain**. Free tier allows one.
Ours: `armed-equal-share.ngrok-free.dev`

## 6. Start the tunnel

```powershell
ngrok http 8080 --domain=armed-equal-share.ngrok-free.dev
```

Leave this window open. This is the **only** command needed to restart the tunnel later —
steps 1–5 are one-time.

---

## The two places the URL must match exactly

The gateway rebuilds the signed URI from `LQABR_GATEWAY_PUBLIC_URL`. If it doesn't match
what HubSpot signed, the HMAC differs and every request returns **401**.

**1. `agents/gateway/config/.env`**

```
LQABR_GATEWAY_PUBLIC_URL=https://armed-equal-share.ngrok-free.dev
```

**2. HubSpot** → Settings → Integrations → Private Apps → *LQABR* → Webhooks → **Target URL**

```
https://armed-equal-share.ngrok-free.dev/hubspot/events
```

> HubSpot's Target URL field **silently reverts on the first save**. Commit it, commit it
> again, then reload the page and confirm the value stuck.

`.env` is read once at boot — **restart uvicorn** after changing it.

---

## Checks

```powershell
# tunnel is up and reaching the gateway
curl https://armed-equal-share.ngrok-free.dev/healthz
```

Request inspector (every inbound request, headers and body, replayable):

```
http://127.0.0.1:4040
```

---

## Errors we hit

| Error | Cause | Fix |
|---|---|---|
| `'ngrok' is not recognized` | PATH not refreshed in the open shell | step 2 |
| `ERR_NGROK_121` | agent 3.3.1, account minimum 3.20.0 | `ngrok update` |
| `ERR_NGROK_6024` | free-tier browser interstitial | Not a problem — only served to browser User-Agents. HubSpot's POSTs pass straight through. |
| `401` on every webhook | `LQABR_GATEWAY_PUBLIC_URL` ≠ HubSpot Target URL | make them identical, restart uvicorn |

---

## Full restart sequence

Two windows:

```powershell
# window 1 — gateway
cd agents\gateway
python -m uvicorn src.server:app --host 127.0.0.1 --port 8080

# window 2 — tunnel
ngrok http 8080 --domain=armed-equal-share.ngrok-free.dev
```

The domain is static, so nothing in `.env` or HubSpot needs changing between restarts.
