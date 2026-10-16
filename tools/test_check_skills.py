#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for check_skills.py and skills/INDEX.tsv verification."""
import tempfile
import unittest
from pathlib import Path

import check_skills

MD = "---\nname: {name}\ndescription: Use when testing.\n---\n\n# Example\n"
ORIGIN = ("upstream: https://github.com/owner/repo\npath: skills/demo\ncommit: " + "a" * 40 +
          "\nlicence: MIT\nchanges: added provenance\n")


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


if __name__ == "__main__":
    unittest.main()
