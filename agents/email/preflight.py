#!/usr/bin/env python3
"""Preflight — prove the environment before a single email goes out.

Read-only. It sends nothing, writes nothing to HubSpot, and prints no secret
value (credentials appear only as a short fingerprint).

Answers, against the REAL services, the questions that unit tests cannot:

    1. Do the credentials resolve, and from where?
    2. Does HubSpot have the contact properties this agent selects and writes
       — `object_id` and `email_campaign_complete`? A wrong name is the one
       misconfiguration that used to email a different audience.
    3. Is the Mailgun domain real and the API key valid?
    4. Is the model provider's key resolvable?
    5. Is the run-state directory writable? (a run refuses to start otherwise)

Run it from the repo root, inside WSL, with the venv active:

    source .venv/bin/activate
    python agents/email/preflight.py

Exit status is 0 only if every REQUIRED check passed.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parent / "src"
for _p in (str(REPO_ROOT), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load agents/email/.env exactly the way the service does.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

# The modules under test log their own diagnostics; this script reports each
# check itself, so silence the duplicates and keep the output readable.
import logging as _logging
for _n in ("lqabr.model", "lqabr.secrets"):
    _logging.getLogger(_n).setLevel(_logging.ERROR)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = YELLOW = DIM = RESET = ""

_results: List[Tuple[str, bool, bool]] = []   # (name, passed, required)


def fingerprint(value: str) -> str:
    """Short, non-reversible id — enough to tell two credentials apart without
    ever printing one."""
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else "none"


def check(name: str, required: bool = True) -> Callable:
    def decorator(fn: Callable[[], Optional[str]]) -> Callable:
        def run() -> None:
            try:
                detail = fn() or ""
                print(f"  {GREEN}PASS{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
                _results.append((name, True, required))
            except Exception as exc:  # noqa: BLE001 - a preflight reports, never crashes
                tag = f"{RED}FAIL{RESET}" if required else f"{YELLOW}WARN{RESET}"
                print(f"  {tag}  {name}\n        {exc}")
                _results.append((name, False, required))
        return run
    return decorator


# ----------------------------------------------------------------- 1. secrets
@check("credential source")
def _source() -> str:
    from lqabr_core.secrets import secrets_source
    mode = secrets_source()
    if mode == "env":
        raise RuntimeError(
            "LQABR_SECRETS_SOURCE=env — the Secret Manager API is disabled. "
            "Use secret_manager (or auto) for anything but offline work.")
    return f"LQABR_SECRETS_SOURCE={mode}"


@check("HubSpot token resolves")
def _hubspot_secret() -> str:
    from lqabr_core.secrets import get_secret
    token = get_secret("lqabr-hubspot-access-token")
    if not token.startswith("pat-"):
        raise RuntimeError(
            "resolved, but it does not look like a HubSpot private-app token "
            "(expected a 'pat-' prefix) — is the Secret Manager version the "
            "right value?")
    return f"fingerprint {fingerprint(token)}"


@check("Mailgun API key resolves")
def _mailgun_secret() -> str:
    from lqabr_core.secrets import get_secret
    return f"fingerprint {fingerprint(get_secret('lqabr-mailgun-api-key'))}"


@check("Mailgun signing key resolves")
def _mailgun_signing() -> str:
    from lqabr_core.secrets import get_secret
    return f"fingerprint {fingerprint(get_secret('lqabr-mailgun-webhook-signing-key'))}"


@check("model provider key resolves")
def _model_key() -> str:
    from lqabr_core.model import _provider_of, ensure_provider_credentials

    model = os.environ.get("LQABR_EMAIL_MODEL", "gemini-2.0-flash")
    provider = _provider_of(model)
    populated = ensure_provider_credentials(model)
    if populated:
        return f"{model}: {populated} populated from Secret Manager"
    env_var = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GOOGLE_API_KEY"}.get(provider)
    if env_var and os.environ.get(env_var):
        return f"{model}: {env_var} already set"
    if provider == "gemini" and os.environ.get("GOOGLE_GENAI_USE_ENTERPRISE", "") in ("1", "true"):
        return f"{model}: Vertex AI via ADC — no key needed"
    raise RuntimeError(
        f"no usable credential for {model}. Every lead would be flagged "
        f"unresolved at step 6.")


# ----------------------------------------------------------------- 2. HubSpot
@check("HubSpot reachable and token accepted")
def _hubspot_live() -> str:
    import requests
    from lqabr_core.secrets import get_secret

    resp = requests.get(
        "https://api.hubapi.com/crm/v3/properties/contacts",
        headers={"Authorization": f"Bearer {get_secret('lqabr-hubspot-access-token')}"},
        timeout=30)
    if resp.status_code == 401:
        raise RuntimeError("HTTP 401 — the token in Secret Manager is not valid for this portal")
    if resp.status_code == 403:
        raise RuntimeError("HTTP 403 — token valid but missing a required CRM scope")
    resp.raise_for_status()
    _hubspot_live.properties = {p["name"] for p in resp.json().get("results", [])}
    return f"{len(_hubspot_live.properties)} contact properties visible"


@check("lead-selection property exists")
def _selection_property() -> str:
    from mcp.hubspot.schema import object_id_property

    prop = object_id_property()
    known = getattr(_hubspot_live, "properties", None)
    if known is None:
        raise RuntimeError("skipped — the HubSpot property list could not be read")
    if prop not in known:
        near = sorted(p for p in known if "id" in p.lower() or "trigger" in p.lower())[:8]
        raise RuntimeError(
            f"'{prop}' does NOT exist on contacts. The run will fail rather than "
            f"email the wrong audience — set LQABR_HUBSPOT_OBJECT_ID_PROPERTY to "
            f"the real name. Candidates: {near or 'none obvious'}")
    return f"{prop}"


@check("campaign-complete property exists")
def _complete_property() -> str:
    from mcp.hubspot.schema import campaign_complete_property

    prop = campaign_complete_property()
    known = getattr(_hubspot_live, "properties", None)
    if known is None:
        raise RuntimeError("skipped — the HubSpot property list could not be read")
    if prop not in known:
        near = sorted(p for p in known if "campaign" in p.lower() or "complete" in p.lower())[:8]
        raise RuntimeError(
            f"'{prop}' does NOT exist on contacts. Step 9's write-back would 400 "
            f"and the hand-off to voice would never fire. Candidates: "
            f"{near or 'none obvious'}")
    return f"{prop}"


@check("lqabr_email_status is writable with the values we send")
def _status_property() -> str:
    known = getattr(_hubspot_live, "properties", None)
    if known is None:
        raise RuntimeError("skipped — the HubSpot property list could not be read")
    if "lqabr_email_status" not in known:
        raise RuntimeError("lqabr_email_status does not exist on contacts")
    return "present"


# ----------------------------------------------------------------- 3. Mailgun
@check("Mailgun domain valid and key accepted")
def _mailgun_live() -> str:
    import requests
    from lqabr_core.secrets import get_secret

    domain = os.environ.get("MAILGUN_DOMAIN", "")
    if not domain:
        raise RuntimeError("MAILGUN_DOMAIN is not set — the Mailgun client raises without it")
    base = os.environ.get("MAILGUN_API_BASE", "https://api.mailgun.net/v3")
    resp = requests.get(f"{base}/domains/{domain}",
                        auth=("api", get_secret("lqabr-mailgun-api-key")), timeout=30)
    if resp.status_code == 401:
        raise RuntimeError(
            "HTTP 401 — key rejected. If this is an EU account, set "
            "MAILGUN_API_BASE=https://api.eu.mailgun.net/v3")
    if resp.status_code == 404:
        raise RuntimeError(f"domain '{domain}' not found on this Mailgun account")
    resp.raise_for_status()
    body = resp.json().get("domain", {})
    state = body.get("state", "unknown")
    if state != "active":
        raise RuntimeError(f"domain state is '{state}', not 'active' — sends will be rejected")
    return f"{domain} ({state})"


@check("click tracking is on (the campaign-complete signal)", required=False)
def _click_tracking() -> str:
    import requests
    from lqabr_core.secrets import get_secret

    domain = os.environ.get("MAILGUN_DOMAIN", "")
    base = os.environ.get("MAILGUN_API_BASE", "https://api.mailgun.net/v3")
    resp = requests.get(f"{base}/domains/{domain}/tracking",
                        auth=("api", get_secret("lqabr-mailgun-api-key")), timeout=30)
    resp.raise_for_status()
    tracking = resp.json().get("tracking", {})
    opens = tracking.get("open", {}).get("active")
    clicks = tracking.get("click", {}).get("active")
    if not opens:
        raise RuntimeError(
            "open tracking is OFF — lqabr_email_status can never reach OPENED, "
            "so no campaign would ever complete")
    if not clicks:
        raise RuntimeError("click tracking is OFF (needs the email.<domain> CNAME)")
    return "opens and clicks tracked"


# --------------------------------------------------------------- 4. run state
@check("run-state directory writable")
def _runstate() -> str:
    from runstate import RunStateStore
    return str(RunStateStore().ensure_writable())


@check("CTA url set", required=False)
def _cta() -> str:
    url = os.environ.get("LQABR_CTA_URL", "")
    if not url:
        raise RuntimeError(
            "LQABR_CTA_URL is empty — the email still sends and an OPEN still "
            "completes the campaign, but you lose the click signal")
    return url


def main() -> int:
    print(f"\n{DIM}LQABR email agent — preflight (read-only; sends nothing){RESET}\n")
    print(" credentials")
    for fn in (_source, _hubspot_secret, _mailgun_secret, _mailgun_signing, _model_key):
        fn()
    print("\n HubSpot")
    for fn in (_hubspot_live, _selection_property, _complete_property, _status_property):
        fn()
    print("\n Mailgun")
    for fn in (_mailgun_live, _click_tracking):
        fn()
    print("\n runtime")
    for fn in (_runstate, _cta):
        fn()

    failed = [n for n, ok, req in _results if not ok and req]
    warned = [n for n, ok, req in _results if not ok and not req]
    print()
    if failed:
        print(f"{RED}{len(failed)} required check(s) failed{RESET} — do not run a campaign yet:")
        for n in failed:
            print(f"    - {n}")
        return 1
    if warned:
        print(f"{YELLOW}All required checks passed, {len(warned)} warning(s).{RESET}")
    else:
        print(f"{GREEN}All checks passed.{RESET}")
    print(f"\n{DIM}Next: start the service, then run a dry-run campaign — see "
          f"agents/email/run_local.sh{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
