#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for judge_groups.py; no test calls a live model."""
import csv
import json
import re
import tempfile
import unittest
from pathlib import Path
import sys

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import classify
import judge_groups

REPO_ROOT = Path(__file__).resolve().parent.parent


class RuleGraderTests(unittest.TestCase):
    def test_rule_grader_pass_fail(self):
        must_match = [r"def\s+get\b", r"def\s+clear\b", r"threading\.Lock"]
        must_not_match = [r"TODO", r"pass\s*#"]

        # Valid answer
        good_answer = """
        import threading
        class Cache:
            def __init__(self):
                self._lock = threading.Lock()
            def get(self, k):
                with self._lock: return 1
            def clear(self):
                with self._lock: pass
        """
        passed, failed = judge_groups.grade_answer(good_answer, must_match, must_not_match)
        self.assertTrue(passed)
        self.assertEqual(failed, [])

        # Missing a required match
        missing_answer = """
        class Cache:
            def get(self, k): return 1
        """
        passed, failed = judge_groups.grade_answer(missing_answer, must_match, must_not_match)
        self.assertFalse(passed)
        self.assertTrue(any("missing" in f for f in failed))

        # Contains a forbidden match
        forbidden_answer = good_answer + "\n# TODO: optimize later"
        passed, failed = judge_groups.grade_answer(forbidden_answer, must_match, must_not_match)
        self.assertFalse(passed)
        self.assertTrue(any("unexpected" in f for f in failed))


