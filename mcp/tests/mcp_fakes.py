"""Test doubles for the central MCP suite."""


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text if text is not None else ("{}" if json_body is None else "body")

    def json(self):
        return self._json


class FakeSession:
    """Records every HTTP call and replays a queued list of responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def _next(self):
        if not self.responses:
            return FakeResponse()
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()


class RecordingObs:
    def __init__(self):
        self.audits = []
        self.processes = []

    def audit(self, **fields):
        self.audits.append(fields)

    def process(self, **fields):
        self.processes.append(fields)
