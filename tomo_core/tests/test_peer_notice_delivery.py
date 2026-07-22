import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from tomo_core.peer_exchange import PeerExchange
from tomo_core.peer_service import PeerService


class PeerNoticeDeliveryTests(unittest.TestCase):
    def test_sensitive_submit_atomically_creates_one_pending_notice(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = PeerExchange(directory)
            exchange.register_handle("a", "alice")
            exchange.register_handle("b", "bobby")
            relationship = exchange.invite("a", "bobby", now=now)
            exchange.accept("b", relationship.relationship_id, now=now)
            exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
            exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)

            first = exchange.submit("a", "bobby", "generation", "call", "sensitive", "share your exact calendar details", now=now)
            duplicate = exchange.submit("a", "bobby", "generation", "call", "sensitive", "share your exact calendar details", now=now)

            self.assertEqual(first.pending_id, duplicate.pending_id)
            notice = exchange.claim_notice(now=now)
            self.assertEqual(notice.pending_id, first.pending_id)
            self.assertEqual(
                notice.text,
                "alice's tomo requests one-time permission for a sensitive request: sensitive_request - share your exact calendar details. "
                "send confirm peer request "
                + first.pending_id[:8]
                + " or cancel peer request "
                + first.pending_id[:8]
                + ".",
            )

    def test_missing_installation_defers_notice_instead_of_busy_looping(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)
            exchange.submit("a", "bobby", "generation", "call", "sensitive", "share your exact calendar details", now=now)
            missing = type("Installations", (), {"installation_for_tomo": lambda *_: None})()
            service = PeerService(exchange, missing, None, send_notice=lambda *_: None, clock=lambda: now)

            self.assertTrue(service.run_once())

            self.assertIsNone(exchange.claim_notice(now=now))

    def test_missing_availability_grant_creates_category_safe_notice(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)

            result = exchange.submit(
                "a",
                "bobby",
                "generation",
                "call",
                "availability",
                "are you free friday?",
                now=now,
            )
            notice = exchange.claim_notice(now=now)

            self.assertEqual(result.status, "confirmation_pending")
            self.assertIn("an availability request", notice.text)
            self.assertIn("availability - are you free friday?", notice.text)

    def test_notice_is_sent_once_and_callback_failure_is_unknown(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)
            exchange.submit("a", "bobby", "generation", "call", "sensitive", "share your exact calendar details", now=now)
            sent = []
            service = PeerService(exchange, self._installations(), None, send_notice=lambda installation, text: sent.append((installation.tomo_id, text)), clock=lambda: now)
            self.assertTrue(service.run_once())
            self.assertEqual(len(sent), 1)
            self.assertFalse(service.run_once())
            exchange.submit("a", "bobby", "generation-2", "call", "sensitive", "share your exact calendar details", now=now)
            failing = PeerService(exchange, self._installations(), None, send_notice=lambda *_: (_ for _ in ()).throw(RuntimeError("secret")), clock=lambda: now)
            self.assertTrue(failing.run_once())
            self.assertFalse(failing.run_once())

    def test_notice_send_is_linearized_inside_the_source_generation_guard(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = []

        @contextmanager
        def guard(owner, generation):
            events.append(("enter", owner, generation))
            try:
                yield True
            finally:
                events.append(("exit", owner, generation))

        with tempfile.TemporaryDirectory() as directory:
            exchange = PeerExchange(
                directory,
                source_generation_active=lambda *_: True,
                source_generation_guard=guard,
            )
            exchange.register_handle("a", "alice")
            exchange.register_handle("b", "bobby")
            relationship = exchange.invite("a", "bobby", now=now)
            exchange.accept("b", relationship.relationship_id, now=now)
            exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
            exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)
            exchange.submit("a", "bobby", "generation", "call", "sensitive", "share your exact calendar details", now=now)
            service = PeerService(
                exchange,
                self._installations(),
                None,
                send_notice=lambda *_: events.append(("send",)),
                clock=lambda: now,
            )

            self.assertTrue(service.run_once())
            self.assertEqual(events[0], ("enter", "a", "generation"))
            self.assertEqual(events[1], ("send",))
            self.assertEqual(events[2], ("exit", "a", "generation"))

    def test_crash_after_send_attempt_reservation_never_replays_notice(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = self._exchange(directory, now)
            exchange.submit(
                "a",
                "bobby",
                "generation",
                "call",
                "sensitive",
                "share your exact calendar details",
                now=now,
            )
            notice = exchange.claim_notice(now=now, lease_seconds=1)

            self.assertTrue(exchange.begin_notice_attempt(notice, now=now))

            self.assertIsNone(exchange.claim_notice(now=now + timedelta(seconds=2)))

    @staticmethod
    def _installations():
        return type("Installations", (), {"installation_for_tomo": lambda _, owner: type("Installation", (), {"tomo_id": owner, "chat_id": "123"})()})()

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
