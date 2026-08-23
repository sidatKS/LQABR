#!/usr/bin/env python3
"""Count + list HubSpot properties for an object type. Run in WSL (needs net).
Usage:  python3 scripts/dev/hs_props.py tickets
Token is read from mcp/.env (LQABR_HUBSPOT_ACCESS_TOKEN)."""
import os, sys, json, urllib.request

obj = sys.argv[1] if len(sys.argv) > 1 else "tickets"

# load token from mcp/.env
tok = os.environ.get("LQABR_HUBSPOT_ACCESS_TOKEN", "")
if not tok:
    for line in open("mcp/.env"):
        if line.startswith("LQABR_HUBSPOT_ACCESS_TOKEN="):
            tok = line.split("=", 1)[1].strip()
if not tok:
    sys.exit("no token found in env or mcp/.env")

req = urllib.request.Request(
    f"https://api.hubapi.com/crm/v3/properties/{obj}",
    headers={"Authorization": f"Bearer {tok}"})
data = json.load(urllib.request.urlopen(req, timeout=30))
props = data.get("results", [])

hs = [p for p in props if p["name"].startswith("hs_")]
custom = [p for p in props if not p["name"].startswith("hs_")]

print(f"OBJECT: {obj}")
print(f"TOTAL properties: {len(props)}")
print(f"  system (hs_*):        {len(hs)}")
print(f"  standard + custom:    {len(custom)}")
print()
print("Non-hs_ properties (the ones you'd actually set/read):")
for p in sorted(custom, key=lambda x: x["name"]):
    opts = ""
    if p.get("options"):
        vals = [o["value"] for o in p["options"]]
        opts = "  options=[" + ", ".join(vals[:8]) + ("..." if len(vals) > 8 else "") + "]"
    print(f'  {p["name"]:28} {p["type"]:10}{opts}')
