"""Test doubles for the Email Agent suite."""

from lqabr_core.crm import CRMError


class FakeCRM:
    """Stands in for mcp.hubspot.crm.HubSpotCRM."""

    def __init__(self, profiles=None, leads=None):
        self.profiles = profiles or {}
        self.leads = leads or []
        self.patches = []
        self.raise_on_patch = None

    def leads_for_trigger(self, object_id, limit=25):
        return self.leads[:limit]

    def get_lead_profile(self, object_id):
        # Real HubSpot answers an unknown contact id with a 404, which
        # run_campaign relies on to fall back to the trigger-batch search.
        try:
            return self.profiles[str(object_id)]
        except KeyError:
            raise CRMError(
                f"HubSpot GET /crm/v3/objects/contacts/{object_id} failed: HTTP 404")

    def patch_object(self, object_id, properties):
        if self.raise_on_patch is not None:
            raise self.raise_on_patch
        self.patches.append((str(object_id), dict(properties)))
        return {"id": str(object_id)}

    def mark_sent(self, object_id):
        import time
        props = {"lqabr_email_status": "SENT",
                 "last_modified_email": int(time.time() * 1000)}
        return self.patch_object(object_id, props)

    def mark_campaign_complete(self, object_id):
        return self.patch_object(object_id, {"email_campaign_complete": True})


class FakeSession:
    """Stands in for mcp.hubspot.server.MCPSession."""

    def __init__(self, crm=None):
        self.crm = crm or FakeCRM()
        self.bearer_calls = 0

    def acquire_bearer(self):
        self.bearer_calls += 1
        return "test-bearer"


class FakeMailgun:
    def __init__(self, message_id="<msg-1@mg>", error=None):
        self.message_id = message_id
        self.error = error
        self.sends = []

    def send_email(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.sends.append(kwargs)
        return {"id": self.message_id}
