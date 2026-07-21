# 02 — Secret Manager

Script: `02_secret_manager.sh` · **in progress** (5/11 populated) · started 2026-07-16

Secret **values** are entered by the owner directly into Secret Manager and are
**never** pasted into chat, committed, or recorded here. This doc tracks only
which secret *names* exist and whether they hold a version.

## Method

Done per-secret (not the full interactive script) so values can be added as they
become available. Secure pattern — hidden input, value never on the command line
or in shell history:

```bash
gcloud secrets create <name> --replication-policy=automatic --project ldqfingsrv
read -r -s -p "<name>: " V && printf '%s' "$V" | \
  gcloud secrets versions add <name> --data-file=- --project ldqfingsrv; unset V; echo
```

## Progress (metadata only)

| Secret | Container | Version | Notes |
|---|---|---|---|
| `lqabr-mailgun-api-key` | ✅ | v1 enabled (2026-07-16) | Mailgun sending API key (`auth=("api", KEY)`) |
| `lqabr-mailgun-webhook-signing-key` | ✅ | v1 enabled (2026-07-16) | Mailgun HTTP webhook signing key |
| `lqabr-hubspot-access-token` | ✅ | v1 enabled (2026-07-16) | HubSpot Service Key "ZinchMarketingAgent" (`pat-na2-…`). Scopes confirmed 2026-07-16: `crm.schemas.contacts.write`, `crm.objects.contacts.read/write` present (+leads/deals/companies/appointments/lists) → step 04 ready |
| `lqabr-twilio-account-sid` | ✅ | v1 enabled (2026-07-16) | Twilio Account SID (`AC…`) — REST basic-auth username + `/Accounts/{SID}` path |
| `lqabr-twilio-auth-token` | ✅ | v1 enabled (2026-07-16) | Twilio auth token — REST basic-auth password **and** the `X-Twilio-Signature` HMAC key (no separate webhook secret, unlike Mailgun) |
| `lqabr-zoominfo-username` | — | — | ⏸ **parked** — org uses SSO (see note) |
| `lqabr-zoominfo-password` | — | — | ⏸ **parked** — org uses SSO (see note) |
| `lqabr-zoom-account-id` | — | — | pending |
| `lqabr-zoom-client-id` | — | — | pending |
| `lqabr-zoom-client-secret` | — | — | pending |
| `lqabr-zoom-webhook-secret-token` | — | — | pending |

## Related non-secret config (for step 05, NOT Secret Manager)

From the Mailgun setup (`reply.tekninjas.com`, US region `api.mailgun.net`):

- `MAILGUN_DOMAIN=reply.tekninjas.com`
- `MAILGUN_FROM="LQABR Outreach <outreach@reply.tekninjas.com>"` (confirm sender)

Mailgun **verification public key** (`pubkey-…`) was provided but is **not used**
by LQABR (email-validation API) — no secret stored for it.

From the Twilio setup — both still hold `config.sh` placeholders:

- `TWILIO_FROM_NUMBER` — the E.164 sending number. **Not yet set**; `config.sh`
  has `+15551234567`. `TwilioClient.__init__` raises
  `TwilioError: TWILIO_FROM_NUMBER is not configured` without it, so the two
  Twilio secrets alone are not enough to make the agent work.
- `LQABR_WEBHOOK_BASE_URL` — public base URL of the text_voice webhook service
  (the Cloud Run URL after `05`; an ngrok tunnel for local Twilio testing).

## ZoomInfo — parked (SSO blocker)

The ZoomInfo org is SSO-federated (`zoominfo.tekninjas.com` → SAML →
`app.zoominfo.com`). LQABR's `zoominfo_client.py` authenticates by POSTing raw
`username`/`password` to `https://api.zoominfo.com/authenticate` — an SSO login
has no ZoomInfo-native password, so that flow fails. Unblock via **either**:

- **Path A (no code change):** a dedicated ZoomInfo **API service account** that
  is API-entitled **and** exempt from SSO (own username/password) → store in the
  two secrets as-is. Request from ZoomInfo admin / CSM.
- **Path B (code change):** switch to ZoomInfo **PKI auth** (client-id + private
  key) in `agents/ingestion/src/zoominfo_client.py` → different secret shape.

## Gotcha

- `versions add` fails `NOT_FOUND` if the `create` step didn't run first — create
  the container before adding a version.

## Per-vendor command references

- Mailgun: [mailgun-secrets-commands.md](mailgun-secrets-commands.md) — create,
  set, rotate, verify commands for the two Mailgun secrets.
- HubSpot: [hubspot-secrets-commands.md](hubspot-secrets-commands.md) — create,
  set, rotate, verify commands + the token's confirmed scopes.
