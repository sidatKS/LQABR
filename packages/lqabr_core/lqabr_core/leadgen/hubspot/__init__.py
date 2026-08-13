"""In-process HubSpot layer for the CSV-join lead-generation path.

Two tools (schema/crm), an M2M auth utility (auth), and the failure taxonomy
(failures). Kept together and isolated from ``lqabr_core.crm`` (the 9-pointer
adapter) — see ``lqabr_core.leadgen``.
"""
