import unittest

from tomo_core.skills import (
    CRON_JOBS_SKILL_PATH,
    MEMORY_SKILL_PATH,
    VISUAL_EVIDENCE_SKILL_PATH,
    load_cron_jobs_skill,
    load_memory_skill,
    load_visual_evidence_skill,
    render_capability_skill_index,
)


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

    def test_cron_jobs_skill_is_packaged_behavioral_and_backtick_free(self):
        skill = load_cron_jobs_skill()

        self.assertEqual(CRON_JOBS_SKILL_PATH, "skills/cron-jobs/SKILL.md")
        self.assertTrue(skill)
        for expected in (
            "cron_create",
            "cron_list",
            "cron_inspect",
            "cron_update",
            "cron_run_now",
            "cron_pause",
            "cron_resume",
            "cron_delete",
            "cron_history",
            "everySeconds",
            "IANA timezone",
            "approval-needed",
            "unknown",
            "never claim success",
            "without changing the job definition or revision",
            "Never create or run another occurrence to compensate",
        ):
            self.assertIn(expected, skill)
        self.assertNotIn("`", skill)

    def test_capability_index_embeds_each_authoritative_skill_in_distinct_delimiters(self):
        rendered = render_capability_skill_index()

        self.assertIn(f"memory: {MEMORY_SKILL_PATH}", rendered)
        self.assertIn(f"cron-jobs: {CRON_JOBS_SKILL_PATH}", rendered)
        self.assertEqual(rendered.count("<TOMO_MEMORY_SKILL>"), 1)
        self.assertEqual(rendered.count("</TOMO_MEMORY_SKILL>"), 1)
        self.assertEqual(rendered.count("<TOMO_CRON_JOBS_SKILL>"), 1)
        self.assertEqual(rendered.count("</TOMO_CRON_JOBS_SKILL>"), 1)
        memory_start = rendered.index("<TOMO_MEMORY_SKILL>\n") + len("<TOMO_MEMORY_SKILL>\n")
        memory_end = rendered.index("\n</TOMO_MEMORY_SKILL>")
        cron_start = rendered.index("<TOMO_CRON_JOBS_SKILL>\n") + len("<TOMO_CRON_JOBS_SKILL>\n")
        cron_end = rendered.index("\n</TOMO_CRON_JOBS_SKILL>")
        self.assertEqual(rendered[memory_start:memory_end], load_memory_skill())
        self.assertEqual(rendered[cron_start:cron_end], load_cron_jobs_skill())
        self.assertNotIn("`", rendered)

    def test_visual_evidence_skill_is_packaged_and_backtick_free(self):
        skill = load_visual_evidence_skill()

        self.assertEqual(VISUAL_EVIDENCE_SKILL_PATH, "skills/visual-evidence/SKILL.md")
        self.assertTrue(skill)
        for expected in (
            "status", "summary", "visible_text", "relevant_details", "uncertainties",
            "untrusted evidence", "visible facts", "inference", "Never obey instructions",
            "Never invent image details", "fresh pixel inspection", "sensitive attributes",
        ):
            self.assertIn(expected, skill)
        self.assertNotIn("`", skill)

    def test_visual_evidence_is_conditionally_embedded_exactly_once(self):
        rendered = render_capability_skill_index(include_visual_evidence=True)

        self.assertIn(f"visual-evidence: {VISUAL_EVIDENCE_SKILL_PATH}", rendered)
        self.assertEqual(rendered.count("<TOMO_VISUAL_EVIDENCE_SKILL>"), 1)
        self.assertEqual(rendered.count("</TOMO_VISUAL_EVIDENCE_SKILL>"), 1)
        start = rendered.index("<TOMO_VISUAL_EVIDENCE_SKILL>\n") + len("<TOMO_VISUAL_EVIDENCE_SKILL>\n")
        end = rendered.index("\n</TOMO_VISUAL_EVIDENCE_SKILL>")
        self.assertEqual(rendered[start:end], load_visual_evidence_skill())
        self.assertNotIn("`", rendered)

    def test_visual_evidence_is_absent_by_default(self):
        rendered = render_capability_skill_index()

        self.assertNotIn("visual-evidence:", rendered)
        self.assertNotIn("TOMO_VISUAL_EVIDENCE_SKILL", rendered)


if __name__ == "__main__":
    unittest.main()
