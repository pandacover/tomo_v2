import tempfile
import unittest
from datetime import datetime, timezone

from tomo_core.peer_exchange import PeerExchange
from tomo_core.peer_service import PeerService
from tomo_core.sandbox_protocol import SandboxCompletedEvent, SandboxFrameEvent
from tomo_core.models import PeerTurn
from tomo_core.sessions import ConversationSession


class _Installations:
    def installation_for_tomo(self, owner): return type("Installation", (), {"tomo_id": owner})()


class _Dispatch:
    def iter_peer_events(self, installation, turn, generation_id, session_id, is_active=None):
        yield SandboxFrameEvent(0, 0, 0, "hello")
        yield SandboxCompletedEvent(1, {"status": "completed"})


class _RecordingDispatch(_Dispatch):
    def iter_peer_events(self, installation, turn, generation_id, session_id, is_active=None):
        self.turn = turn
        yield from super().iter_peer_events(installation, turn, generation_id, session_id, is_active)


class PeerServiceTests(unittest.TestCase):
    def test_ordinary_request_executes_once_and_completes(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = PeerExchange(directory)
            exchange.register_handle("a", "alice"); exchange.register_handle("b", "bobby")
            relationship = exchange.invite("a", "bobby", now=now); exchange.accept("b", relationship.relationship_id, now=now)
            exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
            exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)
            request = exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "hello", now=now)
            self.assertTrue(PeerService(exchange, _Installations(), _Dispatch(), clock=lambda: now).run_once())
            self.assertEqual(exchange.inspect("a", request.request_id).response.frames, ("hello",))

    def test_normalized_purpose_is_delivered_to_the_peer_turn(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = PeerExchange(directory)
            exchange.register_handle("a", "alice"); exchange.register_handle("b", "bobby")
            relationship = exchange.invite("a", "bobby", now=now); exchange.accept("b", relationship.relationship_id, now=now)
            exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
            exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)
            exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "hello", purpose="untrusted custom purpose", now=now)
            dispatch = _RecordingDispatch()
            PeerService(exchange, _Installations(), dispatch, clock=lambda: now).run_once()
            self.assertEqual(dispatch.turn.purpose, "question")

    def test_ordinary_response_with_private_contact_is_not_persisted(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            exchange = PeerExchange(directory)
            exchange.register_handle("a", "alice"); exchange.register_handle("b", "bobby")
            relationship = exchange.invite("a", "bobby", now=now); exchange.accept("b", relationship.relationship_id, now=now)
            exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=now)
            exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=now)
            request = exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "hello", now=now)
            claim = exchange.claim(now=now)
            self.assertFalse(exchange.complete("b", request.request_id, claim.lease_token, ("email me@example.com",), relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions, now=now))
            self.assertIsNone(exchange.inspect("a", request.request_id).response)

    def test_peer_session_append_is_idempotent_and_excluded_from_its_history(self):
        turn = PeerTurn("request", 1, "relationship", "thread", "request", "alice", "question", "ordinary_message", "hello", "2026-01-01T00:01:00+00:00")
        session = ConversationSession("peer:thread")
        self.assertTrue(session.append_peer_once(turn))
        self.assertFalse(session.append_peer_once(turn))
        self.assertEqual(session.model_history_for_burst("request"), [])