class ScoringAndVerdictTests(unittest.TestCase):
    def test_pass_at_k_vs_pass_pow_k(self):
        tasks = [
            judge_groups.Task("t1", "prompt 1", ("match",), (), "rubric 1"),
            judge_groups.Task("t2", "prompt 2", ("match",), (), "rubric 2"),
            judge_groups.Task("t3", "prompt 3", ("match",), (), "rubric 3"),
        ]
        # t1: 1 pass, 2 fails -> pass@3 = 1.0, pass^3 = 0.0
        # t2: 3 passes -> pass@3 = 1.0, pass^3 = 1.0
        # t3: 3 fails -> pass@3 = 0.0, pass^3 = 0.0
        trial_records = [
            {"member": "m1", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 7.0},
            {"member": "m1", "task_id": "t1", "trial": 2, "pass": False, "rubric_score": 4.0},
            {"member": "m1", "task_id": "t1", "trial": 3, "pass": False, "rubric_score": 3.0},
            {"member": "m1", "task_id": "t2", "trial": 1, "pass": True, "rubric_score": 9.0},
            {"member": "m1", "task_id": "t2", "trial": 2, "pass": True, "rubric_score": 8.0},
            {"member": "m1", "task_id": "t2", "trial": 3, "pass": True, "rubric_score": 8.5},
            {"member": "m1", "task_id": "t3", "trial": 1, "pass": False, "rubric_score": 1.0},
            {"member": "m1", "task_id": "t3", "trial": 2, "pass": False, "rubric_score": 2.0},
            {"member": "m1", "task_id": "t3", "trial": 3, "pass": False, "rubric_score": 1.5},
        ]
        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["m1"], tasks, trial_records, k=3)
        self.assertEqual(verdict, "keep-better")
        self.assertEqual(decided_by, "rule")
        self.assertAlmostEqual(scores["m1"]["pass_at_k"], 2 / 3, places=3)
        self.assertAlmostEqual(scores["m1"]["pass_pow_k"], 1 / 3, places=3)

    def test_keep_better_at_gap(self):
        tasks = [
            judge_groups.Task(f"t{i}", f"prompt {i}", ("match",), (), "rubric")
            for i in range(1, 5)
        ]
        # Cand A passes 3/4 tasks (rule_score = 0.75), judge_score = 4.0 (lower)
        # Cand B passes 2/4 tasks (rule_score = 0.50), judge_score = 9.0 (higher)
        # Rule score gap is 0.75 - 0.50 = 0.25 >= 0.20 -> keep-better for Cand A (decided_by rule).
        trial_records = []
        for i in range(1, 5):
            for seed in range(1, 4):
                a_pass = i <= 3  # passes t1, t2, t3
                b_pass = i <= 2  # passes t1, t2
                trial_records.append({
                    "member": "cand-a", "task_id": f"t{i}", "trial": seed,
                    "pass": a_pass, "rubric_score": 4.0,
                })
                trial_records.append({
                    "member": "cand-b", "task_id": f"t{i}", "trial": seed,
                    "pass": b_pass, "rubric_score": 9.0,
                })

        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["cand-a", "cand-b"], tasks, trial_records, k=3)
        self.assertEqual(verdict, "keep-better")
        self.assertEqual(decided_by, "rule")
        self.assertEqual(kept, ["cand-a"])
        self.assertEqual(dropped, ["cand-b"])
        self.assertEqual(scores["cand-a"]["rule_score"], 0.75)
        self.assertEqual(scores["cand-b"]["rule_score"], 0.50)

    def test_judge_tie_break_picks_higher_judge_score(self):
        tasks = [
            judge_groups.Task("t1", "prompt 1", ("m1",), (), "rubric 1"),
            judge_groups.Task("t2", "prompt 2", ("m2",), (), "rubric 2"),
        ]
        # Both Cand A and Cand B pass t1 and fail t2 -> rule score tie (0.50 vs 0.50, gap 0.0 < 0.2)
        # Cand A has judge score 6.0, Cand B has judge score 8.5 -> judge gap = 2.5 >= 2.0
        # Judge tie-break picks Cand B with keep-better (decided_by judge)
        trial_records = [
            {"member": "cand-a", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 6.0},
            {"member": "cand-a", "task_id": "t2", "trial": 1, "pass": False, "rubric_score": 6.0},
            {"member": "cand-b", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 8.5},
            {"member": "cand-b", "task_id": "t2", "trial": 1, "pass": False, "rubric_score": 8.5},
        ]
        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["cand-a", "cand-b"], tasks, trial_records, k=1)
        self.assertEqual(verdict, "keep-better")
        self.assertEqual(kept, ["cand-b"])
        self.assertEqual(dropped, ["cand-a"])
        self.assertEqual(decided_by, "judge")

    def test_rule_gap_overrides_contrary_judge_score(self):
        tasks = [
            judge_groups.Task(f"t{i}", f"prompt {i}", ("m",), (), "rubric")
            for i in range(1, 5)
        ]
        # Cand A rule_score = 0.75, judge_score = 3.0
        # Cand B rule_score = 0.50, judge_score = 9.5 (contrary judge gap = 6.5 >= 2.0)
        # Rule gap is 0.25 >= 0.20 -> rule gap overrides judge score, Cand A wins (decided_by rule)
        trial_records = []
        for i in range(1, 5):
            trial_records.append({
                "member": "cand-a", "task_id": f"t{i}", "trial": 1,
                "pass": i <= 3, "rubric_score": 3.0,
            })
            trial_records.append({
                "member": "cand-b", "task_id": f"t{i}", "trial": 1,
                "pass": i <= 2, "rubric_score": 9.5,
            })
        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["cand-a", "cand-b"], tasks, trial_records, k=1)
        self.assertEqual(verdict, "keep-better")
        self.assertEqual(kept, ["cand-a"])
        self.assertEqual(dropped, ["cand-b"])
        self.assertEqual(decided_by, "rule")

    def test_keep_both_when_each_wins_one_task(self):
        tasks = [
            judge_groups.Task("t1", "prompt 1", ("m1",), (), "rubric 1"),
            judge_groups.Task("t2", "prompt 2", ("m2",), (), "rubric 2"),
        ]
        # Member A passes t1, fails t2 (wins t1)
        # Member B fails t1, passes t2 (wins t2)
        # Rule gap < 0.2, judge gap 0.5 < 2.0 -> keep-both with distinct wins
        trial_records = [
            {"member": "cand-a", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 8.0},
            {"member": "cand-a", "task_id": "t2", "trial": 1, "pass": False, "rubric_score": 2.0},
            {"member": "cand-b", "task_id": "t1", "trial": 1, "pass": False, "rubric_score": 3.0},
            {"member": "cand-b", "task_id": "t2", "trial": 1, "pass": True, "rubric_score": 8.5},
        ]
        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["cand-a", "cand-b"], tasks, trial_records, k=1)
        self.assertEqual(verdict, "keep-both")
        self.assertEqual(decided_by, "rule")
        self.assertCountEqual(kept, ["cand-a", "cand-b"])
        self.assertEqual(dropped, [])
        self.assertIn("distinct_use_vs", scores["cand-a"])
        self.assertEqual(scores["cand-a"]["distinct_use_vs"]["cand-b"], ["t1"])
        self.assertEqual(scores["cand-b"]["distinct_use_vs"]["cand-a"], ["t2"])

    def test_propose_merge_on_tie(self):
        tasks = [
            judge_groups.Task("t1", "prompt 1", ("m1",), (), "rubric 1"),
            judge_groups.Task("t2", "prompt 2", ("m2",), (), "rubric 2"),
        ]
        # Both pass t1, both fail t2; neither wins where other fails; judge gap = 1.0 < 2.0
        trial_records = [
            {"member": "cand-a", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 7.0},
            {"member": "cand-a", "task_id": "t2", "trial": 1, "pass": False, "rubric_score": 3.0},
            {"member": "cand-b", "task_id": "t1", "trial": 1, "pass": True, "rubric_score": 8.0},
            {"member": "cand-b", "task_id": "t2", "trial": 1, "pass": False, "rubric_score": 3.5},
        ]
        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group(["cand-a", "cand-b"], tasks, trial_records, k=1)
        self.assertEqual(verdict, "propose-merge")
        self.assertEqual(decided_by, "none")
        self.assertCountEqual(kept, ["cand-a", "cand-b"])
        self.assertEqual(dropped, [])

    def test_existing_skill_never_dropped_replace_proposed(self):
        tasks = [
            judge_groups.Task(f"t{i}", f"prompt {i}", ("m",), (), "rubric")
            for i in range(1, 5)
        ]
        existing = "skills/pre-mortem"
        cand = "owner/repo:skills/pre-mortem/SKILL.md"

        # Candidate passes 4/4 (rule_score = 1.0)
        # Existing passes 2/4 (rule_score = 0.5)
        # Candidate wins by gap 0.50 >= 0.20
        trial_records = []
        for i in range(1, 5):
            trial_records.append({
                "member": cand, "task_id": f"t{i}", "trial": 1, "pass": True, "rubric_score": 9.0,
            })
            trial_records.append({
                "member": existing, "task_id": f"t{i}", "trial": 1, "pass": (i <= 2), "rubric_score": 5.0,
            })

        verdict, kept, dropped, scores, decided_by = judge_groups.decide_group([existing, cand], tasks, trial_records, k=1)
        self.assertEqual(verdict, "replace-proposed")
        self.assertEqual(decided_by, "rule")
        self.assertEqual(kept, [cand])
        # Existing skill must NEVER be dropped
        self.assertNotIn(existing, dropped)
        self.assertEqual(dropped, [])


