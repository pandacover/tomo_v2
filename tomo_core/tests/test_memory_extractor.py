import unittest
from tomo_core.memory.extractor import ProactiveMemoryExtractor
from tomo_core.providers import ProviderTextDelta


class MockProvider:
    name = "mock"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, json_response: str) -> None:
        self.json_response = json_response

    def stream(self, messages, *, tools=(), actor_id=None):
        yield ProviderTextDelta(self.json_response)


class TestProactiveMemoryExtractor(unittest.TestCase):
    def test_extract_memories_parses_json_array(self):
        json_output = """
        [
            {
                "kind": "fact",
                "subject_key": "self",
                "topic": "name",
                "value": "Alex",
                "statement": "Owner's name is Alex",
                "confidence": 0.95,
                "salience": 1.0,
                "scope": "always"
            },
            {
                "kind": "preference",
                "subject_key": "self",
                "topic": "coffee",
                "value": "dark roast",
                "statement": "Owner prefers dark roast coffee",
                "confidence": 0.85,
                "salience": 0.7,
                "scope": "contextual"
            }
        ]
        """
        provider = MockProvider(json_output)
        extractor = ProactiveMemoryExtractor(provider=provider, min_confidence=0.5)

        controls = extractor.extract_memories(
            user_text="Hi, I'm Alex and I love dark roast coffee.",
            assistant_text="Nice to meet you, Alex!",
            generation_id="gen-123",
            timestamp="2026-07-24T00:00:00Z",
        )

        self.assertEqual(len(controls), 2)

        # First control: name claim forced to always scope
        self.assertEqual(controls[0].subject_key, "self")
        self.assertEqual(controls[0].topic, "name")
        self.assertEqual(controls[0].statement, "Owner's name is Alex")
        self.assertEqual(controls[0].surface_scope, "always")
        self.assertGreaterEqual(controls[0].confidence, 0.95)

        # Second control: preference claim
        self.assertEqual(controls[1].subject_key, "self")
        self.assertEqual(controls[1].topic, "coffee")
        self.assertEqual(controls[1].statement, "Owner prefers dark roast coffee")
        self.assertEqual(controls[1].surface_scope, "contextual")

    def test_extract_memories_filters_low_confidence(self):
        json_output = """
        [
            {
                "kind": "inference",
                "subject_key": "self",
                "topic": "mood",
                "value": "tired",
                "statement": "Owner might be tired",
                "confidence": 0.3,
                "salience": 0.4,
                "scope": "contextual"
            }
        ]
        """
        provider = MockProvider(json_output)
        extractor = ProactiveMemoryExtractor(provider=provider, min_confidence=0.5)

        controls = extractor.extract_memories(
            user_text="Long day...",
            assistant_text="Hope you get some rest!",
            generation_id="gen-456",
            timestamp="2026-07-24T00:00:00Z",
        )

        # Filtered out because confidence 0.3 < min_confidence 0.5
        self.assertEqual(len(controls), 0)

    def test_extract_memories_handles_markdown_fences(self):
        json_output = """```json
        [
            {
                "kind": "fact",
                "subject_key": "self",
                "topic": "city",
                "value": "Seattle",
                "statement": "Owner lives in Seattle",
                "confidence": 0.9,
                "salience": 0.8,
                "scope": "always"
            }
        ]
        ```"""
        provider = MockProvider(json_output)
        extractor = ProactiveMemoryExtractor(provider=provider, min_confidence=0.5)

        controls = extractor.extract_memories(
            user_text="I live in Seattle.",
            assistant_text="Seattle is great!",
            generation_id="gen-789",
            timestamp="2026-07-24T00:00:00Z",
        )

        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0].topic, "city")
        self.assertEqual(controls[0].statement, "Owner lives in Seattle")


if __name__ == "__main__":
    unittest.main()
