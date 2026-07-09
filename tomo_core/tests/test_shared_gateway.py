import tempfile
import unittest

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import StaticProvider
from tomo_core.shared_gateway import SharedTelegramGateway
from tomo_core.telegram import FakeTelegramClient


def private_update(text, chat_id="123", message_id=1):
    return {"update_id": message_id, "message": {"message_id": message_id, "from": {"id": int(chat_id)}, "chat": {"id": int(chat_id), "type": "private"}, "text": text}}


class SharedGatewayTests(unittest.TestCase):
    def test_start_token_binds_chat_and_sends_welcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("user-1", "tmnvm_bot")
            registry = RuntimeInstanceRegistry(tmp, lambda _: StaticProvider("hello"), client)
            gateway = SharedTelegramGateway(client=client, store=store, instances=registry)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertIn("connected", client.sent_messages[-1]["text"])

    def test_message_routes_to_bound_runtime_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("user-1", "tmnvm_bot")
            installation = store.consume_start_token(link.token, chat_id="123", actor_id="123")
            registry = RuntimeInstanceRegistry(tmp, lambda _: StaticProvider("yooo from your own tomo"), client)
            gateway = SharedTelegramGateway(client=client, store=store, instances=registry)

            gateway.process_update(private_update("hi", chat_id="123", message_id=2))

            self.assertEqual(client.sent_messages[-1]["actor_id"], "123")
            self.assertIn("yooo", client.sent_messages[-1]["text"])
            self.assertTrue((__import__("pathlib").Path(tmp) / "instances" / installation.tomo_id / "sessions").exists())
