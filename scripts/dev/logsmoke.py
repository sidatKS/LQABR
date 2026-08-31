"""Exercise the logging in both modes with a fully faked campaign.

No HubSpot, no Mailgun, no model call — just the log path.
Run:  python logtest.py normal|debug  <logdir>
"""
import os
import sys
import types
from pathlib import Path

MODE = sys.argv[1]
LOGDIR = sys.argv[2] if len(sys.argv) > 2 else ""
os.environ["LQABR_EMAIL_LOG_MODE"] = MODE
if LOGDIR:
    os.environ["LQABR_EMAIL_LOG_DIR"] = LOGDIR
os.environ["LQABR_EMAIL_LOG_FORMAT"] = "console"   # force the aligned view on stdout
os.environ["LQABR_EMAIL_MODEL"] = "anthropic/claude-sonnet-5"

SRC = Path(__file__).resolve().parent / "agents" / "email" / "src"
sys.path.insert(0, str(SRC))

import outreach  # noqa: E402
from mcp.hubspot.schema import ValidatedProfile  # noqa: E402

outreach.configure_logging()

PROFILE = ValidatedProfile(
    object_id="533967041217", email_id="harish@brex.com",
    first_name="Harish", last_name="Bandla", employee_id="E00009",
    job_title="President", company="Brex", industry="FINANCIAL_SERVICES",
    company_id="C0009", company_website="https://www.brex.com",
    company_about="Financial services company offering corporate cards and spend management.",
    industry_group="", phone="+1 (203) 267-9870", probability=0,
    email_status="PENDING",
    lead_context=("Brex is a rapidly scaling AI-native finance platform — growing revenue "
                  "80% year-over-year in 2025 and serving more than 35,000 businesses "
                  "across 120+ countries — whose core product decisions now hinge on "
                  "how quickly the engineering team can ship governed AI features."),
)

ctx = outreach.bind_run(PROFILE.object_id)

with outreach.span(ctx, "load_leads", step=5, model=outreach.MODEL, dry_run=False) as out:
    out.update(leads_found=1, unresolved=0, company=PROFILE.company,
               industry=PROFILE.industry, has_lead_context=PROFILE.has_lead_context,
               lead_context_chars=len(PROFILE.lead_context))

outreach.log_process(ctx, step=6, event="lead 1/1", glyph=outreach.BUSY,
                     objectId=PROFILE.object_id, state="working")

if outreach.debug_mode():
    outreach.log_process(ctx, step=5, event="profile", debug_only=True,
                         objectId=PROFILE.object_id, **outreach.fields(PROFILE))

outreach.log_audit(ctx, step=5, direction="outbound", endpoint="mcp:/get_lead_profile",
                   method="POST", status_code=200, bearer="pat-na1-fake-token",
                   duration_ms=812.0)

outreach.log_process(ctx, step=6, event="claim", objectId=PROFILE.object_id,
                     status="SENT", reason="claimed before construction")

try:
    with outreach.span(ctx, "construct", step=6, objectId=PROFILE.object_id,
                       industry=PROFILE.industry,
                       lead_context_chars=len(PROFILE.lead_context)):
        outreach.log_model(ctx, model_name=outreach.MODEL, step=6,
                           input_tokens=6191, output_tokens=1500,
                           duration_ms=43067.3, stop_reason="max_tokens",
                           prompt="SYSTEM: draft an outreach email…",
                           completion="I'll draft an outreach email for Harish at Brex…")
        raise outreach.SkillError("replied, but not as JSON")
except outreach.SkillError:
    pass

outreach.log_process(ctx, step=7, event="lead 1/1", glyph=outreach.FAIL,
                     objectId=PROFILE.object_id, outcome="unresolved", remaining=0)
outreach.log_process(ctx, step=7, event="batch_complete", glyph=outreach.OK,
                     lead_count=1, sent=0, rejected=0, unresolved=1)
