import json
import unittest

from tomo_core.context import ContextHydrator
from tomo_core.conversation.models import ConversationMove, ConversationRequest, MoveConfidence, MovePlan, TurnBudget
from tomo_core.conversation.prompts import build_segment_messages, build_segment_repair_messages
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst


class ConversationPromptTests(unittest.TestCase):
    def setUp(self):
        self.envelope = InboundEnvelope("telegram", "user-1", "message-1", "my interview is tomorrow")
        self.soul = "TOMO SOUL SENTINEL"
        self.history = ({"role": "user", "content": "i really need this job"},)

    def test_first_segment_prompt_requests_one_internal_plan_then_bounded_frames(self):
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

        self.assertIn('exactly one internal turn_plan JSONL record before any frame record', system)
        self.assertIn("zero to three frame JSONL records", system)
        self.assertIn('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"...","confidence":"low","reaction":null}', system)
        self.assertIn('{"type":"frame","text":"..."}', system)
        self.assertIn("zero to five memory_control records", system)
        self.assertIn("future conversations", system)
        self.assertIn("one JSON object per physical line, with no code fences, backticks, or prose outside records", system)
        self.assertNotIn("`", system)
        self.assertIn("turn-level purposes, never frame or bubble sections", system)
        self.assertIn("target one to two sentences per frame and no more than one to three frames this segment", system)
        self.assertIn("at most 3 frames per segment, 3 sentences per frame, and 800 characters per frame", system)
        self.assertIn("never claim an action happened without a supplied observation", system)
        self.assertIn("zero or one useful pre-tool frame", system)
        self.assertIn("batch independent related native tool calls in one assistant response", system)
        self.assertIn("tool announcements are optional social output, never execution telemetry", system)
        self.assertIn("memory and session context hydration is silent unless the user explicitly requests it", system)
        self.assertIn("do not offer mutation, booking, purchase, send, delete, or other side-effect capabilities", system)
        self.assertIn('"name":"search"', system)
        self.assertIn("## answer", system)
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

        self.assertIn("fixed turn plan", system)
        self.assertIn("primary_move=answer", system)
        self.assertIn("supporting_moves=reassure", system)
        self.assertIn("do not emit a turn_plan record", system)
        self.assertNotIn('{"type":"turn_plan"', system)
        self.assertIn('{"type":"frame","text":"..."}', system)
        self.assertIn("optional internal memory_control records before any frame record", system)
        self.assertIn("one JSON object per physical line, with no code fences, backticks, or prose outside records", system)
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

        self.assertIn("complete with one final frame", messages[0]["content"])
        self.assertIn("at most 1 frames per segment", messages[0]["content"])
        self.assertNotIn("one to three final frames", messages[0]["content"])

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
        self.assertIn("do not emit a turn_plan, native tool call", repair[-1]["content"])

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
