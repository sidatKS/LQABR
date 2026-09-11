"""In-VPC MCP connectivity probe. Runs inside a Cloud Run job, stdlib only.

Walks the full streamable-http binding -- initialize, notifications/initialized,
tools/list -- because tools/call is only valid after a session exists. Calling
tools/list cold returns 500 and proves nothing about health.

NOTE: no "at" sign may appear in this file; 02_probe.sh passes it to
gcloud --args using a caret-at-caret field delimiter.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["MCP_URL"].rstrip("/")   # a trailing slash breaks the token audience
ENDPOINT = BASE + "/mcp"
PROTOCOL = "2025-06-18"
META = ("http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?audience=" + BASE)


def post(body, session=None):
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = PROTOCOL   # required after initialize
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def unwrap(raw):
    """The reply may be SSE-framed; take the data: lines when present."""
    lines = [l[6:] for l in raw.splitlines() if l.startswith("data: ")]
    return "\n".join(lines) if lines else raw


print("endpoint:", ENDPOINT, flush=True)

try:
    TOKEN = urllib.request.urlopen(urllib.request.Request(
        META, headers={"Metadata-Flavor": "Google"}), timeout=15).read().decode()
except Exception as exc:
    print("FATAL: metadata server would not mint a token:", exc)
    sys.exit(1)
print("TOKEN_LEN", len(TOKEN), flush=True)

status, headers, body = post({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": PROTOCOL, "capabilities": {},
               "clientInfo": {"name": "mcp-probe", "version": "1.0"}},
})
print("initialize:", status)
print(body[:600], flush=True)
if status == 404:
    print("VERDICT: 404 -- ingress=internal rejected this caller, or the service is absent.")
    print("         Check the job has --network/--subnet/--vpc-egress set.")
    sys.exit(4)
if status in (401, 403):
    print("VERDICT: auth failure -- SA lacks run.invoker, or the audience is wrong.")
    sys.exit(2)
if status not in (200, 202):
    print("VERDICT: initialize failed with", status)
    sys.exit(3)

session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
print("session:", session or "<none returned>", flush=True)

print("notifications/initialized:",
      post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)[0], flush=True)

status, _, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
print("tools/list:", status)
try:
    tools = json.loads(unwrap(body))["result"]["tools"]
except Exception as exc:
    print("could not parse tools/list:", exc)
    print(body[:600])
    sys.exit(5)

for tool in tools:
    print("  -", tool.get("name"))
print("VERDICT: CONNECTED --", len(tools), "tools reachable over the private mesh")
sys.exit(0)
