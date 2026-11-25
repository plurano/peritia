#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for check_skills.py and skills/INDEX.tsv verification."""
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

import check_skills

MD = "---\nname: {name}\ndescription: Use when testing.\n---\n\n# Example\n"
ORIGIN = ("upstream: https://github.com/owner/repo\npath: skills/demo\ncommit: " + "a" * 40 +
          "\nlicence: MIT\nchanges: added provenance\n")


class CheckSkillsLicenceTests(unittest.TestCase):
    def test_readme_licence_exception_example_matches_schema(self):
        """Load the documented example as a licence exception register."""
        readme = Path(__file__).resolve().parent.parent / "README.md"
        sample = re.search(r"^```yaml\n(exceptions:\n.*?)^```$", readme.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(sample, "README needs a complete YAML exception example")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register = root / check_skills.LICENCE_EXCEPTIONS_FILE
            register.parent.mkdir()
            register.write_text(sample.group(1), encoding="utf-8")
            self.assertEqual(len(check_skills._read_licence_exceptions(root, today=date.max)), 1)

    def test_strict_front_matter_projects_both_licence_keys_after_key_limit(self):
        """Keep both trimmed licence declarations after 100 other keys."""
        filler = "".join(f"field-{index}: value\n" for index in range(100))
        text = f'---\n{filler}license: " GPL-3.0-only "\nlicence: " LGPL-3.0-only "\n---\n'
        for option in ("exact_license", "require_string_licence"):
            with self.subTest(option=option):
                meta = check_skills.front_matter(text, **{option: True})
                self.assertEqual(meta["license"], "GPL-3.0-only")
                self.assertEqual(meta["licence"], "LGPL-3.0-only")
        self.assertNotIn("license", check_skills.front_matter(text))
        self.assertNotIn("licence", check_skills.front_matter(text))


class CheckSkillsIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        check_skills.write(self.root, "alpha", MD.format(name="alpha"), ORIGIN)
        check_skills.write(self.root, "beta", MD.format(name="beta"), "upstream: own\n", licence=False)
        check_skills.write_index(self.root, [
            {"skill": "alpha", "source": "owner/repo", "functions": "coding", "domains": "dev", "overlap_group": "1"},
            {"skill": "beta", "source": "peritia", "functions": "", "domains": "", "overlap_group": ""},
        ])

    def test_valid_index_and_skills_pass(self):
        self.assertEqual(check_skills.check(self.root), 0)
        self.assertEqual(check_skills.index_problems(self.root), [])

    def test_missing_row_refused_naming_skill(self):
        check_skills.write_index(self.root, [
            {"skill": "alpha", "source": "owner/repo"},
        ])
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any("missing row for beta" in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)

    def test_extra_row_refused_naming_skill(self):
        check_skills.write_index(self.root, [
            {"skill": "alpha", "source": "owner/repo"},
            {"skill": "beta", "source": "peritia"},
            {"skill": "gamma", "source": "owner/repo"},
        ])
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any("extra row for gamma" in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)

    def test_missing_index_file_refused(self):
        (self.root / "skills" / "INDEX.tsv").unlink()
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any("file missing" in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)

    def test_header_exact_check(self):
        (self.root / "skills" / "INDEX.tsv").write_text("wrong\theader\nalpha\towner/repo\t\t\t\n", encoding="utf-8")
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any("header must be exact" in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)

    def test_duplicate_row_refused(self):
        check_skills.write_index(self.root, [
            {"skill": "alpha", "source": "owner/repo"},
            {"skill": "alpha", "source": "owner/repo"},
            {"skill": "beta", "source": "peritia"},
        ])
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any("duplicate row for alpha" in p for p in problems), problems)

    def test_workbench_and_agent_control_files_refused(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / ".hive").mkdir()
        (alpha / ".hive" / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
        problems = check_skills.skill_problems(alpha)
        self.assertTrue(any("alpha/.hive/PROTOCOL.md" in p for p in problems), problems)
        (alpha / ".hive" / "PROTOCOL.md").unlink()
        (alpha / ".hive").rmdir()
        self.assertEqual(check_skills.skill_problems(alpha), [])

        (alpha / "notes.md").write_text("Prose mentioning .hive and PROTOCOL.md stays valid.\n", encoding="utf-8")
        self.assertEqual(check_skills.skill_problems(alpha), [])

    def test_dangling_skill_md_link_refused(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha") + "\nSee [Guide](reference/missing.md) and [Present](notes.md).\n",
            encoding="utf-8",
        )
        (alpha / "notes.md").write_text("# Present\n", encoding="utf-8")
        problems = check_skills.skill_problems(alpha)
        self.assertTrue(any("links to missing file reference/missing.md" in p for p in problems), problems)
        self.assertFalse(any("notes.md" in p for p in problems), problems)

    def test_planted_tilde_fence_containing_backtick_fence(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n~~~\n```\n[Inside](reference/inside-tilde.md)\n~~~\n"
            + "See [After](reference/after-tilde.md).\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertFalse(any("inside-tilde.md" in p for p in problems), problems)
        self.assertTrue(any("links to missing file reference/after-tilde.md" in p for p in problems), problems)

    def test_planted_quad_fence_containing_triple_python_fence(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n````markdown\n```python\n[Inside](reference/inside-quad.md)\n````\n"
            + "See [After](reference/after-quad.md).\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertFalse(any("inside-quad.md" in p for p in problems), problems)
        self.assertTrue(any("links to missing file reference/after-quad.md" in p for p in problems), problems)

    def test_top_level_four_space_backticks_is_no_fence_and_link_after_checked(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n    ```\n[Missing](reference/missing.md)\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertTrue(any("links to missing file reference/missing.md" in p for p in problems), problems)

    def test_nested_list_four_space_fence_skips_link_inside(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n* item\n    ```\n    [Inside](reference/inside.md)\n    ```\nSee [After](reference/after.md).\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertFalse(any("inside.md" in p for p in problems), problems)
        self.assertTrue(any("links to missing file reference/after.md" in p for p in problems), problems)

    def test_tilde_fence_skips_link_inside(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n~~~\n[Inside](reference/inside.md)\n~~~\nSee [After](reference/after.md).\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertFalse(any("inside.md" in p for p in problems), problems)
        self.assertTrue(any("links to missing file reference/after.md" in p for p in problems), problems)

    def test_unclosed_fence_runs_to_end_and_skips_links(self):
        alpha = self.root / "skills" / "alpha"
        (alpha / "SKILL.md").write_text(
            MD.format(name="alpha")
            + "\n```\n[Inside](reference/inside.md)\n[Still Inside](reference/still-inside.md)\n",
            encoding="utf-8",
        )
        problems = check_skills.skill_problems(alpha)
        self.assertFalse(any("inside.md" in p for p in problems), problems)
        self.assertFalse(any("still-inside.md" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
