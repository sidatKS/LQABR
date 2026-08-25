# agents/summary — agent-local rules

The repo-root `CLAUDE.md` applies, with these overrides. Where the two
disagree **inside this folder**, this file wins.

## 1. The standalone contract (non-negotiable)

This agent has no dependency on anything else in the repo:

* **No `lqabr_core`.** Need something it has? Copy the piece into
  `packages/summary_core` and test it here. A copy that drifts is a smaller
  problem than a shared package that cannot be upgraded independently.
* **No `import mcp.*`.** The HubSpot MCP is reached over the network at
  runtime (`summary_core.mcp`), against a URL from the environment.
* **No shared tests, no shared infra.** Own `tests/pytest.ini`, own
  `Dockerfile`, own `infra/`. The repo's `infra/gcp/config.sh` and
  `05_deploy_agents.sh` are not edited for this agent.

`tests/test_standalone.py` enforces all of it. Do not skip or xfail it.

## 2. Nothing external is hard-coded

Tool names, HubSpot property names, the MCP URL, the model — every one is
read from the environment with a sensible default. Renaming a HubSpot
property or an MCP tool must be a config change, never a code edit. Tool
names are additionally **discovered** via `tools/list` at startup and
asserted, so a mismatch fails loudly at boot instead of silently not writing.

## 3. Never invent a summary

A model response that fails schema validation is retried once, then returned
as an explicit error. A half-parsed or fabricated summary must never reach
the CRM. Bad input is flagged with a reason — never dropped silently.

## 4. Fetching is guarded

The `url` and `api` adapters accept http/https only, refuse private,
loopback and link-local addresses unless explicitly allowlisted, cap the
body, time out, and retry 3 times with backoff on 429/5xx.

## 5. Tests are offline

No test opens a socket, reads a developer `.env`, or needs an API key. The
MCP is a fake; HTTP is mocked.

## 6. Logs never carry secrets

Log the secret's NAME and where it came from, never its value. Same for the
MCP auth token and the model key.
