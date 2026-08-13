# Prerequisites — set up gcloud BEFORE anything

> ⚠️ **Do this first.** Every numbered script in `infra/gcp/` (`00`–`06`)
> shells out to `gcloud` (and `04_hubspot_properties.py` uses `python3`).
> Nothing — not even `config.sh` — will run until the SDK is installed,
> authenticated, and pointed at the target project. Complete all four steps
> below, in order, then continue to [config.md](config.md).

**Never record secret values here — names and metadata only.**

---

## TL;DR — new team member, copy-paste setup

For a teammate setting up a fresh machine against the existing `ldqfingsrv`
project. (Full explanation of each step is below.)

```bash
# 1. Install gcloud locally + put it on PATH (no sudo, no cloud changes)
curl -sSL https://sdk.cloud.google.com > /tmp/gcloud_install.sh
bash /tmp/gcloud_install.sh --disable-prompts --install-dir="$HOME"
grep -q 'google-cloud-sdk/path.bash.inc' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<'EOF'
if [ -f "$HOME/google-cloud-sdk/path.bash.inc" ]; then . "$HOME/google-cloud-sdk/path.bash.inc"; fi
EOF
exec -l "$SHELL"

# 2. Authenticate (interactive — opens a browser / device flow)
gcloud init                             # at the project prompt, SELECT existing: ldqfingsrv
gcloud auth application-default login   # ADC for SDK tooling

# 3. Point at the project
gcloud config set project ldqfingsrv
```

**Differences vs. the first-time project owner's setup:**

- A teammate **selects the existing `ldqfingsrv`** at the `gcloud init` project
  prompt — they do **not** "Create a new project."
- A teammate does **not** link billing — that's a one-time, project-level
  action already done on `ldqfingsrv`.
- They must first be **granted IAM access** on `ldqfingsrv`, or it won't appear
  in their project list. Ask the project owner for a role grant.

---

## Step 1 — Install the Google Cloud SDK (once per machine)

> **Scope:** this step is **local only** — it unpacks the SDK under
> `$HOME/google-cloud-sdk` (no `sudo`, no system-wide changes) and adds it to
> `PATH`. It does **not** authenticate, set a project, link billing, or change
> anything in the cloud. That's Steps 2–4.

User-local install under `$HOME/google-cloud-sdk` — no `sudo`/root, fully
reversible (delete one directory), same shell the `infra/gcp/` scripts run in.

```bash
# Download the official installer and run it non-interactively
curl -sSL https://sdk.cloud.google.com > /tmp/gcloud_install.sh
bash /tmp/gcloud_install.sh --disable-prompts --install-dir="$HOME"

# Put gcloud on PATH for every new shell (idempotent — safe to re-run)
grep -q 'google-cloud-sdk/path.bash.inc' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<'EOF'

# Google Cloud SDK (added for LQABR infra scripts)
if [ -f "$HOME/google-cloud-sdk/path.bash.inc" ]; then . "$HOME/google-cloud-sdk/path.bash.inc"; fi
if [ -f "$HOME/google-cloud-sdk/completion.bash.inc" ]; then . "$HOME/google-cloud-sdk/completion.bash.inc"; fi
EOF

# Reload PATH in the current shell
exec -l "$SHELL"      # or: source "$HOME/google-cloud-sdk/path.bash.inc"
```

Verify:

```bash
gcloud version            # expect: Google Cloud SDK 5xx.x.x
command -v gcloud         # expect: /home/<you>/google-cloud-sdk/bin/gcloud
```

## Step 2 — Authenticate the CLI (`gcloud init`)

Interactive — opens a browser / device-code flow. Pick (or add) your Google
account and select the target project when prompted.

```bash
gcloud init
```

## Step 3 — Application Default Credentials (ADC)

Needed by SDK-based tooling and `04_hubspot_properties.py` when it talks to
Google APIs. Also interactive.

```bash
gcloud auth application-default login
```

## Step 4 — Point at the target project

Use the `PROJECT_ID` (and region) from `infra/gcp/config.sh`, so every later
script targets the right project.

```bash
gcloud config set project <PROJECT_ID>
gcloud config set compute/region <REGION>     # optional; matches config.sh

# Confirm the active context
gcloud config list                            # account + project + region
gcloud auth list                              # active credentialed account
```

✅ Once all four pass, gcloud is ready — proceed to [config.md](config.md)
and `00_enable_apis.sh`.

---

## As-run record (this machine)

Captured from the actual setup on 2026-07-15 so the log reflects reality.

**Environment observed**

- Ubuntu 24.04.4 LTS (Noble) on WSL2, x86_64
- Pre-existing tools: `curl`, `apt`, `sudo`, `tar`, `python3` 3.12.3
- gcloud was **not** installed anywhere (WSL or Windows side) before this.

**Step 1 (install) — done.** Verification observed:

```
$ gcloud version
Google Cloud SDK 576.0.0
bq 2.1.34
bundled-python3-unix 3.14.6
core 2026.07.10
gcloud-crc32c 1.0.0
gsutil 5.37

$ command -v gcloud
/home/svk/google-cloud-sdk/bin/gcloud
```

Confirmed resolvable in a fresh login shell (`bash -lic 'command -v gcloud'`).

**Steps 2–4 (init / ADC / project) — pending.** Interactive; to be run by the
operator. Record account, project, and region here once done (names only, no
secrets).

## Deviations / gotchas

- The installer prints `Shell cwd was reset to <repo>` at the end — harmless.
- Ran with `--disable-prompts`, so usage-reporting opt-in was skipped
  (defaults to disabled) and the shell profile was **not** auto-edited — that
  is why the PATH block is appended manually.
- To uninstall: `rm -rf "$HOME/google-cloud-sdk"` and remove the SDK block
  from `~/.bashrc`.