def _setup_resume_env(tmp: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    groups_tsv = tmp / "groups.tsv"
    trials_jsonl = tmp / "trials.jsonl"
    verdicts_tsv = tmp / "verdicts.tsv"
    eval_tasks_json = tmp / "eval-tasks.json"
    candidates_tsv = tmp / "candidates.tsv"
    checkouts = tmp / "checkouts"
    skills_dir = tmp / "skills"
    checkouts.mkdir()
    skills_dir.mkdir()

    with groups_tsv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["group_id", "members", "edges", "has_existing"], delimiter="\t")
        writer.writeheader()
        writer.writerow({"group_id": "1", "members": "cand-1,cand-2", "edges": "", "has_existing": "false"})

    with candidates_tsv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "path", "skill_name", "functions"], delimiter="\t")
        writer.writeheader()
        writer.writerow({"source": "s1", "path": "p1", "skill_name": "cand-1", "functions": "coding"})
        writer.writerow({"source": "s2", "path": "p2", "skill_name": "cand-2", "functions": "coding"})

    tasks_data = {
        "version": 1,
        "classes": {
            "coding": [{
                "id": "code-1", "prompt": "write a greeting module in python",
                "must_match": ["hello"], "must_not_match": ["bad"],
                "rubric": "friendly greeting",
            }]
        },
    }
    eval_tasks_json.write_text(json.dumps(tasks_data), encoding="utf-8")
    return groups_tsv, eval_tasks_json, candidates_tsv, verdicts_tsv, trials_jsonl, checkouts, skills_dir


