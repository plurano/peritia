#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for judge_groups.py; no test calls a live model."""
import csv
import json
import math
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
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


def make_tasks(count=3):
    return [judge_groups.Task(f"t{i}", f"prompt {i}", ("hello",), (), "rubric") for i in range(count)]


def records_for(outcomes, rubric=None):
    return [{"member": member, "task_id": f"t{i}", "trial": j + 1,
             "pass": passed, "rubric_score": (rubric or {}).get(member, 5.0)}
            for member, tasks in outcomes.items() for i, passes in enumerate(tasks)
            for j, passed in enumerate(passes)]


class ScoringAndVerdictTests(unittest.TestCase):
    def decide(self, outcomes, rubric=None):
        return judge_groups.decide_group(list(outcomes), make_tasks(), records_for(outcomes, rubric))

    def test_pass_at_k_vs_pass_pow_k(self):
        result = self.decide({"a": [[True, False, False], [True] * 3, [False] * 3]})
        self.assertEqual(result[0], "keep-better")
        self.assertAlmostEqual(result[3]["a"]["pass_at_k"], 2 / 3, places=3)
        self.assertAlmostEqual(result[3]["a"]["pass_pow_k"], 1 / 3, places=3)

    def test_two_tasks_vs_one_is_person_decides(self):
        result = self.decide({"a": [[True] * 3, [True] * 3, [False] * 3],
                              "b": [[True] * 3, [False] * 3, [False] * 3]})
        self.assertEqual(result[0], "person-decides")
        self.assertEqual(result[1:3], (["a", "b"], []))
        self.assertEqual(result[4], "none")

    def test_partial_trial_split_is_not_distinct_use(self):
        result = self.decide({"a": [[True, True, False], [True, False, False], [False] * 3],
                              "b": [[True, False, False], [True, True, False], [False] * 3]})
        self.assertEqual(result[0], "person-decides")

    def test_keep_both_requires_opposite_unanimous_tasks(self):
        result = self.decide({"a": [[True] * 3, [False] * 3, [False] * 3],
                              "b": [[False] * 3, [True] * 3, [False] * 3]})
        self.assertEqual(result[:3], ("keep-both", ["a", "b"], []))
        self.assertEqual(result[3]["a"]["distinct_use_vs"], {"b": ["t0"]})
        self.assertEqual(result[3]["b"]["distinct_use_vs"], {"a": ["t1"]})

    def test_one_pass_vs_zero_is_not_unanimous(self):
        result = self.decide({"a": [[True, False, False], [False] * 3, [False] * 3],
                              "b": [[False] * 3, [True, False, False], [False] * 3]})
        self.assertEqual(result[0], "person-decides")

    def test_one_trial_is_not_three(self):
        outcomes = {"a": [[True], [False], [False]], "b": [[False], [True], [False]]}
        result = judge_groups.decide_group(list(outcomes), make_tasks(), records_for(outcomes), k=1)
        self.assertEqual(result[0], "person-decides")

    def test_judge_score_never_breaks_rule_tie(self):
        result = self.decide({"a": [[True] * 3, [False] * 3, [False] * 3],
                              "b": [[True] * 3, [False] * 3, [False] * 3]}, {"a": 0, "b": 10})
        self.assertEqual(result[0], "person-decides")
        self.assertEqual(result[3]["b"]["judge_score"], 10)

    def test_rule_gap_ignores_contrary_judge(self):
        result = self.decide({"a": [[True] * 3] * 3, "b": [[False] * 3] * 3}, {"a": 0, "b": 10})
        self.assertEqual(result[:3], ("keep-better", ["a"], ["b"]))
        self.assertEqual(result[4], "rule")

    def test_gap_boundary(self):
        for other_passes, verdict in ((17, "person-decides"), (16, "keep-better")):
            with self.subTest(gap=(50 - other_passes) / 100):
                outcomes = {"a": [[i < 50] * 3 for i in range(100)],
                            "b": [[i < other_passes] * 3 for i in range(100)]}
                result = judge_groups.decide_group(list(outcomes), make_tasks(100), records_for(outcomes))
                self.assertEqual(result[0], verdict)

    def test_existing_skill_never_dropped_replace_proposed(self):
        result = self.decide({"skills/example": [[False] * 3] * 3, "a": [[True] * 3] * 3})
        self.assertEqual(result[:3], ("replace-proposed", ["a"], []))

    def test_no_tasks_or_incomplete_trials_require_person(self):
        for tasks, records in (([], []), (make_tasks(), []),
                               (make_tasks(), records_for({"a": [[True] * 3] * 3}))):
            with self.subTest(tasks=tasks, records=records):
                result = judge_groups.decide_group(["a", "b"], tasks, records)
                self.assertEqual(result[0], "person-decides")
                self.assertEqual(result[2], [])

    def test_duplicate_trials_are_not_evidence(self):
        records = records_for({"a": [[True] * 3] * 3, "b": [[False] * 3] * 3})
        records[1]["trial"] = 1
        self.assertEqual(judge_groups.decide_group(["a", "b"], make_tasks(), records)[0], "person-decides")

    def test_distinct_pair_does_not_drop_unproven_third_member(self):
        result = self.decide({"a": [[True] * 3, [False] * 3, [False] * 3],
                              "b": [[False] * 3, [True] * 3, [False] * 3],
                              "c": [[True] * 3, [False] * 3, [False] * 3]})
        self.assertEqual(result[:3], ("person-decides", ["a", "b", "c"], []))


