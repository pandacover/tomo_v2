import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from tomo_core.cron_capability import verify_capability
from tomo_core.peer_capability import verify_capability as verify_peer_capability
from tomo_core.peer_exchange import PeerExchange
from tomo_core.cron_models import CronJob, DeliveryAttempt, JobIntent, RunOutcome, ScheduleSpec
from tomo_core.cron_store import CronStore
from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.models import InboundEnvelope, OutboundBubble
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta, StaticProvider
from tomo_core.shared_gateway import InProcessTelegramRuntimeDispatch, SharedTelegramGateway
from tomo_core.sandbox_dispatch import SandboxDispatchError
from tomo_core.sandbox_protocol import SandboxCompletedEvent, SandboxFrameEvent, SandboxReactionEvent, SandboxStaleEvent
from tomo_core.telegram import FakeTelegramClient
from tomo_core.telegram_router import RetryableTelegramUpdateError, StaleRevisionTelegramUpdateError, TelegramUpdateRouter
from tomo_core.tool_execution import ToolExecutor
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


def private_update(text, chat_id="123", from_id="123", message_id=1):
    return {"update_id": message_id, "message": {"message_id": message_id, "from": {"id": int(from_id)}, "chat": {"id": int(chat_id), "type": "private"}, "text": text}}


def group_update(text):
    return {"message": {"message_id": 1, "from": {"id": 123}, "chat": {"id": -100, "type": "group"}, "text": text}}


class FakeRuntimeDispatch:
    def __init__(self, worker_error=None):
        self.worker_error = worker_error
        self.calls = []

    def send_setup(self, chat_id, tomo_id, reply_to_message_id):
        self.calls.append(("setup", chat_id, tomo_id, reply_to_message_id))

    def ensure_worker(self, installation):
        self.calls.append(("worker", installation))
        if self.worker_error:
            raise self.worker_error

    def send_connected(self, chat_id, reply_to_message_id):
        self.calls.append(("connected", chat_id, reply_to_message_id))

    def send_retry(self, chat_id, reply_to_message_id):
        self.calls.append(("retry", chat_id, reply_to_message_id))

    def deliver_telegram(self, installation, update_id, envelope):
        self.calls.append(("dispatch", installation, update_id, envelope))
        return [OutboundBubble("reply")]

    def iter_telegram_events(self, installation, work, is_active=None):
        self.calls.append(("iter", installation, work))
        yield SandboxFrameEvent(0, 0, 0, "first.")
        yield SandboxFrameEvent(1, 0, 1, "second.")
        yield SandboxCompletedEvent(2, {"logical_text": "first. second."})

    def iter_automation_events(self, installation, turn, generation_id, session_id, is_active=None):
        self.calls.append(("automation", installation, turn, generation_id, session_id))
        yield SandboxFrameEvent(0, 0, 0, "scheduled result")
        yield SandboxCompletedEvent(1, {"status": "completed", "logical_text": "scheduled result"})


class RecordingTypingLease:
    instances = []

    def __init__(self, send_typing, actor_id, *, is_active):
        self.send_typing = send_typing
        self.actor_id = actor_id
        self.is_active = is_active
        self.calls = []
        self.closed = False
        type(self).instances.append(self)

    def start(self):
        self.calls.append("start")

    def pause_for_first_delivery(self):
        self.calls.append("pause")

    def resume_after_failed_delivery(self):
        self.calls.append("resume")

    def close(self):
        self.calls.append("close")
        self.closed = True