class EndToEndAndResumeTests(unittest.TestCase):
    def test_resume_skips_done_trials(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            groups_tsv, eval_tasks_json, candidates_tsv, verdicts_tsv, trials_jsonl, checkouts, skills_dir = (
                _setup_resume_env(Path(tmp_dir))
            )
            chat_counter = 0

            def fake_chat(sys_prompt: str, user_prompt: str, temp: float, seed: int) -> str:
                nonlocal chat_counter
                chat_counter += 1
                return "hello world"

            def fake_judge(prompt: str) -> float:
                return 8.0

            run_kwargs = {
                "groups_tsv": groups_tsv, "eval_tasks_json": eval_tasks_json,
                "checkouts": checkouts, "skills_dir": skills_dir,
                "verdicts_out": verdicts_tsv, "trials_out": trials_jsonl,
                "candidates_tsv": candidates_tsv, "chat_fn": fake_chat,
                "judge_fn": fake_judge, "k": 1,
            }

            # First run: 2 calls
            judge_groups.run(**run_kwargs)
            self.assertEqual(chat_counter, 2)
            self.assertTrue(trials_jsonl.exists())
            self.assertEqual(len(trials_jsonl.read_text(encoding="utf-8").strip().splitlines()), 2)
            self.assertTrue(verdicts_tsv.exists())

            with verdicts_tsv.open(encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter="\t")
                self.assertIn("decided_by", reader.fieldnames)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertIn(rows[0]["decided_by"], ("rule", "judge", "none"))

            # Second run: all trials done -> 0 new chat calls
            judge_groups.run(**run_kwargs)
            self.assertEqual(chat_counter, 2)
            self.assertEqual(len(trials_jsonl.read_text(encoding="utf-8").strip().splitlines()), 2)


class EvalTasksJsonTests(unittest.TestCase):
    def test_eval_tasks_json_validity_and_prompt_disjointness(self):
        tasks_file = REPO_ROOT / "tools" / "eval-tasks.json"
        self.assertTrue(tasks_file.is_file(), f"missing {tasks_file}")
        with tasks_file.open(encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("version"), 1)
        classes = data.get("classes", {})
        functions_keys = set(classify.FUNCTIONS.keys()) - {"none"}

        self.assertEqual(set(classes.keys()), functions_keys)

        for cls_name, task_list in classes.items():
            self.assertEqual(len(task_list), 3, f"class {cls_name} should have exactly 3 tasks")
            for t in task_list:
                self.assertIn("id", t)
                prompt = t.get("prompt", "")
                self.assertTrue(prompt, f"Task {t['id']} has empty prompt")

                must_match = t.get("must_match", [])
                must_not_match = t.get("must_not_match", [])
                self.assertIsInstance(must_match, list)
                self.assertIsInstance(must_not_match, list)
                self.assertGreaterEqual(len(must_match), 2, f"Task {t['id']} has fewer than 2 must_match assertions")
                self.assertLessEqual(len(must_match), 4, f"Task {t['id']} has more than 4 must_match assertions")

                for pat in must_match:
                    try:
                        compiled = re.compile(pat, re.IGNORECASE)
                    except re.error as e:
                        self.fail(f"Task {t['id']} invalid must_match regex {pat}: {e}")
                    self.assertIsNone(
                        compiled.search(prompt),
                        f"Task {t['id']} prompt matches its own must_match pattern {pat!r}:\n  prompt: {prompt!r}"
                    )

                for pat in must_not_match:
                    try:
                        compiled = re.compile(pat, re.IGNORECASE)
                    except re.error as e:
                        self.fail(f"Task {t['id']} invalid must_not_match regex {pat}: {e}")
                    self.assertIsNone(
                        compiled.search(prompt),
                        f"Task {t['id']} prompt matches its own must_not_match pattern {pat!r}:\n  prompt: {prompt!r}"
                    )


if __name__ == "__main__":
    unittest.main()
