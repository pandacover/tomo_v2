import tempfile
import unittest
from datetime import datetime, timezone

from tomo_core.peer_exchange import PeerError, PeerExchange
from tomo_core.peer_safety import contains_unauthorized_output, contains_unsafe_content, render_peer_response
from tomo_core.peer_service import PeerService
from tomo_core.runtime import _peer_frame_grounded
from tomo_core.sandbox_protocol import SandboxCompletedEvent, SandboxFrameEvent


class _Installations:
    def installation_for_tomo(self, owner):
        return type("Installation", (), {"tomo_id": owner})()


class _Dispatch:
    def __init__(self, text):
        self.text = text

    def iter_peer_events(self, installation, turn, generation_id, session_id, is_active=None):
        yield SandboxFrameEvent(0, 0, 0, self.text)
        yield SandboxCompletedEvent(1, {"status": "completed"})


class PeerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.exchange = PeerExchange(self.directory.name)
        self.exchange.register_handle("a", "alice")
        self.exchange.register_handle("b", "bobby")
        relationship = self.exchange.invite("a", "bobby", now=self.now)
        self.exchange.accept("b", relationship.relationship_id, now=self.now)
        self.exchange.update_grant("a", relationship.relationship_id, "b", True, False, False, 0, now=self.now)
        self.exchange.update_grant("b", relationship.relationship_id, "a", False, True, False, 0, now=self.now)

    def tearDown(self):
        self.directory.cleanup()

    def test_rejects_credentials_before_persistence_without_echoing_them(self):
        secret = "api_key=super-secret-value"

        with self.assertRaisesRegex(PeerError, "^unsafe_content$") as caught:
            self.exchange.submit("a", "bobby", "generation", "call", "ordinary_message", secret, now=self.now)

        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.exchange.relationship_history("a", self.exchange.list_relationships("a")[0].relationship_id), ())

    def test_safe_discussion_of_password_is_accepted(self):
        result = self.exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "How should I choose a password?", now=self.now)

        self.assertEqual(result.status, "pending")

    def test_detector_covers_each_required_secret_family(self):
        samples = (
            "-----BEGIN " + "PRIVATE KEY-----",
            "Bearer very-secret-token-value",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturevalue",
            "sk-this-is-a-secret-value",
            "xai-this-is-a-secret-value",
            "ghp_thisisasecretvalue",
            "AKIA1234567890ABCDEF",
            "password: very-secret-value",
            "access_token=very-secret-value",
            "client_secret=very-secret-value",
        )

        self.assertTrue(all(contains_unsafe_content(sample) for sample in samples))

    def test_unsafe_worker_frame_becomes_safe_terminal_failure(self):
        secret = "Bearer very-secret-token-value"
        request = self.exchange.submit("a", "bobby", "generation", "call", "ordinary_message", "hello", now=self.now)

        PeerService(self.exchange, _Installations(), _Dispatch(secret), clock=lambda: self.now).run_once()

        inspection = self.exchange.inspect("a", request.request_id)
        self.assertEqual(inspection.status, "failed")
        self.assertEqual(inspection.response.frames, ("unable to answer right now",))
        self.assertNotIn(secret, repr(inspection))

    def test_confirmed_scope_allows_only_its_private_category(self):
        self.assertFalse(contains_unauthorized_output("calendar meeting at 3", "sensitive", "calendar_detail"))
        self.assertTrue(contains_unauthorized_output("calendar meeting at 3; email me@example.com", "sensitive", "calendar_detail"))
        self.assertFalse(contains_unauthorized_output("meet at 123 Main Street", "sensitive", "precise_location"))
        self.assertTrue(contains_unauthorized_output("meet at 123 Main Street; call +1 555 555 5555", "sensitive", "precise_location"))
        self.assertFalse(contains_unauthorized_output("yes, Tuesday works", "sensitive", "commitment_proposal"))
        self.assertTrue(contains_unauthorized_output("yes, email me@example.com", "sensitive", "commitment_proposal"))

    def test_ordinary_output_rejects_private_categories(self):
        for text in ("her therapy appointment is tomorrow", "bank balance is 12", "call the attorney", "their tax return"):
            self.assertTrue(contains_unauthorized_output(text, "ordinary_message"))

    def test_sensitive_peer_frames_require_exact_typed_json_and_render_fixed_text(self):
        self.assertEqual(
            render_peer_response("availability", '{"status":"free","window":"morning"}'),
            "Availability: free (morning).",
        )
        self.assertIsNone(render_peer_response("availability", '{"status":"free","window":"morning","note":"private"}'))
        self.assertIsNone(render_peer_response("contact_phone", "call me at +15551234567"))
        rendered = render_peer_response("commitment_proposal", '{"response":"accept","counter_time":null}')
        self.assertIn("proposal only", rendered)
        self.assertIn("no booking", rendered)

    def test_personal_typed_frame_must_match_a_projected_candidate(self):
        candidates = [{"email": "owner@example.com"}]

        self.assertTrue(
            _peer_frame_grounded(
                '{"email":"owner@example.com"}', "contact_email", candidates
            )
        )
        self.assertFalse(
            _peer_frame_grounded(
                '{"email":"invented@example.com"}', "contact_email", candidates
            )
        )
        self.assertTrue(
            _peer_frame_grounded(
                '{"status":"unknown","window":"unknown"}', "availability", []
            )
        )

    def test_typed_counter_time_survives_final_output_admission(self):
        rendered = render_peer_response(
            "commitment_proposal",
            '{"response":"counter","counter_time":"2026-08-01T18:30:00+00:00"}',
        )

        self.assertIsNotNone(rendered)
        self.assertFalse(
            contains_unauthorized_output(
                rendered, "sensitive", "commitment_proposal"
            )
        )

    def test_typed_precise_location_survives_final_output_admission(self):
        rendered = render_peer_response(
            "precise_location",
            '{"latitude":37.7749,"longitude":-122.4194}',
        )

        self.assertEqual(
            rendered, "Location coordinates: 37.774900, -122.419400."
        )
        self.assertFalse(
            contains_unauthorized_output(rendered, "sensitive", "precise_location")
        )