class CopyCollapseTests(unittest.TestCase):
    def setUp(self):
        self.original = "anthropics/skills:skills/pdf/SKILL.md"
        self.copy = "ComposioHQ/awesome-claude-skills:pdf/SKILL.md"
        self.items = {self.original: judge_groups.Item(self.original, "upstream-blob", "PDF_tool", ""),
                      self.copy: judge_groups.Item(self.copy, "copy-blob", "pdf-tool", "")}
        self.ranks = {s.repository: s.rank for s in judge_groups.load_sources(judge_groups.DEFAULT_SOURCES)}

    def collapse(self, cosine=0.99, metadata=None, ranks=None, members=None):
        embeddings = {"upstream-blob": [1, 0], "copy-blob": [cosine, math.sqrt(1 - cosine ** 2)]}
        return judge_groups.collapse_copies(members or [self.copy, self.original], self.items,
                                            metadata or {}, embeddings, ranks or self.ranks)

    def test_anthropics_original_beats_composio_copy(self):
        kept, copies = self.collapse()
        self.assertEqual(kept, [self.original])
        self.assertEqual(copies, {self.copy: f"copy-of:{self.original}"})
        self.assertEqual(self.collapse(members=[self.original, self.copy]), (kept, copies))

    def test_source_repo_overrides_rank_without_name_or_embedding(self):
        kept, copies = judge_groups.collapse_copies(
            [self.copy, self.original], {},
            {self.original: {"source_repo": "https://github.com/ComposioHQ/awesome-claude-skills.git"}},
            {}, self.ranks)
        self.assertEqual(kept, [self.copy])
        self.assertEqual(copies, {self.original: f"copy-of:{self.copy}"})

    def test_all_source_fields_recognise_original(self):
        for field in ("source", "source_repo", "license_source", "upstream", "origin", "repository", "homepage"):
            with self.subTest(field=field):
                self.assertEqual(self.collapse(0.90, {self.copy: {field: "anthropics/skills"}})[0], [self.original])

    def test_same_name_cosine_point_nine_is_not_copy(self):
        self.assertEqual(self.collapse(0.90), ([self.copy, self.original], {}))

    def test_cosine_boundary(self):
        self.assertEqual(self.collapse(0.97)[0], [self.original])
        self.assertEqual(self.collapse(0.9699)[1], {})

    def test_equal_rank_uses_lexical_source(self):
        self.assertEqual(self.collapse(ranks={key: 1 for key in self.ranks})[0], [self.copy])

    def test_missing_cache_does_not_infer_copy(self):
        self.assertEqual(judge_groups.collapse_copies([self.copy, self.original], self.items, {}, {}, self.ranks)[1], {})

    def test_provenance_must_match_whole_repository(self):
        for source in ("anthropics/skills-extra", "other/skills"):
            with self.subTest(source=source):
                self.assertEqual(self.collapse(0.9, {self.copy: {"source_repo": source}})[1], {})

    def test_different_names_are_not_embedding_copies(self):
        self.items[self.copy] = judge_groups.Item(self.copy, "copy-blob", "other", "")
        self.assertEqual(self.collapse()[1], {})


class PrimaryClassTests(unittest.TestCase):
    def test_majority(self):
        self.assertEqual(judge_groups.primary_class(["a", "b", "c"],
                         {"a": {"coding", "testing"}, "b": {"testing"}, "c": {"research"}}), "testing")

    def test_ties_follow_taxonomy(self):
        self.assertEqual(judge_groups.primary_class(["a", "b"],
                         {"a": {"coding"}, "b": {"ci-release"}}), "coding")

    def test_no_function_has_no_primary(self):
        self.assertEqual(judge_groups.primary_class(["a"], {"a": {"none"}}), "")


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
                "id": f"code-{i}", "prompt": "write a greeting module in python",
                "must_match": ["hello"], "must_not_match": ["bad"],
                "rubric": "friendly greeting",
            } for i in range(3)]
        },
    }
    eval_tasks_json.write_text(json.dumps(tasks_data), encoding="utf-8")
    return groups_tsv, eval_tasks_json, candidates_tsv, verdicts_tsv, trials_jsonl, checkouts, skills_dir


