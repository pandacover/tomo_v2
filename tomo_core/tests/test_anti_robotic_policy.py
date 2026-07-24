import tempfile
import unittest
from pathlib import Path

from tomo_core.conversation.engine import should_repair_performative_output
from tomo_core.conversation.models import Frame
from tomo_core.conversation.prompts import build_segment_messages
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst
from tomo_core.personal_data import MemorySourceRef, MemoryWriteControl
from tomo_core.runtime import PersonalAgentRuntime, _is_low_stakes_casual_input
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository
from tomo_core.soul import load_soul
from tomo_core.telegram import TelegramDeliverySink


class FakeTelegramClient:
    def react_to_message(self, actor_id, message_id, emoji):
        pass


class AntiRoboticPolicyTests(unittest.TestCase):
    def test_soul_md_is_pruned_and_free_of_slang_dictionary(self):
        soul_text = load_soul(Path("SOUL.md"))
        lines = [line for line in soul_text.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 35, "SOUL.md should be concise (~20 lines)")
        self.assertNotIn("## slang", soul_text.lower(), "Slang dictionary must be removed from SOUL.md")
        self.assertNotIn("bro ur aura is cooked", soul_text.lower(), "In-prompt calibration examples must be removed from SOUL.md")

    def test_tomo_core_policy_injected_in_system_prompt(self):
        from tomo_core.conversation.models import ConversationRequest, TurnBudget
        envelope = InboundEnvelope("telegram", "u1", "m1", "hello")
        request = ConversationRequest.from_history(envelope=envelope, soul="SOUL text", history=[])
        from tomo_core.context import ContextSnapshot
        ctx = ContextSnapshot((), ())
        budget = TurnBudget(1, 0, 0, 1, 1, 1, 100)
        messages = build_segment_messages(request, ctx, budget, segment_index=0)
        sys_content = messages[0]["content"]
        self.assertIn("<TOMO_CORE_POLICY>", sys_content)
        self.assertIn("plain is better than performed", sys_content)
        self.assertIn("<TOMO_MEMORY_CONTRACT>", sys_content)

    def test_relevance_gating_identifies_low_stakes_casual_inputs(self):
        casual_inputs = [
            "having tea with wifey",
            "nm you tell me",
            "hey",
            "yo",
            "i'll deal with it tomorrow",
            "should i rewrite it again?",
        ]
        for text in casual_inputs:
            self.assertTrue(_is_low_stakes_casual_input(text), f"Expected low-stakes casual input: {text}")

        informational_inputs = [
            "why is my server crashing with OOM?",
            "what is the weather like in Seattle?",
            "remember that my passport expires in 2028",
            "can you fix this SQL query?",
            "how do I deploy to Railway?",
        ]
        for text in informational_inputs:
            self.assertFalse(_is_low_stakes_casual_input(text), f"Expected informational input: {text}")

    def test_performative_repair_evaluates_short_casual_bloat(self):
        casual_user_text = "having tea with wifey"

        # Performative output (>16 words / >1 sentence) -> repair candidate
        bloated_frames = [
            Frame(0, 0, "bet. tea with wifey is a clean little win."),
            Frame(0, 1, "hope the tea is actually good and not just emotional support water in a mug."),
        ]
        self.assertTrue(should_repair_performative_output(casual_user_text, bloated_frames))

        # Plain concise output (1 sentence, <= 16 words) -> no repair
        plain_frames = [Frame(0, 0, "nice, enjoy")]
        self.assertFalse(should_repair_performative_output(casual_user_text, plain_frames))

    def test_performative_repair_skips_technical_questions(self):
        tech_question = "why is my server crashing?"
        detailed_tech_output = [
            Frame(0, 0, "The server is crashing because out-of-memory (OOM) killer is terminating node processes when peak RAM hits 100%."),
            Frame(0, 1, "Increase the container memory limit or optimize heavy JSON array parsing."),
        ]
        # Must return False to avoid false positives on valid technical answers!
        self.assertFalse(should_repair_performative_output(tech_question, detailed_tech_output))

    def test_15_representative_conversations_evaluation(self):
        eval_cases = [
            {"input": "having tea with wifey", "casual": True},
            {"input": "nm you tell me", "casual": True},
            {"input": "hey", "casual": True},
            {"input": "why is my server crashing with OOM?", "casual": False},
            {"input": "what is the weather like in Seattle?", "casual": False},
            {"input": "please use normal vocabulary from now on", "casual": False},
            {"input": "anyway did you see that new trailer?", "casual": True},
            {"input": "my friend passed away yesterday", "casual": False},
            {"input": "can you fix this SQL query?", "casual": False},
            {"input": "i'll deal with it tomorrow", "casual": True},
            {"input": "should i rewrite it again?", "casual": True},
            {"input": "what do you think about this logo?", "casual": False},
            {"input": "will this database migration definitely work?", "casual": False},
            {"input": "i can just mix bleach and ammonia right?", "casual": False},
            {"input": "remember that my passport expires in 2028", "casual": False},
        ]

        for case in eval_cases:
            is_casual = _is_low_stakes_casual_input(case["input"])
            self.assertEqual(is_casual, case["casual"], f"Evaluation failed for input: {case['input']}")


if __name__ == "__main__":
    unittest.main()
