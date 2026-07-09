import tempfile
import unittest
from contextlib import contextmanager

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import StaticProvider
from tomo_core.shared_gateway import SharedTelegramGateway, TelegramRuntimeDispatch
from tomo_core.telegram import FakeTelegramClient


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

    def ensure_worker(self, tomo_id):
        self.calls.append(("worker", tomo_id))
        if self.worker_error:
            raise self.worker_error

    def send_connected(self, chat_id, reply_to_message_id):
        self.calls.append(("connected", chat_id, reply_to_message_id))

    def send_retry(self, chat_id, reply_to_message_id):
        self.calls.append(("retry", chat_id, reply_to_message_id))

    def dispatch(self, installation, envelope):
        self.calls.append(("dispatch", installation, envelope))


class SharedGatewayTests(unittest.TestCase):
    def test_start_binds_sends_setup_ensures_worker_then_connected(self):
        with self._store() as store:
            link = store.create_install_link("user-1", "tmnvm_bot")
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertEqual([call[0] for call in dispatch.calls], ["setup", "worker", "connected"])

    def test_start_keeps_binding_and_sends_retry_when_worker_setup_fails(self):
        with self._store() as store:
            link = store.create_install_link("user-1", "tmnvm_bot")
            dispatch = FakeRuntimeDispatch(worker_error=RuntimeError("unavailable"))
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertEqual([call[0] for call in dispatch.calls], ["setup", "worker", "retry"])

    def test_dm_routes_to_trusted_installation_with_sender_as_actor_and_bound_chat_as_target(self):
        with self._store() as store:
            installation = self._installation(store, chat_id="123", actor_id="999")
            dispatch = FakeRuntimeDispatch()
            gateway = SharedTelegramGateway(client=FakeTelegramClient(), store=store, dispatch=dispatch)

            self.assertTrue(gateway.process_update(private_update("hi", chat_id="123", from_id="999", message_id=2)))

            _, routed_installation, envelope = dispatch.calls[-1]
            self.assertEqual(routed_installation, installation)
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
                dispatch=TelegramRuntimeDispatch(client=client, instances=instances),
            )

            gateway.process_update(private_update("hi", chat_id="123", from_id="999", message_id=2))

            self.assertEqual(client.typing_actor_ids, ["123"])
            self.assertEqual(client.sent_messages[-1]["actor_id"], "123")
            session = instances.get(installation.tomo_id).sessions.load("telegram:actor:999")
            self.assertEqual(session.model_history()[-2]["content"], "hi")

    @staticmethod
    @contextmanager
    def _store():
        with tempfile.TemporaryDirectory() as data_dir:
            yield TelegramOnboardingStore(data_dir)

    @staticmethod
    def _installation(store, chat_id, actor_id):
        link = store.create_install_link("user-1", "tmnvm_bot")
        return store.consume_start_token(link.token, chat_id=chat_id, actor_id=actor_id)