def setup_copy_env(tmp):
    paths = _setup_resume_env(tmp)
    groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
    rows = []
    for source, blob, function in (("anthropics/skills", "original", "coding"),
                                   ("ComposioHQ/awesome-claude-skills", "copy", "testing")):
        skill = checkouts / source.replace("/", "__") / "pdf" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: pdf\ndescription: {blob}\n---\n{blob} content\n")
        rows.append({"source": source, "path": "pdf/SKILL.md", "skill_name": "pdf", "blob_id": blob,
                     "functions": function, "verdict": "candidate"})
    with candidates.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    members = [f"{row['source']}:{row['path']}" for row in rows]
    groups.write_text("group_id\tmembers\n1\t" + ",".join(members) + "\n")
    cache = tmp / "cache.json"
    cache.write_text(json.dumps({"original": [1, 0], "copy": [0.99, math.sqrt(1 - 0.99 ** 2)]}))
    return paths, members, cache


class EndToEndAndResumeTests(unittest.TestCase):
    def test_cli_uses_cache_and_never_calls_models_for_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            paths, members, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            chat, judge = Mock(return_value="hello"), Mock(return_value=9.0)
            with patch.object(judge_groups.JudgeHarness, "chat", chat), patch.object(judge_groups.JudgeHarness, "judge", judge):
                status = judge_groups.main(["run", "--groups", str(groups), "--eval-tasks", str(tasks),
                                           "--candidates", str(candidates), "--classified", str(Path(folder) / "absent"),
                                           "--checkouts", str(checkouts), "--skills-dir", str(skills),
                                           "--out", str(verdicts), "--trials-out", str(trials), "--cache", str(cache),
                                           "--base-url", "http://127.0.0.1:9999/v1", "--model", "test-model",
                                           "--judge-model", "test-judge"])
            self.assertEqual(status, 0)
            self.assertEqual(chat.call_count, 9)
            self.assertEqual(judge.call_count, 9)
            self.assertTrue(all("original content" in call.args[0] for call in chat.call_args_list))
            with verdicts.open() as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["primary_class"], "coding")
            self.assertEqual(json.loads(row["copies"]), {members[1]: f"copy-of:{members[0]}"})
            records = [json.loads(line) for line in trials.read_text().splitlines()]
            self.assertEqual({r["member"] for r in records}, {members[0]})

    def test_only_majority_class_tasks_run(self):
        with tempfile.TemporaryDirectory() as folder:
            groups, tasks, candidates, verdicts, trials, checkouts, skills = _setup_resume_env(Path(folder))
            candidates.write_text(candidates.read_text().replace("coding", "coding;testing", 1).replace("cand-2\tcoding", "cand-2\ttesting"))
            data = json.loads(tasks.read_text())
            data["classes"]["testing"] = [{"id": f"test-{i}", "prompt": f"test prompt {i}", "must_match": ["hello"]}
                                           for i in range(3)]
            tasks.write_text(json.dumps(data))
            chat = Mock(return_value="hello")
            row = judge_groups.run(groups, tasks, checkouts, skills, verdicts, trials, candidates,
                                   chat_fn=chat, judge_fn=lambda prompt: 8)[0]
            self.assertEqual(row["primary_class"], "testing")
            self.assertEqual(chat.call_count, 18)
            self.assertEqual({call.args[1] for call in chat.call_args_list}, {f"test prompt {i}" for i in range(3)})

    def test_existing_copy_is_replacement_proposal_never_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            paths, members, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            existing = skills / "pdf"
            existing.mkdir()
            (existing / "SKILL.md").write_text("---\nname: pdf\nsource_repo: anthropics/skills\n---\nExisting content\n")
            groups.write_text("group_id\tmembers\n1\t" + members[0] + ",skills/pdf\n")
            chat = Mock(return_value="hello")
            row = judge_groups.run(groups, tasks, checkouts, skills, verdicts, trials, candidates,
                                   chat_fn=chat, judge_fn=lambda prompt: 8, cache_path=cache)[0]
            self.assertEqual(row["verdict"], "replace-proposed")
            self.assertEqual(row["dropped"], "")
            self.assertEqual(json.loads(row["copies"]), {"skills/pdf": f"copy-of:{members[0]}"})
            self.assertTrue((existing / "SKILL.md").exists())
            self.assertEqual(chat.call_count, 9)

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

            # First run: 2 members, 3 primary tasks
            judge_groups.run(**run_kwargs)
            self.assertEqual(chat_counter, 6)
            self.assertTrue(trials_jsonl.exists())
            self.assertEqual(len(trials_jsonl.read_text(encoding="utf-8").strip().splitlines()), 6)
            self.assertTrue(verdicts_tsv.exists())

            with verdicts_tsv.open(encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter="\t")
                self.assertIn("decided_by", reader.fieldnames)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertIn(rows[0]["decided_by"], ("rule", "none"))

            # Second run: all trials done -> 0 new chat calls
            judge_groups.run(**run_kwargs)
            self.assertEqual(chat_counter, 6)
            self.assertEqual(len(trials_jsonl.read_text(encoding="utf-8").strip().splitlines()), 6)


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
