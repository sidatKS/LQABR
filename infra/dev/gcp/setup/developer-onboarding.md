# Developer onboarding — local gcloud access to `ldqfingsrv`

For members of the **AIDeveloper** group (`ai2d@aidefinitive.com`) to set up
their machine and operate the already-provisioned LQABR infra. Developers do
**not** provision infra — you install gcloud, authenticate with your own
`@aidefinitive.com` login, point at the project, and redeploy / observe /
run code.

## Prerequisite (one-time, done by the owner)

Your `@aidefinitive.com` account must be a member of the **`ai2d@aidefinitive.com`**
group — that group carries all the project roles. If `gcloud` later says
"permission denied" on everything, you are probably not in the group yet; ask
the project owner to add you.

## Step 1 — Install the Google Cloud SDK (local, no sudo)

```bash
curl -sSL https://sdk.cloud.google.com > /tmp/gcloud_install.sh
bash /tmp/gcloud_install.sh --disable-prompts --install-dir="$HOME"
grep -q 'google-cloud-sdk/path.bash.inc' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<'EOF'
if [ -f "$HOME/google-cloud-sdk/path.bash.inc" ]; then . "$HOME/google-cloud-sdk/path.bash.inc"; fi
if [ -f "$HOME/google-cloud-sdk/completion.bash.inc" ]; then . "$HOME/google-cloud-sdk/completion.bash.inc"; fi
EOF
exec -l "$SHELL"
gcloud version      # confirm it resolves
```

(Scope: installs under `$HOME/google-cloud-sdk` and sets PATH only — no cloud
changes. Full detail in [prerequisites.md](prerequisites.md).)

## Step 2 — Authenticate (`gcloud init`)

Interactive — opens a browser / device flow. On WSL it prints a URL: open it in
your browser, sign in with your **`@aidefinitive.com`** account, approve, paste
the code back.

```bash
gcloud init
```

At the prompts:
- Sign in with your group-member account (e.g. `aidcld@aidefinitive.com`).
- **Pick the existing project `ldqfingsrv`** from the list. Do NOT create a new
  project. (If it isn't listed, choose "Enter a project ID" and type
  `ldqfingsrv`.)

## Step 3 — Application Default Credentials (for code / SDKs)

```bash
gcloud auth application-default login
```

Your local code (e.g. `lqabr_core.secrets`) uses ADC to read Secret Manager and
call Google APIs as **you**. The group's `secretmanager.secretAccessor` role is
what lets your code read the `lqabr-*` secret values.

## Step 4 — Point at the project + verify

```bash
gcloud config set project ldqfingsrv
gcloud config list                 # account = your @aidefinitive.com, project = ldqfingsrv
gcloud auth list                   # active credentialed account

# quick access check — you should see the project and services:
gcloud run services list --region us-central1        # (empty until infra deploy 05 is run)
gcloud secrets list                                  # lists secret NAMES you can access
```

## What you can and cannot do

**Can** (via group `ai2d@`):
- Redeploy app/agent code to existing Cloud Run services (`run.developer`).
- Build images (`gcloud builds submit` — `cloudbuild.builds.editor`), push/pull
  images (`artifactregistry.writer`).
- Read secret values from your code (`secretmanager.secretAccessor`).
- Read logs/metrics; view billing/cost.

**Cannot** (owner-only, by design):
- Provision/enable infra, create service accounts, change IAM.
- Make a service public/private. `run.developer` cannot set invoker IAM, so when
  you redeploy, **do not pass `--allow-unauthenticated`** — the public webhook
  binding is set once by the owner. Redeploy with a plain
  `gcloud run deploy <service> --image ... --region us-central1` (no invoker
  flag) and the existing public/private setting is preserved.

## Notes

- **Project status:** infra is mid-provisioning. `00/01/03` (APIs, runtime SA,
  Pub/Sub) are done; secrets (`02`), HubSpot props (`04`), Cloud Run deploy
  (`05`), and Scheduler (`06`) are pending the owner. Until `05` runs there are
  no Cloud Run services to redeploy and secrets have no values yet.
- **config.sh:** the repo's `infra/gcp/config.sh` still ships the placeholder
  `PROJECT_ID`. If you run the numbered scripts locally, first set
  `PROJECT_ID=ldqfingsrv` (env or edit) or you'll target a non-existent project.
