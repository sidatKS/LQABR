"""lqabr_core.leadgen — the CSV-join lead-generation path (Lead Profile Agent).

This subpackage is deliberately ISOLATED from the 9-pointer model in
``lqabr_core.types`` / ``lqabr_core.crm``. It carries the alternative HubSpot
schema the Lead Profile Agent uses:

    Contact + Company + association, dedup on custom ``employee_id`` /
    ``company_id`` properties, email stored in the STANDARD ``email``
    property (decided 2026-08-25; the custom ``email_id`` is retired).

The two models coexist (a known, documented reconciliation item). Nothing here
is re-exported from ``lqabr_core`` top-level; import the specific module:

    from lqabr_core.leadgen.hubspot.crm import upsert_lead_profiles, get_lead_profile
    from lqabr_core.leadgen.hubspot.schema import LeadProfile
"""
