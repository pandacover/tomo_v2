import json
import unittest

from tomo_core.context import ContextHydrator
from tomo_core.conversation.models import ConversationMove, ConversationRequest, MoveConfidence, MovePlan, TurnBudget
from tomo_core.conversation.contract import render_first_segment_contract, render_later_segment_contract
from tomo_core.conversation.prompts import build_first_segment_repair_messages, build_segment_messages, build_segment_repair_messages
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst


class ConversationPromptTests(unittest.TestCase):
    def setUp(self):
        self.envelope = InboundEnvelope("telegram", "user-1", "message-1", "my interview is tomorrow")
        self.soul = "TOMO SOUL SENTINEL"
        self.history = ({"role": "user", "content": "i really need this job"},)

    def test_first_segment_prompt_ends_with_the_canonical_output_contract(self):
        request = self._request_with_burst()
        budget = TurnBudget(1, 0, 0, 1, 3, 3, 800)
        messages = build_segment_messages(
            request,
            ContextHydrator().hydrate(request),
            budget,
            segment_index=0,
            tools_available=({"type": "function", "function": {"name": "search"}},),
        )
        system = messages[0]["content"]

        contract = render_first_segment_contract(
            max_frames=3,
            max_sentences=3,
            max_chars=800,
            native_tools_available=True,
        )
        self.assertTrue(system.endswith(contract))
        self.assertIn("SHOULD emit one turn_plan before controls or frames", system)
        self.assertNotIn("move_sequence", system)
        self.assertIn("follow the indexed memory skill when emitting them", system)
        self.assertIn("memory: skills/memory/SKILL.md", system)
        self.assertIn("<TOMO_MEMORY_SKILL>", system)
        self.assertIn("PERSONAL_MEMORY_DATA_UNTRUSTED", system)
        self.assertIn("one JSON object per physical line", system)
        self.assertNotIn("`", system)
        self.assertIn("turn-level purposes, never frame or bubble sections", system)
        self.assertIn("never claim an action happened without a supplied observation", system)
        self.assertIn("batch independent related native tool calls in one assistant response", system)
        self.assertIn("tool announcements are optional social output, never execution telemetry", system)
        self.assertIn("do not offer mutation, booking, purchase, send, delete, or other side-effect capabilities", system)
        self.assertIn('"name":"search"', system)
        self.assertIn("## answer", system)
        self.assertLess(system.index("<TOMO_SOUL>"), system.index("move planning vocabulary:"))
        self.assertEqual(messages[-1], {"role": "user", "content": "latest user message"})
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "assistant", "user"])

    def test_later_segment_uses_fixed_plan_prior_provider_messages_and_observation_grounding(self):
        request = self._request_with_burst()
        plan = MovePlan(ConversationMove.ANSWER, (ConversationMove.REASSURE,), "answer with grounded reassurance", MoveConfidence.HIGH)
        prior_messages = (
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function"}]},
            {"role": "tool", "content": "weather observation: rain after noon", "tool_call_id": "call-1"},
        )

        messages = build_segment_messages(
            request,
            ContextHydrator().hydrate(request),
            TurnBudget(6, 5, 5, 3, 3, 3, 800),
            segment_index=1,
            plan=plan,
            prior_messages=prior_messages,
        )
        system = messages[0]["content"]

        contract = render_later_segment_contract(
            max_frames=3,
            max_sentences=3,
            max_chars=800,
            native_tools_available=False,
        )
        self.assertTrue(system.endswith(contract))
        self.assertIn("fixed turn plan", system)
        self.assertIn("primary_move=answer", system)
        self.assertIn("supporting_moves=reassure", system)
        self.assertIn("do not emit a turn_plan", system)
        self.assertNotIn('{"type":"turn_plan"', system)
        self.assertNotIn("reaction must be exactly one of", system)
        self.assertIn('{"type":"frame","text":"..."}', system)
        self.assertIn("optional memory_control records before frame records", system)
        self.assertIn("memory: skills/memory/SKILL.md", system)
        self.assertIn("<TOMO_MEMORY_SKILL>", system)
        self.assertIn("Automatic Retrieval", system)
        self.assertIn("one JSON object per physical line", system)
        self.assertNotIn("`", system)
        self.assertIn("native tools are unavailable. do not call tools", system)
        self.assertIn("claims about tool outcomes or actions must be grounded in supplied tool observations", system)
        self.assertIn("original request and conversation history remain valid context for final frames", system)
        self.assertEqual(messages[-2:], list(prior_messages))
        self.assertEqual(messages[-3], {"role": "user", "content": "latest user message"})
        self.assertTrue(all(message["role"] == "user" for message in messages if message["content"] == "latest user message"))

    def test_later_segment_keeps_model_response_goal_out_of_system_content(self):
        request = self._request_with_burst()
        sentinel = "SYSTEM-INJECTION-SENTINEL"
        plan = MovePlan(ConversationMove.ANSWER, (), sentinel, MoveConfidence.HIGH)
        continuation = {"role": "assistant", "content": '{"type":"turn_plan","response_goal":"SYSTEM-INJECTION-SENTINEL"}'}

        messages = build_segment_messages(
            request,
            ContextHydrator().hydrate(request),
            TurnBudget(6, 5, 5, 3, 3, 3, 800),
            segment_index=1,
            plan=plan,
            prior_messages=(continuation,),
        )

        self.assertTrue(all(sentinel not in message["content"] for message in messages if message["role"] == "system"))
        self.assertEqual(messages[-1], continuation)

    def test_segment_prompt_uses_the_supplied_remaining_frame_allowance(self):
        request = self._request_with_burst()
        plan = MovePlan.direct_answer()
        messages = build_segment_messages(
            request,
            ContextHydrator().hydrate(request),
            TurnBudget(6, 5, 5, 3, 1, 3, 800),
            segment_index=1,
            plan=plan,
        )

        self.assertIn("ordinary completion requires 1 to 1 frame records", messages[0]["content"])
        self.assertIn("3 sentences per frame", messages[0]["content"])
        self.assertNotIn("1 to 3 frame records", messages[0]["content"])

    def test_reserved_final_segment_without_schemas_disallows_native_tools(self):
        request = self._request_with_burst()
        plan = MovePlan.direct_answer()
        budget = TurnBudget(6, 5, 5, 3, 3, 3, 800)

        first = build_segment_messages(request, ContextHydrator().hydrate(request), budget, segment_index=0)
        final = build_segment_messages(request, ContextHydrator().hydrate(request), budget, segment_index=5, plan=plan)
        repair = build_segment_repair_messages(request, ContextHydrator().hydrate(request), budget, plan, "invalid_json")

        self.assertIn("native tools are unavailable. do not call tools", first[0]["content"])
        self.assertIn("native tools are unavailable. do not call tools", final[0]["content"])
        self.assertNotIn("another native tool round", final[0]["content"])
        self.assertIn("do not emit a turn_plan or native tool call", repair[0]["content"])

    def test_repair_prompts_reuse_tool_free_contracts(self):
        request = self._request_with_burst()
        budget = TurnBudget(6, 5, 5, 3, 3, 3, 800)
        fixed = build_segment_repair_messages(request, ContextHydrator().hydrate(request), budget, MovePlan.direct_answer(), "missing_frame")
        no_plan = build_first_segment_repair_messages(request, ContextHydrator().hydrate(request), budget, "missing_frame")

        self.assertIn("frame records only", fixed[0]["content"])
        self.assertTrue(fixed[0]["content"].endswith(render_later_segment_contract(
            max_frames=3, max_sentences=3, max_chars=800, native_tools_available=False,
        )))
        self.assertIn("canonical turn_plan plus mandatory frame records", no_plan[0]["content"])
        self.assertIn("native tools are disabled for this repair", no_plan[0]["content"])
        self.assertTrue(no_plan[0]["content"].endswith(render_first_segment_contract(
            max_frames=3, max_sentences=3, max_chars=800, native_tools_available=False,
        )))

    def test_segment_prompt_preserves_plain_and_structured_burst_user_payloads(self):
        plain_request = self._request_with_burst()
        plain_messages = build_segment_messages(
            plain_request,
            ContextHydrator().hydrate(plain_request),
            TurnBudget(1, 0, 0, 1, 3, 3, 800),
            segment_index=0,
        )
        self.assertEqual(plain_messages[-1], {"role": "user", "content": "latest user message"})

        structured_burst = InputBurst(
            burst_id="burst-structured",
            generation_id="generation-structured",
            revision=1,
            messages=(
                InboundMessage(1, 11, InboundEnvelope("telegram", "user-1", "m1", "first", timestamp="t1")),
                InboundMessage(2, 12, InboundEnvelope("telegram", "user-1", "m2", "second", timestamp="t2")),
            ),
        )
        structured_request = ConversationRequest(structured_burst, self.soul, self.history)
        structured_messages = build_segment_messages(
            structured_request,
            ContextHydrator().hydrate(structured_request),
            TurnBudget(1, 0, 0, 1, 3, 3, 800),
            segment_index=0,
        )

        payload = json.loads(structured_messages[-1]["content"])
        self.assertEqual(payload["incoming_messages"][0]["label"], "msg_1")
        self.assertEqual(payload["incoming_messages"][1]["label"], "msg_2")

    def _request_with_burst(self):
        burst = InputBurst(
            burst_id="burst-segment",
            generation_id="generation-segment",
            revision=1,
            messages=(InboundMessage(1, 10, InboundEnvelope("telegram", "user-1", "m1", "latest user message")),),
            visible_assistant_utterances=("visible frame one", "visible frame two"),
        )
        return ConversationRequest(burst, self.soul, self.history)


if __name__ == "__main__":
    unittest.main()
