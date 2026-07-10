import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.models import InboundEnvelope, OutboundBubble
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import StaticProvider
from tomo_core.shared_gateway import InProcessTelegramRuntimeDispatch, SharedTelegramGateway
from tomo_core.sandbox_dispatch import SandboxDispatchError
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
            session = instances.get(installation.tomo_id).sessions.load("telegram:actor:999")
            self.assertEqual(session.model_history()[-2]["content"], "hi")

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

    @staticmethod
    @contextmanager
    def _store():
        with tempfile.TemporaryDirectory() as data_dir:
            yield TelegramOnboardingStore(data_dir)

    @staticmethod
    def _installation(store, chat_id, actor_id):
        link = store.create_install_link("user-1", "tmnvm_bot")
        return store.consume_start_token(link.token, chat_id=chat_id, actor_id=actor_id)
