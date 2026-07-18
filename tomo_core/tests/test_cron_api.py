import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from tomo_core.control_api import create_app
from tomo_core.cron_capability import CronCapability, issue_capability, load_or_create_key
from tomo_core.cron_models import RunOutcome
from tomo_core.cron_store import CronStore


class CronApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_delay_schedule_resolves_at_control_host_time_and_is_not_due_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
            key = load_or_create_key(tmp)
            issued_at = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", issued_at, issued_at + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor", "Idempotency-Key": "delay"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp, now=lambda: now))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/v1/cron/jobs", json={"intent": "Remind me", "schedule": {"kind": "delay", "afterSeconds": 600}}, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["schedule"], {"kind": "once", "at": (now + timedelta(seconds=600)).isoformat(), "everySeconds": None, "expression": None, "timezoneName": "UTC", "startsAt": None})
            store = CronStore(tmp)
            self.assertIsNone(store.claim_due_run(now=now + timedelta(seconds=599)))
            due = store.claim_due_run(now=now + timedelta(seconds=600))
            self.assertEqual(due.run.scheduled_for, now + timedelta(seconds=600))

    async def test_delay_schedule_rejects_non_positive_and_excessive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp); now = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", now, now + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor", "Idempotency-Key": "delay-bounds"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                zero = await client.post("/v1/cron/jobs", json={"intent": "Remind me", "schedule": {"kind": "delay", "afterSeconds": 0}}, headers=headers)
                excessive = await client.post("/v1/cron/jobs", json={"intent": "Remind me", "schedule": {"kind": "delay", "afterSeconds": 31536001}}, headers=headers)
            self.assertEqual((zero.status_code, excessive.status_code), (422, 422))

    async def test_schedule_kinds_reject_cross_kind_fields_and_non_strict_delay_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(tmp); now = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", now, now + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor", "Idempotency-Key": "strict-schedules"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                cross_kind = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "once", "at": "2026-01-01T00:00:00Z", "afterSeconds": 60}}, headers=headers)
                boolean_delay = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "delay", "afterSeconds": True}}, headers=headers)
                string_delay = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "delay", "afterSeconds": "60"}}, headers=headers)
                boolean_interval = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "interval", "everySeconds": True}}, headers={**headers, "Idempotency-Key": "strict-boolean-interval"})
                string_interval = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "interval", "everySeconds": "60"}}, headers={**headers, "Idempotency-Key": "strict-string-interval"})
                infinite_delay = await client.post("/v1/cron/jobs", content=b'{"intent":"Check","schedule":{"kind":"delay","afterSeconds":NaN}}', headers={**headers, "Content-Type": "application/json"})

            self.assertEqual((cross_kind.status_code, boolean_delay.status_code, string_delay.status_code, boolean_interval.status_code, string_interval.status_code, infinite_delay.status_code), (422, 422, 422, 422, 422, 422))

    async def test_sub_minute_delay_with_seconds_is_due_at_its_exact_future_instant(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 1, 1, 12, 0, 45, tzinfo=timezone.utc)
            key = load_or_create_key(tmp)
            issued_at = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", issued_at, issued_at + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor", "Idempotency-Key": "same-minute-delay"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp, now=lambda: now))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/v1/cron/jobs", json={"intent": "Remind me", "schedule": {"kind": "delay", "afterSeconds": 10}}, headers=headers)

            due_at = now + timedelta(seconds=10)
            self.assertEqual(response.status_code, 200)
            store = CronStore(tmp)
            self.assertIsNone(store.claim_due_run(now=now))
            self.assertIsNone(store.claim_due_run(now=due_at - timedelta(microseconds=1)))
            self.assertEqual(store.claim_due_run(now=due_at).run.scheduled_for, due_at)

    async def test_update_delay_resolves_from_update_request_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = [datetime(2026, 1, 1, 12, tzinfo=timezone.utc)]
            key = load_or_create_key(tmp)
            issued_at = int(time.time())
            token = issue_capability(key, CronCapability("owner", "actor", "telegram:chat", "telegram:actor:actor", issued_at, issued_at + 60))
            headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Actor-Id": "actor", "X-Tomo-Destination": "telegram:chat", "X-Tomo-Session-Id": "telegram:actor:actor"}
            transport = httpx.ASGITransport(app=create_app(data_dir=tmp, now=lambda: clock[0]))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post("/v1/cron/jobs", json={"intent": "Check", "schedule": {"kind": "interval", "everySeconds": 3600}}, headers={**headers, "Idempotency-Key": "create-delay-update"})
                job_id = created.json()["job"]["jobId"]
                clock[0] += timedelta(minutes=5)
                updated = await client.patch(f"/v1/cron/jobs/{job_id}", json={"intent": "Remind me", "schedule": {"kind": "delay", "afterSeconds": 120}, "revision": 1}, headers={**headers, "Idempotency-Key": "update-delay"})

            self.assertEqual(updated.status_code, 200)
            due_at = clock[0] + timedelta(seconds=120)
            self.assertEqual(updated.json()["job"]["schedule"]["at"], due_at.isoformat())
            store = CronStore(tmp)
            self.assertIsNone(store.claim_due_run(now=due_at - timedelta(seconds=1)))
            self.assertEqual(store.claim_due_run(now=due_at).run.scheduled_for, due_at)

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
