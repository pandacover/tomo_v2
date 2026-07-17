import tempfile
import time
import unittest

import httpx

from tomo_core.control_api import create_app
from tomo_core.cron_capability import CronCapability, issue_capability, load_or_create_key
from tomo_core.cron_models import RunOutcome
from tomo_core.cron_store import CronStore


class CronApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_rejects_cross_actor_chat_and_session_request_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp)
            now = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", now, now + 60))
            app = create_app(data_dir=tmp)
            transport = httpx.ASGITransport(app=app)
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Tomo-Owner-Id": "owner",
                "X-Tomo-Actor-Id": "other-actor",
                "X-Tomo-Destination": "telegram:chat",
                "X-Tomo-Session-Id": "telegram:actor:actor",
                "Idempotency-Key": "call-1",
            }
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/v1/cron/jobs", json={"intent": "Check status", "schedule": {"kind": "interval", "everySeconds": 60}}, headers=headers)

            self.assertEqual(response.status_code, 401)

    async def test_capability_scopes_create_and_all_job_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp)
            now = int(time.time())
            owner = issue_capability(key, CronCapability("owner-1", "actor-1", "telegram:chat-1", "telegram:actor:actor-1", now, now + 60))
            other = issue_capability(key, CronCapability("owner-2", "actor-2", "telegram:chat-2", "telegram:actor:actor-2", now, now + 60))
            owner_headers = {"Authorization": f"Bearer {owner}", "X-Tomo-Owner-Id": "owner-1", "X-Tomo-Actor-Id": "actor-1", "X-Tomo-Destination": "telegram:chat-1", "X-Tomo-Session-Id": "telegram:actor:actor-1"}
            other_headers = {"Authorization": f"Bearer {other}", "X-Tomo-Owner-Id": "owner-2", "X-Tomo-Actor-Id": "actor-2", "X-Tomo-Destination": "telegram:chat-2", "X-Tomo-Session-Id": "telegram:actor:actor-2"}
            app = create_app(data_dir=tmp, api_key="dashboard")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = {"intent": "Check status", "schedule": {"kind": "interval", "everySeconds": 60}}
                override = await client.post("/v1/cron/jobs", json={**body, "owner_id": "owner-2", "destination": "telegram:other"}, headers={**owner_headers, "Idempotency-Key": "call-1"})
                self.assertEqual(override.status_code, 422)
                created = await client.post("/v1/cron/jobs", json=body, headers={**owner_headers, "Idempotency-Key": "call-1"})
                self.assertEqual(created.status_code, 200)
                job = created.json()["job"]
                self.assertEqual(job["jobId"], (await client.post("/v1/cron/jobs", json=body, headers={**owner_headers, "Idempotency-Key": "call-1"})).json()["job"]["jobId"])
                conflict = await client.post("/v1/cron/jobs", json={**body, "intent": "Different intent"}, headers={**owner_headers, "Idempotency-Key": "call-1"})
                self.assertEqual(conflict.status_code, 409)
                stale_run = await client.post(f"/v1/cron/jobs/{job['jobId']}/run-now", json={"revision": 2}, headers={**owner_headers, "Idempotency-Key": "stale-run"})
                self.assertEqual(stale_run.status_code, 409)
                self.assertEqual((await client.get("/v1/cron/jobs", headers=other_headers)).json()["jobs"], [])
                denied = await client.get(f"/v1/cron/jobs/{job['jobId']}", headers=other_headers)
                self.assertEqual(denied.status_code, 404)
                paused = await client.post(f"/v1/cron/jobs/{job['jobId']}/pause", json={"revision": 1}, headers={**owner_headers, "Idempotency-Key": "pause-1"})
                self.assertEqual(paused.status_code, 200)
                self.assertEqual(paused.json()["job"]["status"], "paused")
                self.assertEqual((await client.get(f"/v1/cron/jobs/{job['jobId']}/history", headers=owner_headers)).status_code, 200)

    async def test_dashboard_key_cannot_substitute_for_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, api_key="dashboard")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/v1/cron/jobs", headers={"x-api-key": "dashboard"})
            self.assertEqual(response.status_code, 401)

    async def test_create_rejects_oversized_constraint_items_and_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp)
            now = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", now, now + 60))
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Tomo-Owner-Id": "owner",
                "X-Tomo-Actor-Id": "actor",
                "X-Tomo-Destination": "telegram:chat",
                "X-Tomo-Session-Id": "telegram:actor:actor",
                "Idempotency-Key": "bounds",
            }
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                base = {"intent": "Check", "schedule": {"kind": "interval", "everySeconds": 60}}
                item = await client.post("/v1/cron/jobs", json={**base, "constraints": ["x" * 1001]}, headers=headers)
                aggregate = await client.post("/v1/cron/jobs", json={**base, "constraints": ["x" * 1000] * 9}, headers=headers)
            self.assertEqual((item.status_code, aggregate.status_code), (422, 422))

    async def test_non_ascii_capability_token_is_unauthorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/v1/cron/jobs", headers={"Authorization": b"Bearer v1.\xff.signature"})
            self.assertEqual(response.status_code, 401)

    async def test_mutations_replay_the_persisted_response_and_conflicting_keys_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp); now = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", now, now + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "interval", "everySeconds": 60}}, headers={**headers, "Idempotency-Key": "create"})
                job = created.json()["job"]
                paused = await client.post(f"/v1/cron/jobs/{job['jobId']}/pause", json={"revision": 1}, headers={**headers, "Idempotency-Key": "pause"})
                self.assertEqual((await client.post(f"/v1/cron/jobs/{job['jobId']}/pause", json={"revision": 1}, headers={**headers, "Idempotency-Key": "pause"})).json(), paused.json())
                self.assertEqual((await client.post(f"/v1/cron/jobs/{job['jobId']}/pause", json={"revision": 2}, headers={**headers, "Idempotency-Key": "pause"})).status_code, 409)
                resumed = await client.post(f"/v1/cron/jobs/{job['jobId']}/resume", json={"revision": 2}, headers={**headers, "Idempotency-Key": "resume"})
                run = await client.post(f"/v1/cron/jobs/{job['jobId']}/run-now", json={"revision": 3}, headers={**headers, "Idempotency-Key": "run"})
                store = CronStore(tmp); claim = store.claim_due_run(); store.complete_run(claim.run.run_id, claim.lease_token, RunOutcome.SUCCEEDED, "done")
                self.assertEqual((await client.post(f"/v1/cron/jobs/{job['jobId']}/run-now", json={"revision": 3}, headers={**headers, "Idempotency-Key": "run"})).json(), run.json())
                self.assertEqual(resumed.status_code, 200)
