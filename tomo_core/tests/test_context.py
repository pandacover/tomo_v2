import unittest
from pathlib import Path

from tomo_core.context import ContextFact, ContextHydrator, ContextSnapshot
from tomo_core.conversation.models import ConversationRequest
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst


class ContextHydratorTests(unittest.TestCase):
    def test_glossary_defines_personal_data_vocabulary(self):
        glossary = (Path(__file__).parents[1] / "CONTEXT.md").read_text(encoding="utf-8").lower()
        for term in (
            "personal data repository", "autonomous personal memory", "memory control",
            "epistemic kind", "surface scope", "provisional memory", "session search", "reaction intent",
        ):
            self.assertIn(term, glossary)

    def test_glossary_defines_peer_vocabulary_without_redefining_core_identity(self):
        glossary = (Path(__file__).parents[1] / "CONTEXT.md").read_text(
            encoding="utf-8"
        ).lower()
        for term in (
            "peer tomo",
            "agent relationship",
            "relationship grant",
            "inter-agent thread",
            "peer request",
            "commitment proposal",
            "pending peer confirmation",
        ):
            self.assertIn(term, glossary)
        self.assertIn(
            "a peer tomo can request or disclose, but cannot grant authority for either human owner",
            glossary,
        )
        self.assertEqual(glossary.count("owner identity is `tomo_id`"), 1)

    def test_hydrate_copies_current_history_and_visible_frames_without_the_new_burst(self):
        burst = InputBurst(
            burst_id="burst-1",
            generation_id="generation-1",
            revision=1,
            messages=(InboundMessage(1, 10, InboundEnvelope("telegram", "user-1", "m1", "latest user message")),),
            visible_assistant_utterances=("first visible frame", "second visible frame"),
        )
        request = ConversationRequest(
            burst=burst,
            soul="SOUL",
            history=(
                {"role": "user", "content": "earlier user message"},
                {"role": "assistant", "content": "earlier assistant reply"},
            ),
        )

        snapshot = ContextHydrator().hydrate(request)

        self.assertEqual(
            snapshot,
            ContextSnapshot(
                history=(
                    {"role": "user", "content": "earlier user message"},
                    {"role": "assistant", "content": "earlier assistant reply"},
                ),
                visible_frames=("first visible frame", "second visible frame"),
            ),
        )
        self.assertEqual(snapshot.facts, ())
        self.assertNotIn("latest user message", "\n".join(item["content"] for item in snapshot.history))
        self.assertIsNot(snapshot.history[0], request.history[0])

    def test_context_fact_preserves_future_source_metadata(self):
        fact = ContextFact("prefers concise answers", "current_conversation", "2026-07-12T00:00:00Z")

        self.assertEqual(fact.confidence, "provided")
        self.assertEqual(fact.sensitivity, "normal")


if __name__ == "__main__":
    unittest.main()
