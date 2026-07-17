import io
import json
import unittest
from urllib.error import HTTPError

from tomo_core.cron_tools import CronApiClient, cron_idempotency_key, cron_registry


class _Response:
    def __init__(self, payload): self.payload = payload
    def read(self, size=-1): return json.dumps(self.payload).encode()[:size if size >= 0 else None]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class CronToolsTests(unittest.TestCase):
    def test_registry_marks_mutations_serial_and_hides_authority_arguments(self):
        registry = cron_registry(CronApiClient("https://control.example", "capability", opener=lambda *_args, **_kwargs: _Response({"ok": True})), "generation-1")
        names = {schema["function"]["name"]: schema["function"]["parameters"] for schema in registry.schemas()}
        self.assertNotIn("owner_id", names["cron_create"]["properties"])
        self.assertNotIn("destination", names["cron_create"]["properties"])
        self.assertFalse(registry.resolve("cron_create").spec.read_only)
        self.assertFalse(registry.resolve("cron_create").spec.parallel_safe)
        self.assertTrue(registry.resolve("cron_list").spec.read_only)
        self.assertEqual(cron_idempotency_key("g", "create", {"b": 2, "a": 1}), cron_idempotency_key("g", "create", {"a": 1, "b": 2}))

    def test_client_returns_stable_safe_errors(self):
        def fail(*_args, **_kwargs):
            raise HTTPError("https://control.example", 401, "no", {}, io.BytesIO(b'{"detail":"secret"}'))
        client = CronApiClient("https://control.example", "capability", opener=fail)
        self.assertEqual(client.request("GET", "/v1/cron/jobs"), {"ok": False, "error": "cron_unauthorized"})
        self.assertEqual(CronApiClient("not-a-url", "capability").request("GET", "/v1/cron/jobs"), {"ok": False, "error": "cron_unavailable"})

    def test_tool_encodes_path_excludes_job_id_from_body_and_bounds_responses(self):
        captured = {}

        def open_request(request, **_kwargs):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            return _Response({"ok": True})

        registry = cron_registry(CronApiClient("https://control.example", "capability", opener=open_request), "generation-1")
        registry.resolve("cron_pause").invoke({"job_id": "job/with space", "revision": 3})
        self.assertEqual(captured["url"], "https://control.example/v1/cron/jobs/job%2Fwith%20space/pause")
        self.assertEqual(captured["body"], {"revision": 3})

        huge = CronApiClient("https://control.example", "capability", opener=lambda *_args, **_kwargs: _Response({"value": "x" * 70000}))
        self.assertEqual(huge.request("GET", "/v1/cron/jobs"), {"ok": False, "error": "cron_invalid_response"})
