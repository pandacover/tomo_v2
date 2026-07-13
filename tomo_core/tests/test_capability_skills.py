import unittest

from tomo_core.skills import MEMORY_SKILL_PATH, load_memory_skill, render_capability_skill_index


class CapabilitySkillTests(unittest.TestCase):
    def test_memory_skill_is_packaged_nonempty_and_safe_for_system_content(self):
        skill = load_memory_skill()

        self.assertEqual(MEMORY_SKILL_PATH, "skills/memory/SKILL.md")
        self.assertTrue(skill)
        self.assertIn("PERSONAL_MEMORY_DATA_UNTRUSTED", skill)
        self.assertIn("search_memories", skill)
        self.assertIn("Permanent deletion requires confirmation", skill)
        self.assertNotIn("`", skill)

    def test_capability_index_names_and_embeds_the_authoritative_memory_skill(self):
        rendered = render_capability_skill_index()

        self.assertIn(f"memory: {MEMORY_SKILL_PATH}", rendered)
        self.assertIn("<TOMO_MEMORY_SKILL>", rendered)
        self.assertIn(load_memory_skill(), rendered)
        self.assertNotIn("`", rendered)


if __name__ == "__main__":
    unittest.main()
