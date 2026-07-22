import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.peer_exchange import PeerExchange
from tomo_core.peer_service import PeerService
from tomo_core.sandbox_protocol import SandboxCompletedEvent, SandboxFrameEvent


class PeerServiceReliabilityTests(unittest.TestCase):
    def test_worker_loop_survives_one_run_failure(self):
        service = PeerService(None, None, None, poll_seconds=0)  # type: ignore[arg-type]
        calls = []

        def run_once():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise RuntimeError("private failure detail")
            service._stop.set()
            return False

        service.run_once = run_once  # type: ignore[method-assign]

        service._run()

        self.assertEqual(calls, [1, 2])

    def test_prune_runs_on_first_tick_and_at_most_daily(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class Exchange:
            def __init__(self):
                self.pruned = []

            def prune(self, *, now):
                self.pruned.append(now)

            def claim_notice(self, **_kwargs):
                return None

            def claim(self, **_kwargs):
                return None

        exchange = Exchange()
        times = iter((now, now + timedelta(hours=1), now + timedelta(days=1)))
        service = PeerService(exchange, None, None, clock=lambda: next(times))  # type: ignore[arg-type]

        self.assertFalse(service.run_once())
        self.assertFalse(service.run_once())
        self.assertFalse(service.run_once())

        self.assertEqual(exchange.pruned, [now, now + timedelta(days=1)])

    def test_stop_fences_frames_from_an_active_dispatch(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)
            request = exchange.submit(
                "a",
                "bobby",
                "generation",
                "call",
                "ordinary_message",
                "hello",
                now=now,
            )
            installation = type("Installation", (), {"tomo_id": "b"})()
            installations = type(
                "Installations",
                (),
                {"installation_for_tomo": lambda *_: installation},
            )()
            service = None

            class Dispatch:
                def iter_peer_events(self, *_args, **_kwargs):
                    assert service is not None
                    service._stop.set()
                    yield SandboxFrameEvent(0, 0, 0, "late")
                    yield SandboxCompletedEvent(1, {"status": "completed"})

            service = PeerService(exchange, installations, Dispatch(), clock=lambda: now)

            self.assertTrue(service.run_once())

            inspection = exchange.inspect("a", request.request_id)
            self.assertIsNone(inspection.response)

    def test_missing_installation_defers_twice_then_fails(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)
            request = exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "hello", now=now)
            times = iter((now, now + timedelta(seconds=2), now + timedelta(seconds=6)))
            service = PeerService(exchange, type("Installations", (), {"installation_for_tomo": lambda *_: None})(), None, clock=lambda: next(times))

            self.assertTrue(service.run_once())
            self.assertIsNone(exchange.claim(now=now + timedelta(seconds=1)))
            self.assertTrue(service.run_once())
            self.assertTrue(service.run_once())
            inspected = exchange.inspect("a", request.request_id)
            self.assertEqual(inspected.response.frames, ("unable to answer right now",))

    @staticmethod
    def _exchange(directory, now):
        exchange = PeerExchange(directory)
        exchange.register_handle("a", "alice")
        exchange.register_handle("b", "bobby")
        relationship = exchange.invite("a", "bobby", now=now)
        exchange.accept("b", relationship.relationship_id, now=now)
        exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
        exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)
        return exchange
