import unittest
from pathlib import Path


class SoulEllipsisTests(unittest.TestCase):
    def test_soul_defines_intentional_ascii_ellipsis_behavior_and_guardrails(self):
        soul = (Path(__file__).parents[1] / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("trailing off deliberately", soul)
        self.assertIn("standalone low-stakes awkward silence", soul)
        self.assertIn("intentional pause/omission inside a sentence", soul)
        self.assertIn("exactly three ASCII periods", soul)
        self.assertIn("at most one authored ellipsis", soul)
        for phrase in ("fake uncertainty", "material facts", "needed answer", "internal deliberation", "safety", "task-critical"):
            self.assertIn(phrase, soul)
        self.assertIn("never use it for grief, vulnerability, crisis, serious correction", soul.lower())
        self.assertIn("responding appropriately wins", soul)
        self.assertIn("i was going to say this plan is doomed, but...", soul)
        self.assertIn("> ...", soul)
        self.assertIn("the approval process was... optimistic.", soul)
        self.assertNotIn("> damn… that’s rough.", soul)


if __name__ == "__main__":
    unittest.main()
