import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.models import InboundEnvelope, OutboundBubble
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta, StaticProvider
from tomo_core.shared_gateway import InProcessTelegramRuntimeDispatch, SharedTelegramGateway
from tomo_core.sandbox_dispatch import SandboxDispatchError
from tomo_core.sandbox_protocol import SandboxCompletedEvent, SandboxFrameEvent, SandboxReactionEvent
from tomo_core.telegram import FakeTelegramClient
from tomo_core.telegram_router import RetryableTelegramUpdateError


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


class SharedGatewayTests(unittest.TestCase):
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
            self.assertEqual(envelope.native_metadata["chat_id"], "123")
            self.assertEqual(envelope.native_metadata["delivery_chat_id"], "123")

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

    def test_later_bubble_failure_is_not_labeled_first_delivery(self):
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
                SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0).process_update(work)

            self.assertFalse(any(call.kwargs.get("outcome") == "error" for call in emit.call_args_list))

    def test_only_first_bubble_failure_is_labeled_first_delivery_attempt(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            work = self._generation_work(store, installation.tomo_id)
            client = FakeTelegramClient()
            client.send_message = Mock(side_effect=RuntimeError("send failed"))

            with patch("tomo_core.shared_gateway.latency_trace.emit") as emit:
                SharedTelegramGateway(client=client, store=store, dispatch=FakeRuntimeDispatch(), pace_seconds=0).process_update(work)

            first_delivery_errors = [call for call in emit.call_args_list if call.args[1] == "telegram_first_delivery" and call.kwargs.get("outcome") == "error"]
            self.assertEqual(len(first_delivery_errors), 1)

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
                gateway.process_update(work)

            self.assertEqual([message["text"] for message in client.sent_messages], ["first.", "second."])
            first_delivery_outcomes = [call.kwargs.get("outcome") for call in emit.call_args_list if call.args[1] == "telegram_first_delivery"]
            self.assertEqual(first_delivery_outcomes, ["send_complete", "origin_to_delivery"])
            self.assertEqual(store.delivery_status(work.generation_id, 0), "unknown")
            self.assertTrue(store.is_generation_active(work.generation_id, work.revision))
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
