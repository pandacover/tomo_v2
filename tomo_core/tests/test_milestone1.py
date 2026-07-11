import tempfile
import unittest
from pathlib import Path

from tomo_core import ConversationEngine, ConversationRequest, InboundEnvelope, InboundMessage, InputBurst, PersonalAgentRuntime, RuntimeConfig
from tomo_core.conversation.parsing import ConversationOutputError
from tomo_core.delivery import DeliveryPlanner, split_sentences
from tomo_core.providers import StaticProvider
from tomo_core.sessions import JsonSessionStore, StoredMessage
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

    def test_delivery_compatibility_fallback_is_plain_text(self):
        bubbles = DeliveryPlanner().compose("# one. **two**. `three`.", "m")
        self.assertEqual([bubble.text for bubble in bubbles], ["one. two. three."])

    def test_delivery_composes_intentional_utterances_without_truncating(self):
        planner = DeliveryPlanner(max_bubbles=4, max_sentences_per_bubble=3)
        bubbles = planner.compose_utterances(("first thought.", "second thought? still here."), reply_to_message_id="m1")
        self.assertEqual([bubble.text for bubble in bubbles], ["first thought.", "second thought? still here."])
        self.assertEqual(bubbles[0].reply_to_message_id, "m1")
        self.assertIsNone(bubbles[1].reply_to_message_id)
        with self.assertRaises(ValueError):
            planner.compose_utterances((), "m1")
        with self.assertRaises(ValueError):
            planner.compose_utterances(("1", "2", "3", "4", "5"), "m1")
        with self.assertRaises(ValueError):
            planner.compose_utterances(("one. two. three. four.",), "m1")
        with self.assertRaisesRegex(ValueError, "exceeds delivery bubble limit"):
            planner.compose(
                "one. two. three. four. five. six. seven. eight. nine. ten. eleven. twelve. thirteen.",
                "m1",
            )

    def test_static_provider_rejects_oversized_responses_instead_of_truncating(self):
        provider = StaticProvider(" ".join(f"sentence {index}." for index in range(13)))
        request = ConversationRequest.from_history(envelope=InboundEnvelope("telegram", "u", "m", "go"), soul="soul", history=())

        with self.assertRaises(ConversationOutputError) as raised:
            ConversationEngine(provider).respond(request)
        self.assertEqual(raised.exception.code, "sentence_limit")

    def test_static_provider_preserves_the_configured_smoke_response(self):
        response = "yo. telegram is wired."
        request = ConversationRequest.from_history(envelope=InboundEnvelope("telegram", "u", "m", "go"), soul="soul", history=())
        self.assertEqual(ConversationEngine(StaticProvider(response)).respond(request).utterances, (response,))

    def test_sentence_boundaries_do_not_split_urls_versions_or_decimals(self):
        text = "use https://example.com/a with v2.1 and 3.14 values. then continue."
        self.assertEqual(split_sentences(text), ["use https://example.com/a with v2.1 and 3.14 values.", "then continue."])

    def test_delivery_sanitizes_banned_dashes(self):
        bubbles = DeliveryPlanner().compose("bet — this is handled – no weird dash aura.", "m")
        self.assertEqual([bubble.text for bubble in bubbles], ["bet, this is handled, no weird dash aura."])

    def test_dm_only_session_key_does_not_require_room_id(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-42", message_id="m", text="yo")
        self.assertEqual(envelope.session_key, "telegram:actor:user-42")

    def test_input_burst_preserves_ordered_message_boundaries(self):
        burst = InputBurst(
            burst_id="burst-1",
            generation_id="generation-2",
            revision=2,
            messages=(
                InboundMessage(1, 41, InboundEnvelope("telegram", "u", "101", "first", timestamp="2026-07-11T00:00:00+00:00")),
                InboundMessage(2, 42, InboundEnvelope("telegram", "u", "102", "second", timestamp="2026-07-11T00:00:03+00:00")),
            ),
        )

        self.assertEqual(burst.latest.message_id, "102")
        self.assertEqual([message.envelope.text for message in burst.messages], ["first", "second"])

    def test_input_burst_rejects_invalid_batches(self):
        good = InboundMessage(1, 41, InboundEnvelope("telegram", "u", "101", "first"))
        cases = [
            {"messages": ()},
            {"revision": 0, "messages": (good,)},
            {"messages": (InboundMessage(2, 42, InboundEnvelope("telegram", "u", "102", "gap")),)},
            {"messages": (good, InboundMessage(1, 42, InboundEnvelope("telegram", "u", "102", "duplicate ordinal")))},
            {"messages": (good, InboundMessage(2, 41, InboundEnvelope("telegram", "u", "102", "duplicate update")))},
            {"messages": (good, InboundMessage(2, 42, InboundEnvelope("telegram", "u", "101", "duplicate message")))},
            {"messages": (good, InboundMessage(2, 42, InboundEnvelope("telegram", "other", "102", "mixed actor")))},
            {"messages": (good,), "visible_assistant_utterances": (" ",)},
        ]
        for kwargs in cases:
            params = {"burst_id": "burst", "generation_id": "generation", "revision": 1, "messages": (good,)}
            params.update(kwargs)
            with self.subTest(params=params), self.assertRaises(ValueError):
                InputBurst(**params)

    def test_session_persists_burst_messages_once_and_filters_provisional_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:u")
            first = InboundMessage(1, 41, InboundEnvelope("telegram", "u", "101", "first", timestamp="2026-07-11T00:00:00+00:00"))
            second = InboundMessage(2, 42, InboundEnvelope("telegram", "u", "102", "second", timestamp="2026-07-11T00:00:03+00:00"))

            self.assertTrue(session.append_inbound_once(first, "burst-1"))
            self.assertTrue(session.append_inbound_once(second, "burst-1"))
            self.assertFalse(session.append_inbound_once(first, "burst-1"))
            session.append(StoredMessage("assistant", "legacy visible"))
            session.append(StoredMessage("assistant", "stale draft", metadata={"generation_id": "gen-old", "generation_status": "provisional"}))
            session.append(StoredMessage("assistant", "accepted draft", metadata={"generation_id": "gen-ok", "generation_status": "provisional"}))
            session.accept_generations(("gen-ok",))
            store.save_atomic(session)

            saved = JsonSessionStore(tmp).load("telegram:actor:u")
            user_messages = [message for message in saved.messages if message.role == "user"]
            self.assertEqual([message.content for message in user_messages], ["first", "second"])
            self.assertEqual(user_messages[0].metadata["update_id"], 41)
            self.assertEqual(user_messages[0].metadata["message_id"], "101")
            self.assertEqual(user_messages[0].metadata["burst_id"], "burst-1")
            self.assertEqual(user_messages[0].timestamp, "2026-07-11T00:00:00+00:00")
            history = saved.model_history_for_burst("burst-1")
            self.assertEqual([item["content"] for item in history], ["legacy visible", "accepted draft"])

            unaccepted_history = saved.model_history_for_burst("burst-2")
            self.assertNotIn("stale draft", [item["content"] for item in unaccepted_history])

            saved_path = store._path("telegram:actor:u")
            self.assertEqual(saved_path.read_text(encoding="utf-8")[0], "{")
            self.assertEqual(list(saved_path.parent.glob("*.tmp")), [])

    def test_stale_session_snapshots_merge_without_losing_newer_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            stale = store.load("telegram:actor:u")
            newer = store.load("telegram:actor:u")
            newer.append(StoredMessage("user", "newer", metadata={"burst_id": "b2", "update_id": 2}))
            store.save_atomic(newer)
            stale.append(StoredMessage("user", "older", metadata={"burst_id": "b1", "update_id": 1}))

            store.save_atomic(stale)

            saved = store.load("telegram:actor:u")
            self.assertEqual({message.content for message in saved.messages}, {"newer", "older"})


if __name__ == "__main__":
    unittest.main()
