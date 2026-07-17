import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tomo_core import AutomationTurn, PersonalAgentRuntime, RuntimeConfig
from tomo_core.models import AutomationFact
from tomo_core.conversation import ConversationRequest
from tomo_core.personal_data import MemorySearchQuery
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady
from tomo_core.sandbox_protocol import decode_automation, decode_inbound, encode_automation, encode_inbound
from tomo_core.sessions import ConversationSession
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer","confidence":"high","reaction":"👍"}\n'


def automation_turn(**changes):
    values = {
        "generation_id": "generation-1", "revision": 1, "job_id": "job-1", "run_id": "run-1",
        "actor_id": "actor-1", "chat_id": "chat-1", "intent": "Send the daily summary.",
        "scheduled_for": "2026-07-17T12:00:00+00:00",
    }
    values.update(changes)
    return AutomationTurn(**values)


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def stream(self, messages, *, tools=(), actor_id=None):
        self.calls.append((messages, tools, actor_id))
        return iter((ProviderTextDelta(self.payload), ProviderStreamCompleted("stop")))


class AutomationTurnTests(unittest.TestCase):
    def test_model_requires_valid_identity_and_timezone_aware_schedule(self):
        self.assertEqual(automation_turn().session_key, "telegram:actor:actor-1")
        for changes in ({"run_id": ""}, {"revision": True}, {"scheduled_for": "2026-07-17T12:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                automation_turn(**changes)

    def test_automation_wire_round_trip_is_strict_and_legacy_inbound_remains_readable(self):
        turn = automation_turn()
        payload = encode_automation("request-1", turn)
        self.assertEqual(decode_automation(payload), ("request-1", turn))
        malformed = json.loads(payload)
        malformed["extra"] = True
        with self.assertRaises(ValueError):
            decode_automation(json.dumps(malformed))

        from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst
        burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "actor-1", "message-1", "hello")),))
        self.assertEqual(decode_inbound(encode_inbound("request-2", burst)), ("request-2", burst))

    def test_automation_facts_are_bounded_provenance_and_survive_prompt_and_wire(self):
        fact = AutomationFact("Rain expected", "weather connection", "2026-07-17T11:00:00+00:00", "15 minutes", "forecast may change")
        turn = automation_turn(facts=(fact,))

        self.assertEqual(decode_automation(encode_automation("request-1", turn)), ("request-1", turn))
        self.assertIn("weather connection", turn.event_text)
        for changes in ({"source": ""}, {"value": "x" * 2001}, {"observed_at": "not-a-time"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AutomationFact(**{"value": "value", "source": "source", "observed_at": "2026-07-17T11:00:00+00:00", "freshness": "fresh", "uncertainty": "none", **changes})

    def test_session_projects_automation_as_marked_user_context_once(self):
        turn = automation_turn()
        session = ConversationSession(turn.session_key)
        self.assertTrue(session.append_automation_once(turn))
        self.assertFalse(session.append_automation_once(turn))
        stored = session.messages[0]
        self.assertEqual(stored.role, "automation")
        self.assertEqual(stored.metadata["source"], "automation")
        self.assertEqual(stored.metadata["actor_id"], "actor-1")
        self.assertEqual(session.model_history(), [{"role": "user", "content": turn.event_text}])
        self.assertTrue(turn.event_text.startswith("AUTOMATION EVENT. This is system-originated scheduled work, not a user message."))
        self.assertIsNone(ConversationRequest(turn, "soul", ()).envelope)

    def test_runtime_persists_automation_without_reaction_reply_or_memory_write(self):
        control = ('{"type":"memory_control","action":"add","authority":"autonomous",'
                   '"user_intent_excerpt":null,"memory_id":null,"kind":"fact","subject_key":"self",'
                   '"topic":"summary","value":"value","statement":"statement","confidence":0.8,'
                   '"salience":0.8,"surface_scope":"contextual","valid_from":null,"valid_until":null,'
                   '"sources":[{"source_kind":"assistant_conclusion","source_id":"generation-1",'
                   '"observed_at":"2026-07-17T12:00:00+00:00"}]}\n')
        with tempfile.TemporaryDirectory() as temporary:
            soul = Path(temporary) / "SOUL.md"
            soul.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider(PLAN + control + '{"type":"frame","text":"Daily summary."}\n')
            repository = SqlitePersonalDataRepository(Path(temporary) / "tomo.sqlite3")
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=temporary, soul_path=str(soul)), personal_data_repository=repository)

            turn = automation_turn()
            events = list(runtime.handle_automation_turn_iter(turn))
            duplicate_events = list(runtime.handle_automation_turn_iter(turn))

            self.assertEqual([type(event) for event in events], [RuntimeFrameReady, RuntimeCompleted])
            self.assertEqual([type(event) for event in duplicate_events], [RuntimeFrameReady, RuntimeCompleted])
            self.assertIsNone(events[0].bubble.reply_to_message_id)
            self.assertNotIn(RuntimeReactionReady, [type(event) for event in events])
            self.assertEqual(
                [item.reason_code for item in runtime.memory_control_diagnostics],
                ["invalid_provenance", "invalid_provenance"],
            )
            session = repository.load_session("local", "telegram:actor:actor-1")
            self.assertEqual([message.role for message in session.messages], ["automation", "assistant"])
            self.assertEqual(session.messages[0].metadata["run_id"], "run-1")
            self.assertEqual(session.messages[1].metadata["burst_id"], "run-1")
            self.assertEqual(provider.calls[0][2], "actor-1")
            self.assertTrue(provider.calls[0][1])
            self.assertEqual(repository.search_memories(MemorySearchQuery("local", "statement")), ())

    def test_automation_runtime_filters_consequential_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            soul = Path(temporary) / "SOUL.md"
            soul.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider(PLAN + '{"type":"frame","text":"Scheduled summary."}\n')
            unsafe = ToolRegistry((BoundTool(ToolSpec("send_payment", "Send a payment.", {"type": "object", "properties": {}}, read_only=False), lambda _: "sent"),))
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=temporary, soul_path=str(soul)), tool_registry=unsafe)

            list(runtime.handle_automation_turn_iter(automation_turn()))

            names = {schema["function"]["name"] for schema in provider.calls[0][1]}
            self.assertNotIn("send_payment", names)

    def test_automation_completes_with_approval_needed_for_a_blocked_tool(self):
        class ConsequentialProvider(ScriptedProvider):
            def stream(self, messages, *, tools=(), actor_id=None):
                return iter((ProviderToolCallReady("call-1", "send_payment", "{}"), ProviderStreamCompleted("tool_calls")))

        with tempfile.TemporaryDirectory() as temporary:
            soul = Path(temporary) / "SOUL.md"
            soul.write_text("SOUL", encoding="utf-8")
            unsafe = ToolRegistry((BoundTool(ToolSpec("send_payment", "Send a payment.", {"type": "object", "properties": {}}, read_only=False), lambda _: "sent"),))
            runtime = PersonalAgentRuntime(ConsequentialProvider(""), TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=temporary, soul_path=str(soul)), tool_registry=unsafe)

            events = list(runtime.handle_automation_turn_iter(automation_turn()))

            self.assertEqual(events[-1].event.result.status.value, "approval_needed")

    def test_v3_migration_preserves_messages_fts_and_accepted_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tomo.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES(3, '2026-07-17T12:00:00+00:00');
                CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,session_key TEXT NOT NULL,connector TEXT NOT NULL,actor_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,current_generation_id TEXT,current_revision INTEGER,UNIQUE(owner_id,session_key));
                CREATE TABLE messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,timestamp TEXT NOT NULL,ordinal INTEGER,connector_message_id TEXT,update_id INTEGER,burst_id TEXT,generation_id TEXT,generation_status TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX messages_user_delivery_identity ON messages(session_id,burst_id,update_id) WHERE role='user' AND burst_id IS NOT NULL AND update_id IS NOT NULL;
                CREATE UNIQUE INDEX messages_assistant_generation_identity ON messages(session_id,generation_id) WHERE role='assistant' AND generation_id IS NOT NULL;
                CREATE TABLE accepted_generations(session_id TEXT NOT NULL,generation_id TEXT NOT NULL,accepted_at TEXT NOT NULL,PRIMARY KEY(session_id,generation_id));
                CREATE VIRTUAL TABLE messages_fts USING fts5(record_id UNINDEXED,owner_id UNINDEXED,content);
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(record_id,owner_id,content) VALUES(new.id,'owner-1',new.content); END;
                CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN DELETE FROM messages_fts WHERE record_id=old.id; END;
                CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN DELETE FROM messages_fts WHERE record_id=old.id; INSERT INTO messages_fts(record_id,owner_id,content) VALUES(new.id,'owner-1',new.content); END;
                INSERT INTO sessions VALUES('session-1','owner-1','telegram:actor:actor-1','telegram','actor-1','2026-07-17T12:00:00+00:00','2026-07-17T12:00:00+00:00','gen-1',1);
                INSERT INTO messages VALUES('message-1','session-1','assistant','kept searchable','2026-07-17T12:00:00+00:00',1,NULL,NULL,'burst-1','gen-1','accepted','{}','2026-07-17T12:00:00+00:00');
                INSERT INTO accepted_generations VALUES('session-1','gen-1','2026-07-17T12:00:00+00:00');
            """)
            connection.commit()
            connection.close()

            repository = SqlitePersonalDataRepository(path)
            session = repository.load_session("owner-1", "telegram:actor:actor-1")
            self.assertEqual(session.accepted_generation_ids, ("gen-1",))
            self.assertEqual(session.messages[0].content, "kept searchable")
            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 4)
                self.assertEqual(check.execute("SELECT content FROM messages_fts WHERE record_id='message-1'").fetchone()[0], "kept searchable")
            finally:
                check.close()

    def test_future_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tomo.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES(5, '2026-07-17T12:00:00+00:00');
            """)
            connection.close()
            from tomo_core.personal_data import StorageCapabilityError
            with self.assertRaisesRegex(StorageCapabilityError, "unsupported_schema_version"):
                SqlitePersonalDataRepository(path)


if __name__ == "__main__":
    unittest.main()
