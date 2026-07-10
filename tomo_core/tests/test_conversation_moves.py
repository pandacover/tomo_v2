import unittest

from tomo_core.conversation.models import ConversationMove
from tomo_core.conversation.moves import MOVE_CATALOG, render_move_procedures


class ConversationMoveCatalogTests(unittest.TestCase):
    def test_every_move_has_one_soul_molded_definition(self):
        self.assertEqual(set(MOVE_CATALOG), set(ConversationMove))
        for move, definition in MOVE_CATALOG.items():
            with self.subTest(move=move):
                self.assertTrue(definition.objective)
                self.assertGreaterEqual(len(definition.procedure), 3)
                self.assertTrue(definition.completion)

    def test_render_only_includes_selected_move_procedures(self):
        rendered = render_move_procedures((ConversationMove.CHALLENGE, ConversationMove.JOKE))
        self.assertIn("challenge", rendered)
        self.assertIn("joke", rendered)
        self.assertNotIn("reassure", rendered)
        self.assertIn("challenge the decision or reasoning", rendered)


if __name__ == "__main__":
    unittest.main()
