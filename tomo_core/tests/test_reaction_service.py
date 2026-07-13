import unittest

from tomo_core.reaction_service import ReactionDeliveryKey, ReactionService


class ReactionServiceTests(unittest.TestCase):
    def test_settings_veto_duplicate_suppression_and_bounded_metadata(self):
        active = lambda: True
        service = ReactionService(max_recent_deliveries=1)
        first = ReactionDeliveryKey("owner", "chat-1", "generation-1", 1, "message-1")
        second = ReactionDeliveryKey("owner", "chat-1", "generation-2", 2, "message-2")

        self.assertFalse(service.admit(first, reactions_enabled=False, is_active=active))
        self.assertTrue(service.admit(first, reactions_enabled=True, is_active=active))
        self.assertFalse(service.admit(first, reactions_enabled=True, is_active=active))
        self.assertTrue(service.admit(second, reactions_enabled=True, is_active=active))
        self.assertTrue(service.admit(first, reactions_enabled=True, is_active=active))

    def test_optional_cooldown_and_cancellation_veto(self):
        now = [10.0]
        service = ReactionService(cooldown_seconds=5, clock=lambda: now[0])
        first = ReactionDeliveryKey("owner", "chat-1", "generation-1", 1, "message-1")
        second = ReactionDeliveryKey("owner", "chat-1", "generation-2", 2, "message-2")

        self.assertFalse(service.admit(first, reactions_enabled=True, is_active=lambda: False))
        self.assertTrue(service.admit(first, reactions_enabled=True, is_active=lambda: True))
        self.assertFalse(service.admit(second, reactions_enabled=True, is_active=lambda: True))
        now[0] += 5
        self.assertTrue(service.admit(second, reactions_enabled=True, is_active=lambda: True))

    def test_cooldown_survives_other_owner_delivery_key_eviction(self):
        now = [10.0]
        service = ReactionService(cooldown_seconds=5, max_recent_deliveries=2, clock=lambda: now[0])
        owner = ReactionDeliveryKey("owner", "chat-1", "generation-1", 1, "message-1")
        other_one = ReactionDeliveryKey("other", "chat-1", "generation-1", 1, "message-1")
        other_two = ReactionDeliveryKey("third", "chat-1", "generation-1", 1, "message-1")
        retry = ReactionDeliveryKey("owner", "chat-1", "generation-2", 2, "message-2")

        self.assertTrue(service.admit(owner, reactions_enabled=True, is_active=lambda: True))
        self.assertTrue(service.admit(other_one, reactions_enabled=True, is_active=lambda: True))
        self.assertTrue(service.admit(other_two, reactions_enabled=True, is_active=lambda: True))
        self.assertFalse(service.admit(retry, reactions_enabled=True, is_active=lambda: True))

    def test_cooldown_is_scoped_to_owner_and_chat(self):
        now = [10.0]
        service = ReactionService(cooldown_seconds=5, clock=lambda: now[0])
        first = ReactionDeliveryKey("owner", "chat-1", "generation-1", 1, "message-1")
        other_chat = ReactionDeliveryKey("owner", "chat-2", "generation-2", 1, "message-2")
        same_chat = ReactionDeliveryKey("owner", "chat-1", "generation-3", 1, "message-3")

        self.assertTrue(service.admit(first, reactions_enabled=True, is_active=lambda: True))
        self.assertTrue(service.admit(other_chat, reactions_enabled=True, is_active=lambda: True))
        self.assertFalse(service.admit(same_chat, reactions_enabled=True, is_active=lambda: True))


if __name__ == "__main__":
    unittest.main()
