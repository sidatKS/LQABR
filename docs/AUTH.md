# HubSpot auth — where we are and where we're going

Open item from the project bible §8, raised by Mahi on 29 Jul:

> connectivity must be a separate utility / tool; the token must be generated
> M2M and refreshed at runtime — *"we can't use as an environment variable"*,
> *"never hard-code"*.

## What HubSpot actually offers

Verified against HubSpot's current authentication docs (checked 31 Jul 2026):

| grant | CRM scopes? | token life | refreshable | verdict |
| --- | --- | --- | --- | --- |
| **Private app token** | yes | static, no expiry | n/a | works, but it *is* a static token |
| **`client_credentials`** | **no** — HubSpot scopes this grant to webhooks-journal operations (`developer.webhooks_journal.read`) and issues no refresh token | 30 min | no | **unusable** — cannot mint a CRM-scoped token |
| **`refresh_token`** | yes | ~30 min | yes | **the target** |

The requirement said "short-lived + refreshed", which sounds like
client-credentials. It isn't: HubSpot's client-credentials grant cannot write
Contacts or Companies. The flow that satisfies the requirement for CRM scopes is
the **`refresh_token`** grant.

## The two modes in `auth.py`

Both sit behind one `TokenProvider` interface, so switching is a config change —
no call site in `crm.py`, and no other agent, changes.

```
HUBSPOT_AUTH_MODE=private_app     # ← TODAY (interim, by explicit decision)
HUBSPOT_AUTH_MODE=refresh_token   # ← TARGET
```

There is **no default**. An unset `HUBSPOT_AUTH_MODE` raises `AuthConfigError`
and halts the run. Previously it defaulted to `private_app`, which meant a
deploy that simply forgot the variable silently selected the mode that does not
meet the requirement. The interim mode is now something a person has to type.

`client_credentials` is explicitly rejected by `build_provider()` with an
explanation, so nobody re-discovers this the hard way.

Auth failures are **systemic**, not per-record: `AuthConfigError` and
`TokenError` are re-raised as `SystemicFailure`, which halts the run and writes
nothing to `errors/schema_mismatch.jsonl`. A missing credential is not 263 bad
leads. A 401/403 mid-run is treated differently again — it forces one token
refresh and retries, which is the normal expiry path of the `refresh_token`
grant.

### `private_app` — interim, in use today

Reads `HUBSPOT_PRIVATE_APP_TOKEN`. This is what the verified 263-lead run used.

It does **not** satisfy the M2M requirement: the token is static and long-lived.
Every acquisition emits a `auth_mode_interim` line on the process log saying so,
so the deviation is visible in the run log and not only in this file.

### `refresh_token` — target, implemented and unit-tested

```
POST https://api.hubapi.com/oauth/v1/token
  grant_type=refresh_token
  client_id, client_secret, refresh_token
→ { "access_token": ..., "expires_in": 1800 }
```

- Only `client_id`, `client_secret`, `refresh_token` are stored — in Secret
  Manager in prod, `.env` locally. The **access token is never stored**.
- Minted at runtime, cached in-process, auto-refreshed inside a configurable
  skew window (`HUBSPOT_TOKEN_REFRESH_SKEW_SECONDS`, default 120s).
- Every token-endpoint call is audit-logged: endpoint, status, timing. Never the
  secret.

## To switch on the target mode

1. Create a HubSpot **public app** in the developer account with scopes
   `crm.objects.contacts.read/write` and `crm.objects.companies.read/write`.
2. Run the authorization-code handshake **once**, out of band, to obtain a
   refresh token. This happens outside the agent and is not part of any run.
3. Put `client_id`, `client_secret`, `refresh_token` in Secret Manager.
4. Set `HUBSPOT_AUTH_MODE=refresh_token`. Nothing else changes.

Prod hardening (later): Cloud Run workload identity can hold the client secret
so it never sits in an env var at all.
