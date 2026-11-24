# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Integrity of committed skill review patches: thread headers, exact context, ORIGIN.md notes."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
_PATCH_ROOT = _TOOLS_DIR / "skill-patches"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import adopt_skills


def _patch_files() -> list[Path]:
    """Every committed review patch, sorted and bounded."""
    return sorted(_PATCH_ROOT.rglob("*.patch"))[: adopt_skills.MAX_ROWS]


def _target(patch_file: Path) -> tuple[str, str]:
    """Return (skill name, file path inside the skill) a patch applies to."""
    rel_patch = patch_file.relative_to(_PATCH_ROOT)
    rel_target = PurePosixPath(*rel_patch.parts[1:]).as_posix().removesuffix(".patch")
    return rel_patch.parts[0], rel_target


def _reverse_check(git_bin: str, patch_text: str, committed: Path, rel_target: str) -> str:
    """Return git apply --reverse --check stderr for the patch against a scratch copy, or empty on success."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    with tempfile.TemporaryDirectory(prefix="patch-integrity-") as tmp:
        staged = Path(tmp) / rel_target
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(committed.read_bytes())
        proc = subprocess.run(
            [git_bin, "apply", "--whitespace=nowarn", "--reverse", "--check", "-"],
            cwd=tmp,
            input=patch_text.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=30.0,
            env=env,
        )
    if proc.returncode == 0:
        return ""
    return proc.stderr.decode("utf-8", errors="replace").strip() or f"git apply rc {proc.returncode}"


def _changes_items(skill: str) -> list[str]:
    """Return the '; '-separated items of the skill ORIGIN.md changes line."""
    origin = (_REPO_ROOT / "skills" / skill / "ORIGIN.md").read_text(encoding="utf-8")
    lines = [line for line in origin.splitlines()[: adopt_skills.MAX_ROWS] if line.startswith("changes: ")]
    if len(lines) != 1:
        return []
    return lines[0].removeprefix("changes: ").split("; ")


class SkillPatchIntegrityTests(unittest.TestCase):
    """Each patch must apply exactly on the next adoption run and be named in ORIGIN.md."""

    def test_patches_reverse_apply_on_committed_skills(self) -> None:
        """Thread header parses and committed file minus patch exists: context matches committed text."""
        git_bin = shutil.which("git")
        if git_bin is None:
            self.fail("git executable not found")
        patch_files = _patch_files()
        self.assertGreaterEqual(len(patch_files), 2)
        failures: list[str] = []
        for patch_file in patch_files[: adopt_skills.MAX_ROWS]:
            skill, rel_target = _target(patch_file)
            patch_text = patch_file.read_text(encoding="utf-8")
            try:
                threads = adopt_skills.patch_threads(patch_text, rel_target)
            except ValueError as exc:
                failures.append(f"{skill}/{rel_target}: {exc}")
                continue
            committed = _REPO_ROOT / "skills" / skill / rel_target
            if not threads or not committed.is_file():
                failures.append(f"{skill}/{rel_target}: no threads or no committed target")
                continue
            detail = _reverse_check(git_bin, patch_text, committed, rel_target)
            if detail:
                failures.append(f"{skill}/{rel_target}: {detail}")
        self.assertEqual(failures, [])

    def test_origin_notes_name_every_patch(self) -> None:
        """ORIGIN.md changes line carries the build_notes clause for exactly the committed patches."""
        by_skill: dict[str, dict[str, list[str]]] = {}
        for patch_file in _patch_files()[: adopt_skills.MAX_ROWS]:
            skill, rel_target = _target(patch_file)
            threads = adopt_skills.patch_threads(patch_file.read_text(encoding="utf-8"), rel_target)
            by_skill.setdefault(skill, {})[rel_target] = threads
        self.assertGreaterEqual(len(by_skill), 1)
        for skill, patched in sorted(by_skill.items())[: adopt_skills.MAX_ROWS]:
            skill_dir = _REPO_ROOT / "skills" / skill
            clause = adopt_skills.build_notes(skill_dir, [], {}, {}, {}, "", patched=patched)
            with self.subTest(skill=skill):
                self.assertIn(clause, _changes_items(skill))

    def test_origin_clause_check_refuses_a_missing_patch_entry(self) -> None:
        """Negative: a clause built without one committed patch differs from the clause with it."""
        patch_files = _patch_files()
        skill, rel_target = _target(patch_files[0])
        threads = adopt_skills.patch_threads(patch_files[0].read_text(encoding="utf-8"), rel_target)
        skill_dir = _REPO_ROOT / "skills" / skill
        full = adopt_skills.build_notes(skill_dir, [], {}, {}, {}, "", patched={rel_target: threads})
        empty = adopt_skills.build_notes(skill_dir, [], {}, {}, {}, "", patched={})
        self.assertNotEqual(full, empty)
        self.assertNotIn(empty, _changes_items(skill))


if __name__ == "__main__":
    unittest.main()
