import tempfile
import unittest
from pathlib import Path

from tomo_core import InboundEnvelope, PersonalAgentRuntime, RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.sessions import JsonSessionStore
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink


class MilestoneOneTests(unittest.TestCase):
    def test_telegram_turn_starts_typing_replies_first_bubble_and_persists_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            runtime = PersonalAgentRuntime(
                provider=StaticProvider("hey luv. i can hear you. what should we build first?"),
                telegram=TelegramDeliverySink(client),
                config=RuntimeConfig(data_dir=tmp, soul_path=str(Path(tmp) / "SOUL.md")),
            )
            envelope = InboundEnvelope(
                connector="telegram",
                actor_id="user-1",
                message_id="msg-9",
                text="hi tomo",
            )

            bubbles = runtime.handle_telegram_text(envelope)

            self.assertEqual(client.typing_actor_ids, ["user-1"])
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "msg-9")
            self.assertEqual(bubbles[0]["reply_to_message_id"], "msg-9")
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([m.role for m in session.messages], ["user", "assistant"])
            self.assertEqual(session.messages[0].content, "hi tomo")

    def test_delivery_is_plain_text_and_clamped_to_one_to_four_bubbles(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            response = "# one. **two**. `three`. four. five. six. seven. eight. nine. ten."
            runtime = PersonalAgentRuntime(
                provider=StaticProvider(response),
                telegram=TelegramDeliverySink(client),
                config=RuntimeConfig(data_dir=tmp),
            )

            bubbles = runtime.handle_telegram_text(
                InboundEnvelope(connector="telegram", actor_id="u", message_id="m", text="go")
            )

            self.assertGreaterEqual(len(bubbles), 1)
            self.assertLessEqual(len(bubbles), 4)
            sent_text = " ".join(message["text"] or "" for message in client.sent_messages)
            self.assertNotIn("#", sent_text)
            self.assertNotIn("**", sent_text)
            self.assertNotIn("`", sent_text)

    def test_delivery_sanitizes_banned_dashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            response = "bet — this is handled – no weird dash aura."
            runtime = PersonalAgentRuntime(
                provider=StaticProvider(response),
                telegram=TelegramDeliverySink(client),
                config=RuntimeConfig(data_dir=tmp),
            )

            runtime.handle_telegram_text(
                InboundEnvelope(connector="telegram", actor_id="u", message_id="m", text="go")
            )

            sent_text = " ".join(message["text"] or "" for message in client.sent_messages)
            self.assertNotIn("—", sent_text)
            self.assertNotIn("–", sent_text)
            self.assertIn("bet, this is handled, no weird dash aura.", sent_text)

    def test_dm_only_session_key_does_not_require_room_id(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-42", message_id="m", text="yo")
        self.assertEqual(envelope.session_key, "telegram:actor:user-42")


if __name__ == "__main__":
    unittest.main()