class SharedGatewayTests(unittest.TestCase):
    def setUp(self):
        RecordingTypingLease.instances = []

    def test_exact_owner_language_confirms_peer_request_but_ambiguous_yes_does_not(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            exchange = PeerExchange(
                store.data_dir,
                source_generation_active=lambda _owner, _generation: True,
            )
            exchange.register_handle("sender", "alice")
            exchange.register_handle(installation.tomo_id, "bobby")
            relationship = exchange.invite("sender", "bobby")
            exchange.accept(installation.tomo_id, relationship.relationship_id)
            exchange.update_grant(
                "sender",
                relationship.relationship_id,
                installation.tomo_id,
                True,
                False,
                False,
                0,
            )
            exchange.update_grant(
                installation.tomo_id,
                relationship.relationship_id,
                "sender",
                False,
                True,
                False,
                0,
            )
            pending = exchange.submit(
                "sender",
                "bobby",
                "generation",
                "call",
                "sensitive",
                "share your exact calendar details",
            )
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(
                client=FakeTelegramClient(),
                store=store,
                dispatch=dispatch,
                peer_exchange=exchange,
            )

            self.assertTrue(
                gateway.process_update(
                    private_update("yes", chat_id="123", from_id="999", message_id=2)
                )
            )
            self.assertEqual(
                exchange.inspect("sender", pending.request_id).status,
                "confirmation_pending",
            )

            short_id = pending.pending_id.replace("-", "")[:8]
            class RouterClient(FakeTelegramClient):
                def __init__(self, updates):
                    super().__init__()
                    self.updates = updates

                def get_updates(self, **_kwargs):
                    return self.updates

            router = TelegramUpdateRouter(
                client=RouterClient([
                    private_update(
                        f"confirm peer request {short_id}",
                        chat_id="123",
                        from_id="999",
                        message_id=3,
                    )
                ]),
                store=store,
                process_update=gateway.process_update,
            )
            router.poll_once()
            self.assertTrue(router.process_next())

            self.assertEqual(
                exchange.inspect("sender", pending.request_id).status,
                "authorized",
            )
            self.assertEqual([call[0] for call in dispatch.calls], ["dispatch"])

    def test_automation_completed_partial_persists_a_partial_outcome(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")

            class PartialDispatch(FakeRuntimeDispatch):
                def iter_automation_events(self, installation, turn, generation_id, session_id, is_active=None):
                    yield SandboxFrameEvent(0, 0, 0, "partial result")
                    yield SandboxCompletedEvent(1, {"status": "completed_partial"})

            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            cron = CronStore(store.data_dir)
            cron.create(CronJob("partial", installation.tomo_id, "telegram:123", JobIntent("Check status"), ScheduleSpec.once(now)))
            claim = cron.claim_due_run(now=now)
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=PartialDispatch(), cron_store=cron)

            outcome, frames = gateway.execute_cron_claim(claim)

            self.assertEqual(outcome, RunOutcome.PARTIAL)
            self.assertEqual(frames, ("partial result",))

    def test_automation_approval_needed_does_not_expose_blocked_tool_details(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")

            class ApprovalDispatch(FakeRuntimeDispatch):
                def iter_automation_events(self, installation, turn, generation_id, session_id, is_active=None):
                    yield SandboxCompletedEvent(0, {"status": "approval_needed"})

            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            cron = CronStore(store.data_dir)
            cron.create(CronJob("approval", installation.tomo_id, "telegram:123", JobIntent("Check status"), ScheduleSpec.once(now)))
            claim = cron.claim_due_run(now=now)
            outcome, frames = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=ApprovalDispatch(), cron_store=cron).execute_cron_claim(claim)

            self.assertEqual(outcome, RunOutcome.APPROVAL_NEEDED)
            self.assertEqual(frames, ())

    def test_reclaimed_cron_claim_fences_the_active_automation_stream(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            cron = CronStore(store.data_dir)
            cron.create(CronJob("reclaimed", installation.tomo_id, "telegram:123", JobIntent("Check status"), ScheduleSpec.once(now)))
            claim = cron.claim_due_run(now=now, lease_seconds=1)
            self.assertTrue(cron.begin_run(claim.run.run_id, claim.lease_token, now=now, lease_seconds=1))
            test = self

            class ReclaimingDispatch(FakeRuntimeDispatch):
                def iter_automation_events(self, installation, turn, generation_id, session_id, is_active=None):
                    reclaimed_store = CronStore(store.data_dir)
                    test.assertEqual(reclaimed_store.recover_expired_leases(now=now.replace(second=2)), 1)
                    reclaimed = reclaimed_store.claim_due_run(now=now.replace(second=2))
                    test.assertTrue(reclaimed_store.begin_run(reclaimed.run.run_id, reclaimed.lease_token, now=now.replace(second=2)))
                    test.assertFalse(is_active())
                    yield SandboxFrameEvent(0, 0, 0, "stale")

            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=ReclaimingDispatch(), cron_store=cron)
            with self.assertRaisesRegex(RuntimeError, "cron_run_invalidated"):
                gateway.execute_cron_claim(claim)
            self.assertFalse(cron.complete_run(claim.run.run_id, claim.lease_token, RunOutcome.SUCCEEDED, "stale", now=now.replace(second=2)))

    def test_start_binds_sends_setup_ensures_worker_then_connected(self):
        with self._store() as store:
            link = store.create_install_link("user-1", "tmnvm_bot")
            dispatch = FakeRuntimeDispatch()
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertEqual([call[0] for call in dispatch.calls], ["worker"])
            self.assertEqual([message["text"] for message in client.sent_messages], ["tomo is setting up.", "tomo is connected. text me."])

    def test_start_keeps_binding_and_sends_retry_when_worker_setup_fails(self):
        with self._store() as store:
            link = store.create_install_link("user-1", "tmnvm_bot")
            dispatch = FakeRuntimeDispatch(worker_error=SandboxDispatchError("unavailable"))
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertEqual([call[0] for call in dispatch.calls], ["worker"])
            self.assertEqual([message["text"] for message in client.sent_messages], ["tomo is setting up.", "tomo is still setting up. try again in a moment."])

    def test_dm_routes_to_trusted_installation_with_sender_as_actor_and_bound_chat_as_target(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch)

            self.assertTrue(gateway.process_update(private_update("hi", chat_id="123", from_id="999", message_id=2)))

            _, routed_installation, update_id, envelope = dispatch.calls[-1]
            self.assertEqual(routed_installation, installation)
            self.assertEqual(update_id, 2)
            self.assertEqual(envelope.actor_id, "999")
            self.assertNotIn("chat_id", envelope.native_metadata)
            self.assertNotIn("delivery_chat_id", envelope.native_metadata)

    def test_groups_are_ignored(self):
        with self._store() as store:
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch)

            self.assertFalse(gateway.process_update(group_update("hi")))
            self.assertEqual(dispatch.calls, [])

    def test_runtime_dispatch_sends_railway_bubbles_only_to_the_bound_chat(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            client = FakeTelegramClient()
            instances = RuntimeInstanceRegistry(store.data_dir, lambda _: StaticProvider("hello"), client)
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=InProcessTelegramRuntimeDispatch(instances=instances),
            )

            gateway.process_update(private_update("hi", chat_id="123", from_id="999", message_id=2))

            self.assertEqual(client.typing_actor_ids, ["123"])
            self.assertEqual(client.sent_messages[-1]["actor_id"], "123")
            self.assertEqual(client.sent_messages[-1]["text"], "hello")
            session = instances.get(installation.tomo_id).sessions.load("telegram:actor:999")
            self.assertEqual(session.model_history()[-2]["content"], "hi")

    def test_runtime_dispatch_returns_reaction_to_the_gateway_for_direct_delivery(self):
        with self._store() as store:
            self._installation(store, chat_id="123", actor_id="999")
            client = FakeTelegramClient()

            class ReactionProvider:
                name = "reaction"
                supports_images_in = False
                supports_images_out = False
                supports_tool_calls = False

                def stream(self, messages, *, tools=(), actor_id=None):
                    return iter((
                        ProviderTextDelta(
                            '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"reply","confidence":"high","reaction":"👍"}\n'
                            '{"type":"frame","text":"hello"}\n'
                        ),
                        ProviderStreamCompleted("stop"),
                    ))

            instances = RuntimeInstanceRegistry(
                store.data_dir,
                lambda _: ReactionProvider(),
                client,
            )
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=InProcessTelegramRuntimeDispatch(instances=instances),
            )

            gateway.process_update(private_update("hi", chat_id="123", from_id="999", message_id=2))

            self.assertEqual(client.reactions, [{"actor_id": "123", "message_id": "2", "emoji": "👍"}])

    def test_in_process_dispatch_supports_generation_work(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            instances = RuntimeInstanceRegistry(store.data_dir, lambda _: StaticProvider("hello"), client)
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=InProcessTelegramRuntimeDispatch(instances=instances),
                pace_seconds=0,
            )

            self.assertTrue(gateway.process_update(work))
            self.assertEqual(client.sent_messages[-1]["text"], "hello")

    def test_gateway_sends_bubbles_only_to_installation_chat_and_replies_to_the_trigger_first(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            client = FakeTelegramClient()
            dispatch = FakeRuntimeDispatch()
            dispatch.deliver_telegram = Mock(return_value=[OutboundBubble("first"), OutboundBubble("second", "earlier")])
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)

            gateway.process_update(private_update("hello", chat_id="123", from_id="999", message_id=2))

            self.assertEqual([message["actor_id"] for message in client.sent_messages], ["123", "123"])
            self.assertEqual([message["text"] for message in client.sent_messages], ["first", "second"])
            self.assertEqual([message["reply_to_message_id"] for message in client.sent_messages], ["2", "earlier"])

    def test_direct_gateway_envelope_captures_reply_context_without_destination_chat_metadata(self):
        with self._store() as store:
            self._installation(store, chat_id="123", actor_id="999")
            dispatch = FakeRuntimeDispatch()
            update = private_update("hello", chat_id="123", from_id="999", message_id=2)
            update["message"]["reply_to_message"] = {"message_id": 1, "from": {"id": 999}, "chat": {"id": 123, "type": "private"}, "text": "quoted"}

            SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch).process_update(update)

            envelope = dispatch.calls[-1][3]
            self.assertEqual(envelope.reply_context.text, "quoted")
            self.assertNotIn("chat_id", envelope.native_metadata)
            self.assertNotIn("delivery_chat_id", envelope.native_metadata)

    def test_bound_dm_transient_delivery_failure_sends_one_retry_bubble_and_requests_router_retry(self):
        with self._store() as store:
            self._installation(store, chat_id="123", actor_id="999")
            client = FakeTelegramClient()
            dispatch = FakeRuntimeDispatch()
            dispatch.deliver_telegram = Mock(side_effect=SandboxDispatchError("sandbox_exec_failed"))
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)

            with self.assertRaises(RetryableTelegramUpdateError) as raised:
                gateway.process_update(private_update("hello", chat_id="123", from_id="999", message_id=2))

            self.assertEqual(raised.exception.error_code, "sandbox_exec_failed")
            self.assertEqual([message["text"] for message in client.sent_messages], ["tomo had trouble replying. try again in a moment."])
            self.assertEqual([message["reply_to_message_id"] for message in client.sent_messages], ["2"])

    def test_generation_work_retryable_error_leaves_failure_transition_to_router(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            dispatch = FakeRuntimeDispatch()
            dispatch.iter_telegram_events = Mock(side_effect=SandboxDispatchError("sandbox_exec_failed"))
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch, pace_seconds=0)

            with self.assertRaises(RetryableTelegramUpdateError):
                gateway.process_update(work)

            self.assertTrue(store.is_generation_active(work.generation_id, work.revision))

    def test_stale_sandbox_revision_raises_a_typed_retry_without_delivery(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            dispatch = FakeRuntimeDispatch()
            dispatch.iter_telegram_events = Mock(return_value=iter((SandboxStaleEvent(0, 12),)))
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch, pace_seconds=0)

            with self.assertRaises(StaleRevisionTelegramUpdateError) as raised:
                gateway.process_update(work)

            self.assertEqual(raised.exception.current_revision, 12)
            self.assertEqual(client.sent_messages, [])
            self.assertTrue(store.is_generation_active(work.generation_id, work.revision))

    def test_missing_or_rebound_installation_raises_safe_retryable_generation_failure(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            missing = self._generation_work(store, installation.tomo_id)
            rebound = type(missing)(missing.generation_id, missing.burst_id, missing.chat_id, "other-tomo", missing.revision, missing.session_id, missing.inputs, missing.visible_assistant_utterances, missing.accepted_generation_ids)
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)

            with self.assertRaises(RetryableTelegramUpdateError) as raised:
                gateway.process_update(rebound)
            self.assertEqual(raised.exception.error_code, "installation_rebound")
            db = store._connect()
            try:
                db.execute("delete from telegram_installations where chat_id = ?", ("123",))
                db.commit()
            finally:
                db.close()
            with self.assertRaises(RetryableTelegramUpdateError) as raised:
                gateway.process_update(missing)
            self.assertEqual(raised.exception.error_code, "installation_missing")

    def test_generation_work_streams_each_utterance_through_delivery_fence(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch, pace_seconds=0)

            self.assertTrue(gateway.process_update(work))

            self.assertEqual([call[0] for call in dispatch.calls], ["iter"])
            self.assertEqual([message["actor_id"] for message in client.sent_messages], ["123", "123"])
            self.assertEqual([message["text"] for message in client.sent_messages], ["first.", "second."])
            self.assertEqual([message["reply_to_message_id"] for message in client.sent_messages], ["2", None])
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))
            self.assertFalse(store.reserve_delivery(work.generation_id, work.revision, 2, 0, 2, "late.", None))

    def test_generation_typing_lease_stops_after_first_successful_bubble_and_never_restarts(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            gateway = SharedTelegramGateway(
                client=FakeTelegramClient(),
                store=store,
                dispatch=FakeRuntimeDispatch(),
                pace_seconds=0,
                typing_lease_factory=RecordingTypingLease,
            )

            gateway.process_update(work)

            lease = RecordingTypingLease.instances[0]
            self.assertEqual(lease.actor_id, "123")
            self.assertEqual(lease.calls, ["start", "pause", "close", "close"])

    def test_generation_marks_failed_telegram_send_unknown_without_retrying(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            original_send = client.send_message
            attempts = 0

            def fail_first(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("send failed")
                return original_send(*args, **kwargs)

            client.send_message = fail_first
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=FakeRuntimeDispatch(),
                pace_seconds=0,
                typing_lease_factory=RecordingTypingLease,
            )

            self.assertTrue(gateway.process_update(work))

            self.assertEqual(store.delivery_status(work.generation_id, 0), "unknown")
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))
            self.assertEqual(RecordingTypingLease.instances[0].calls, ["start", "pause", "resume", "pause", "close", "close"])

    def test_generation_work_emits_first_delivery_without_message_content(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            with patch("tomo_core.shared_gateway.latency_trace.emit") as emit, patch("tomo_core.shared_gateway.time.time", return_value=3.0):
                SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0).process_update(work)

            phases = [call.args[1] for call in emit.call_args_list]
            self.assertEqual(phases, ["telegram_origin_to_worker_start", "telegram_queue_wait", "telegram_first_delivery", "telegram_first_delivery"])
            self.assertEqual(emit.call_args_list[2].args[0], work.burst_id)
            self.assertNotIn("first.", repr(emit.call_args_list))

    def test_later_bubble_failure_is_not_labeled_first_delivery_or_retried(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            send_message = client.send_message

            def fail_second(*args, **kwargs):
                if client.sent_messages:
                    raise RuntimeError("later send failed")
                return send_message(*args, **kwargs)

            client.send_message = fail_second
            with patch("tomo_core.shared_gateway.latency_trace.emit") as emit:
                self.assertTrue(
                    SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0).process_update(work)
                )

            self.assertFalse(any(call.kwargs.get("outcome") == "error" for call in emit.call_args_list))
            self.assertEqual(store.delivery_status(work.generation_id, 1), "unknown")
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))

    def test_first_bubble_failure_is_labeled_first_delivery_and_not_retried(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            client.send_message = Mock(side_effect=RuntimeError("send failed"))

            with patch("tomo_core.shared_gateway.latency_trace.emit") as emit:
                self.assertTrue(
                    SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0).process_update(work)
                )

            first_delivery_errors = [call for call in emit.call_args_list if call.args[1] == "telegram_first_delivery" and call.kwargs.get("outcome") == "error"]
            self.assertEqual(len(first_delivery_errors), 1)
            self.assertEqual(store.delivery_status(work.generation_id, 0), "unknown")
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))

    def test_generation_work_delivers_one_reaction_before_frames_and_ignores_replay(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()

            class ReactionDispatch(FakeRuntimeDispatch):
                def iter_telegram_events(self, installation, work, is_active=None):
                    yield SandboxReactionEvent(0, installation.tomo_id, installation.actor_id, installation.chat_id, "2", work.generation_id, work.revision, "👍")
                    yield SandboxReactionEvent(1, installation.tomo_id, installation.actor_id, installation.chat_id, "2", work.generation_id, work.revision, "👍")
                    yield SandboxFrameEvent(2, 0, 0, "first.")
                    yield SandboxCompletedEvent(3, {"logical_text": "first."})

            gateway = SharedTelegramGateway(client=client, store=store, dispatch=ReactionDispatch(), pace_seconds=0)
            self.assertTrue(gateway.process_update(work))

            self.assertEqual(client.reactions, [{"actor_id": "123", "message_id": "2", "emoji": "👍"}])
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "2")
            db = store._connect()
            try:
                row = db.execute(
                    "select status from telegram_reaction_deliveries where generation_id = ? and revision = ? and target_message_id = ?",
                    (work.generation_id, work.revision, "2"),
                ).fetchone()
            finally:
                db.close()
            self.assertEqual(row["status"], "sent")

    def test_generation_work_rejects_forged_reaction_bindings(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()

            class ForgedReactionDispatch(FakeRuntimeDispatch):
                def iter_telegram_events(self, installation, work, is_active=None):
                    for field, value in (("owner", "other"), ("actor", "other"), ("chat", "other"), ("target", "1"), ("generation", "other"), ("revision", work.revision + 1)):
                        values = [installation.tomo_id, installation.actor_id, installation.chat_id, "2", work.generation_id, work.revision]
                        values[("owner", "actor", "chat", "target", "generation", "revision").index(field)] = value
                        yield SandboxReactionEvent(0, *values, "👍")
                    yield SandboxFrameEvent(1, 0, 0, "first.")
                    yield SandboxCompletedEvent(2, {"logical_text": "first."})

            self.assertTrue(SharedTelegramGateway(client=client, store=store, dispatch=ForgedReactionDispatch(), pace_seconds=0).process_update(work))
            self.assertEqual(client.reactions, [])
            self.assertEqual(client.sent_messages[0]["text"], "first.")

    def test_generation_work_sends_event_zero_before_event_one_is_generated(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()

            class InspectingDispatch(FakeRuntimeDispatch):
                def iter_telegram_events(self, installation, work, is_active=None):
                    yield SandboxFrameEvent(0, 0, 0, "first.")
                    self.calls.append(("after-first", len(client.sent_messages)))
                    yield SandboxFrameEvent(1, 0, 1, "second.")
                    yield SandboxCompletedEvent(2, {"logical_text": "first. second."})

            dispatch = InspectingDispatch()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch, pace_seconds=0)

            gateway.process_update(work)

            self.assertEqual(dispatch.calls, [("after-first", 1)])

    def test_generation_work_suppresses_stale_generation_before_send(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            store.fail_generation(work.generation_id, "superseded")
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)

            self.assertTrue(gateway.process_update(work))

            self.assertEqual(client.sent_messages, [])
            self.assertIsNone(store.delivery_status(work.generation_id, 0))

    def test_generation_work_marks_reservation_suppressed_if_revision_turns_stale_after_reserve(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            original_reserve = store.reserve_delivery

            def reserve_then_supersede(*args, **kwargs):
                reserved = original_reserve(*args, **kwargs)
                store.fail_generation(work.generation_id, "superseded")
                return reserved

            store.reserve_delivery = reserve_then_supersede
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)

            self.assertTrue(gateway.process_update(work))
            self.assertEqual(client.sent_messages, [])
            self.assertEqual(store.delivery_status(work.generation_id, 0), "suppressed")
            replacement = store.claim_next_work(now=time.time() + 1)
            self.assertEqual(replacement.visible_assistant_utterances, ())

    def test_generation_work_does_not_complete_superseded_generation_after_send_race(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            original_mark_sent = store.mark_delivery_sent

            def mark_sent_after_supersede(*args, **kwargs):
                store.fail_generation(work.generation_id, "superseded")
                return original_mark_sent(*args, **kwargs)

            store.mark_delivery_sent = mark_sent_after_supersede
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)

            self.assertTrue(gateway.process_update(work))

            self.assertEqual([message["text"] for message in client.sent_messages], ["first."])
            self.assertFalse(store.complete_generation(work.generation_id, work.revision))
            replacement = store.claim_next_work(now=time.time() + 1)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.visible_assistant_utterances, ("first.",))

    def test_generation_work_marks_sent_frame_unknown_when_acknowledgement_fails(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            original_mark_sent = store.mark_delivery_sent

            def fail_acknowledgement(*args, **kwargs):
                return False

            store.mark_delivery_sent = fail_acknowledgement
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)

            with patch("tomo_core.shared_gateway.latency_trace.emit") as emit:
                self.assertTrue(gateway.process_update(work))

            self.assertEqual([message["text"] for message in client.sent_messages], ["first.", "second."])
            first_delivery_outcomes = [call.kwargs.get("outcome") for call in emit.call_args_list if call.args[1] == "telegram_first_delivery"]
            self.assertEqual(first_delivery_outcomes, ["send_complete", "origin_to_delivery"])
            self.assertEqual(store.delivery_status(work.generation_id, 0), "unknown")
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))
            store.mark_delivery_sent = original_mark_sent

    def test_router_does_not_retry_generation_when_delivery_acknowledgement_is_uncertain(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            store.enqueue_update(1, "123", '{"update_id":1}', now=1.0, update_kind="message", message_id="1", tomo_id=installation.tomo_id)
            store.enqueue_update(2, "123", '{"update_id":2}', now=1.1, update_kind="message", message_id="2", tomo_id=installation.tomo_id)
            original_mark_sent = store.mark_delivery_sent
            store.mark_delivery_sent = lambda *_args, **_kwargs: False
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0)
            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=gateway.process_update)

            self.assertTrue(router.process_next(now=2))

            db = store._connect()
            try:
                row = db.execute("select status, error_code from telegram_generations order by created_at").fetchone()
            finally:
                db.close()
            self.assertEqual((row["status"], row["error_code"]), ("completed", None))
            self.assertIsNone(store.claim_next_work(now=3))
            store.mark_delivery_sent = original_mark_sent

    def test_in_process_dispatch_passes_active_predicate_to_runtime(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            runtime = Mock()
            runtime.handle_telegram_burst_iter.return_value = iter(())
            instances = Mock()
            instances.get.return_value = runtime

            list(InProcessTelegramRuntimeDispatch(instances).iter_telegram_events(installation, work, is_active=lambda: False))

            self.assertIs(runtime.handle_telegram_burst_iter.call_args.kwargs["is_active"](), False)

    def test_in_process_interactive_turn_gets_fresh_owner_scoped_cron_tools_then_restores_registry(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            base_registry = ToolRegistry()
            runtime = Mock()
            runtime.provider.supports_tool_calls = True
            runtime.tool_registry = base_registry
            runtime.conversation.tool_registry = base_registry
            runtime.conversation.tool_executor = ToolExecutor(base_registry)
            visible_names = []

            def handle(_burst, **_kwargs):
                visible_names.extend(item["function"]["name"] for item in runtime.conversation.tool_registry.schemas())
                return iter(())

            runtime.handle_telegram_burst_iter.side_effect = handle
            instances = Mock()
            instances.get.return_value = runtime
            key = b"k" * 32
            captured = {}

            def fake_registry(client, generation_id):
                captured.update(capability=client.capability, generation_id=generation_id, context=(client.owner_id, client.actor_id, client.destination, client.session_id))
                tool = BoundTool(ToolSpec("cron_list", "list jobs", {"type": "object", "properties": {}}), lambda _arguments: ())
                return ToolRegistry((tool,))

            dispatch = InProcessTelegramRuntimeDispatch(instances, "http://127.0.0.1:8787", key, clock=lambda: 100.0)
            with patch("tomo_core.shared_gateway.cron_registry", side_effect=fake_registry):
                list(dispatch.iter_telegram_events(installation, work))

            claims = verify_capability(key, captured["capability"], now=100)
            self.assertEqual((claims.owner_id, claims.actor_id, claims.destination, claims.session_id), (installation.tomo_id, installation.actor_id, "telegram:123", f"telegram:actor:{installation.actor_id}"))
            self.assertEqual(captured["context"], (claims.owner_id, claims.actor_id, claims.destination, claims.session_id))
            self.assertEqual(captured["generation_id"], work.generation_id)
            self.assertIn("cron_list", visible_names)
            self.assertIs(runtime.tool_registry, base_registry)
            self.assertIs(runtime.conversation.tool_registry, base_registry)

    def test_in_process_interactive_turn_adds_generation_bound_peer_tools_and_restores_executor(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            base_registry = ToolRegistry()
            original_executor = ToolExecutor(base_registry)
            runtime = Mock()
            runtime.provider.supports_tool_calls = True
            runtime.tool_registry = base_registry
            runtime.conversation.tool_registry = base_registry
            runtime.conversation.tool_executor = original_executor
            visible_names = []
            runtime.handle_telegram_burst_iter.side_effect = lambda _burst, **_kwargs: visible_names.extend(item["function"]["name"] for item in runtime.conversation.tool_registry.schemas()) or iter(())
            instances = Mock(); instances.get.return_value = runtime
            captured = {}

            def fake_registry(client):
                captured["client"] = client
                return ToolRegistry((BoundTool(ToolSpec("peer_list", "list peers", {"type": "object", "properties": {}}), lambda _arguments: ()),))

            dispatch = InProcessTelegramRuntimeDispatch(instances, "http://127.0.0.1:8787", None, b"p" * 32, clock=lambda: 100.0)
            with patch("tomo_core.shared_gateway.peer_registry", side_effect=fake_registry):
                list(dispatch.iter_telegram_events(installation, work))

            claim = verify_peer_capability(b"p" * 32, captured["client"].capability, now=100, operation="ask")
            self.assertEqual(
                (
                    claim.owner_id,
                    claim.actor_id,
                    claim.destination,
                    claim.session_id,
                    claim.generation_id,
                ),
                (
                    installation.tomo_id,
                    installation.actor_id,
                    "telegram:123",
                    work.session_id,
                    work.generation_id,
                ),
            )
            self.assertIn("peer_list", visible_names)
            self.assertIs(runtime.tool_registry, base_registry)
            self.assertIs(runtime.conversation.tool_registry, base_registry)
            self.assertIs(runtime.conversation.tool_executor, original_executor)

    def test_cron_claim_executes_in_owner_bound_automation_lane_then_delivers_without_reply(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            cron = CronStore(store.data_dir)
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            cron.create(CronJob("job", installation.tomo_id, "telegram:123", JobIntent("check status", ("be concise",)), ScheduleSpec.once(now)))
            claim = cron.claim_due_run(now=now)
            dispatch = FakeRuntimeDispatch()
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)

            outcome, frames = gateway.execute_cron_claim(claim)
            receipt = gateway.send_cron_delivery(DeliveryAttempt("delivery", "run", 1, "lease", "leased", 0, "telegram:123", frames[0], 1, installation.tomo_id, "job"))

            self.assertEqual((outcome.value, frames), ("succeeded", ("scheduled result",)))
            turn = dispatch.calls[-1][2]
            self.assertEqual((turn.actor_id, turn.chat_id, turn.intent, turn.constraints), ("999", "123", "check status", ("be concise",)))
            self.assertEqual(receipt, "1")
            self.assertEqual(client.sent_messages, [{"actor_id": "123", "text": "scheduled result", "reply_to_message_id": None}])

    def test_cron_delivery_rejects_rebound_installation_before_telegram_send(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            client = FakeTelegramClient()
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch())
            attempt = DeliveryAttempt("delivery", "run", 1, "lease", "leased", 0, "telegram:123", "result", 1, f"other-{installation.tomo_id}", "job")

            with self.assertRaisesRegex(RuntimeError, "installation_unavailable"):
                gateway.send_cron_delivery(attempt)

            self.assertEqual(client.sent_messages, [])

    @staticmethod
    @contextmanager
    def _store():
        with tempfile.TemporaryDirectory() as data_dir:
            yield TelegramOnboardingStore(data_dir)

    @staticmethod
    def _installation(store, chat_id, actor_id):
        link = store.create_install_link("user-1", "tmnvm_bot")
        return store.consume_start_token(link.token, chat_id=chat_id, actor_id=actor_id)

    @staticmethod
    def _generation_work(store, tomo_id):
        store.enqueue_update(
            1,
            "123",
            '{"update_id":1,"message":{"message_id":1,"date":1,"from":{"id":999},"chat":{"id":123,"type":"private"},"text":"first"}}',
            now=1.0,
            update_kind="message",
            message_id="1",
            telegram_sent_at=1.0,
            tomo_id=tomo_id,
        )
        store.enqueue_update(
            2,
            "123",
            '{"update_id":2,"message":{"message_id":2,"date":2,"from":{"id":999},"chat":{"id":123,"type":"private"},"text":"second"}}',
            now=1.1,
            update_kind="message",
            message_id="2",
            telegram_sent_at=2.0,
            tomo_id=tomo_id,
        )
        work = store.claim_next_work(now=2.0)
        assert work is not None
        return work
