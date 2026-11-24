#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for judge_groups.py; no test calls a live model."""
import csv
import io
import json
import math
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import classify
import input_identity
import judge_groups
import overlap

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
        embeddings = {self.original: [1, 0], self.copy: [cosine, math.sqrt(1 - cosine ** 2)]}
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
    """Write two distinct skills and three distinct task prompts in one group."""
    groups_tsv = tmp / "groups.tsv"
    trials_jsonl = tmp / "trials.jsonl"
    verdicts_tsv = tmp / "verdicts.tsv"
    eval_tasks_json = tmp / "eval-tasks.json"
    candidates_tsv = tmp / "candidates.tsv"
    checkouts = tmp / "checkouts"
    skills_dir = tmp / "skills"
    checkouts.mkdir()
    skills_dir.mkdir()
    for member in ("cand-1", "cand-2"):
        (checkouts / member).write_text(member, encoding="utf-8")

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
                "id": f"code-{i}", "prompt": f"write a greeting module in python, variant {i}",
                "must_match": ["hello"], "must_not_match": ["bad"],
                "rubric": "friendly greeting",
            } for i in range(3)]
        },
    }
    eval_tasks_json.write_text(json.dumps(tasks_data), encoding="utf-8")
    return groups_tsv, eval_tasks_json, candidates_tsv, verdicts_tsv, trials_jsonl, checkouts, skills_dir


def setup_copy_env(tmp):
    """Build two near copies and a validated cache at their serving endpoint."""
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
    overlap.get_embeddings(overlap.load_candidates(candidates, checkouts),
                           lambda texts: [[1, 0], [0.99, math.sqrt(1 - 0.99 ** 2)]],
                           cache, model="embed-model", provider="http://127.0.0.1:9999/v1")
    return paths, members, cache


class EndToEndAndResumeTests(unittest.TestCase):
    def test_cli_uses_cache_and_never_calls_models_for_copy(self):
        """Use matching cached embeddings to judge only the original skill."""
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
                                           "--judge-model", "test-judge", "--embed-model", "embed-model",
                                           "--embed-dims", "2"])
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
        """Retain an existing skill even when its source names an original."""
        with tempfile.TemporaryDirectory() as folder:
            paths, members, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            existing = skills / "pdf"
            existing.mkdir()
            (existing / "SKILL.md").write_text("---\nname: pdf\nsource_repo: anthropics/skills\n---\nExisting content\n")
            groups.write_text("group_id\tmembers\n1\t" + members[0] + ",skills/pdf\n")
            chat = Mock(return_value="hello")
            row = judge_groups.run(groups, tasks, checkouts, skills, verdicts, trials, candidates,
                                   chat_fn=chat, judge_fn=lambda prompt: 8, cache_path=cache,
                                   embed_model="embed-model", base_url="http://127.0.0.1:9999/v1")[0]
            self.assertEqual(row["verdict"], "replace-proposed")
            self.assertEqual(row["dropped"], "")
            self.assertEqual(json.loads(row["copies"]), {"skills/pdf": f"copy-of:{members[0]}"})
            self.assertTrue((existing / "SKILL.md").exists())
            self.assertEqual(chat.call_count, 9)

    def test_distinct_embed_endpoint_selects_cached_copies(self):
        """Resolve embeddings at a separate endpoint for direct and CLI callers."""
        with tempfile.TemporaryDirectory() as folder:
            paths, members, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            row = judge_groups.run(groups, tasks, checkouts, skills, verdicts, trials, candidates,
                                   chat_fn=Mock(return_value="hello"), judge_fn=Mock(return_value=8.0),
                                   cache_path=cache, embed_model="embed-model", base_url="https://chat.test/v1",
                                   embed_base_url="http://127.0.0.1:9999/v1")[0]
            self.assertEqual(json.loads(row["copies"]), {members[1]: f"copy-of:{members[0]}"})
            args = judge_groups.parse_args(["run", "--base-url", "https://chat.test/v1", "--model", "answer",
                                            "--judge-model", "judge", "--embed-base-url", "https://embed.test/v1"])
            self.assertEqual(args.embed_base_url, "https://embed.test/v1")

    def test_cli_reports_malformed_cached_vector(self):
        """Return the cache path and input hash for a malformed vector."""
        with tempfile.TemporaryDirectory() as folder:
            paths, _, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            data = json.loads(cache.read_text())
            key = input_identity.text_sha256("pdf: original")
            next(iter(data["namespaces"].values()))["vectors"][key]["vector"] = "damaged"
            cache.write_text(json.dumps(data))
            args = ["run", "--groups", str(groups), "--eval-tasks", str(tasks), "--candidates", str(candidates),
                    "--checkouts", str(checkouts), "--skills-dir", str(skills), "--out", str(verdicts),
                    "--trials-out", str(trials), "--cache", str(cache), "--base-url", "http://127.0.0.1:9999/v1",
                    "--model", "answer", "--judge-model", "judge", "--embed-model", "embed-model"]
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(judge_groups.main(args), 1)
            self.assertIn(str(cache), stderr.getvalue())
            self.assertIn(key, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_other_embedding_model_does_not_infer_copy(self):
        """Warn and retain both members when a cache belongs to another model."""
        with tempfile.TemporaryDirectory() as folder:
            paths, _, cache = setup_copy_env(Path(folder))
            groups, tasks, candidates, verdicts, trials, checkouts, skills = paths
            chat, judge = Mock(return_value="hello"), Mock(return_value=8.0)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                row = judge_groups.run(groups, tasks, checkouts, skills, verdicts, trials, candidates,
                                       chat_fn=chat, judge_fn=judge, cache_path=cache, embed_model="other-model",
                                       base_url="http://127.0.0.1:9999/v1", k=1)[0]
            self.assertEqual(json.loads(row["copies"]), {})
            self.assertEqual((chat.call_count, judge.call_count), (6, 6))
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            self.assertIn("other-model", stderr.getvalue())
            self.assertIn(str(cache), stderr.getvalue())

    def test_cache_cli_requires_model(self):
        """Reject a cache without a specified embedding model."""
        stderr = io.StringIO()
        with (patch.dict("os.environ", {"PERITIA_LLM_EMBED_MODEL": ""}),
              redirect_stderr(stderr), self.assertRaises(SystemExit) as error):
            judge_groups.parse_args(["run", "--cache", "cache.json"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--cache requires --embed-model", stderr.getvalue())


class TrialIdentityTests(unittest.TestCase):
    """Check answer, rule grade and model judge invalidation independently."""

    def setUp(self):
        """Create a two-member run with independently identifiable prompts and answers."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        groups, self.tasks, candidates, self.verdicts, self.trials, self.checkouts, skills = (
            _setup_resume_env(Path(temp.name))
        )
        self.chat = Mock(return_value="hello world")
        self.judge = Mock(return_value=8.0)
        self.options = {
            "groups_tsv": groups, "eval_tasks_json": self.tasks, "checkouts": self.checkouts,
            "skills_dir": skills, "verdicts_out": self.verdicts, "trials_out": self.trials,
            "candidates_tsv": candidates, "chat_fn": self.chat, "judge_fn": self.judge,
            "k": 2, "model": "model-one", "judge_model": "judge-one", "base_url": "", "temperature": 0.7,
        }

    def first_run(self):
        """Write twelve trials and clear the counters so the next run is measurable."""
        judge_groups.run(**self.options)
        self.assert_calls(12, 12)
        self.chat.reset_mock()
        self.judge.reset_mock()
        return self.trials.read_bytes()

    def assert_calls(self, chat, judge):
        """Check both fake model call counts for a single run."""
        self.assertEqual((self.chat.call_count, self.judge.call_count), (chat, judge))

    def new_rows(self, previous=b""):
        """Decode appended rows after checking that the prior bytes are intact."""
        current = self.trials.read_bytes()
        self.assertTrue(current.startswith(previous))
        return [json.loads(line) for line in current[len(previous):].splitlines()]

    def set_task_field(self, field, value):
        """Replace one field of the first task without changing its id."""
        data = json.loads(self.tasks.read_text())
        data["classes"]["coding"][0][field] = value
        self.tasks.write_text(json.dumps(data), encoding="utf-8")

    def test_duplicate_task_id_is_refused_before_running_trials(self):
        """Reject two trial inputs with the same task ID and different prompts."""
        self.set_task_field("id", "code-1")
        with self.assertRaisesRegex(ValueError, r"duplicate trial task ID 'code-1'"):
            judge_groups.run(**self.options)
        self.assert_calls(0, 0)
        self.assertFalse(self.trials.exists())

    def test_duplicate_group_member_is_refused_before_running_trials(self):
        """Reject repeated group-member inputs within or across rows."""
        groups = self.options["groups_tsv"]
        for rows in ("1\tcand-1,cand-1\n", "1\tcand-1\n1\tcand-1\n"):
            with self.subTest(rows=rows):
                groups.write_text(f"group_id\tmembers\n{rows}", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, r"duplicate trial group/member ID \('1', 'cand-1'\)"):
                    judge_groups.run(**self.options)
                self.assert_calls(0, 0)
                self.assertFalse(self.trials.exists())
    def test_identical_answers_still_record_each_slot(self):
        """Persist six logical slots per seed and reuse inference across them."""
        (self.checkouts / "cand-2").write_text("cand-1")
        data = json.loads(self.tasks.read_text())
        for task in data["classes"]["coding"]:
            task["prompt"] = "Identical prompt"
        self.tasks.write_text(json.dumps(data))
        verdicts = judge_groups.run(**self.options)
        self.assert_calls(2, 2)
        records = self.new_rows()
        self.assertEqual(len(records), 12)
        self.assertEqual({(r["group_id"], r["member"], r["task_id"], r["trial"]) for r in records},
                         {("1", f"cand-{m}", f"code-{t}", s)
                          for m in (1, 2) for t in range(3) for s in (1, 2)})
        scores = json.loads(verdicts[0]["scores"])
        self.assertEqual({member: score["pass_pow_k"] for member, score in scores.items()},
                         {"cand-1": 1.0, "cand-2": 1.0})
        before = self.trials.read_bytes()
        self.chat.reset_mock()
        self.judge.reset_mock()
        self.assertEqual(judge_groups.run(**self.options), verdicts)
        self.assert_calls(0, 0)
        self.assertEqual(self.trials.read_bytes(), before)

    def test_changing_grader_version_regrades_without_judging(self):
        """Bind local grades to the grader version independently from the judge."""
        before = self.first_run()
        with (patch.object(judge_groups, "GRADER_VERSION", "next"),
              patch.object(judge_groups, "grade_answer", wraps=judge_groups.grade_answer) as grade):
            judge_groups.run(**self.options)
        self.assertEqual(grade.call_count, 12)
        self.assert_calls(0, 0)
        self.assertEqual(len(self.new_rows(before)), 12)

    def test_unknown_trial_version_never_resumes(self):
        """Keep fingerprinted records from an unsupported version without using them."""
        self.first_run()
        records = self.new_rows()
        for record in records:
            record["record_version"] = 99
        before = "".join(json.dumps(row) + "\n" for row in records).encode()
        self.trials.write_bytes(before)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            judge_groups.run(**self.options)
        self.assert_calls(12, 12)
        self.assertEqual(len(self.new_rows(before)), 12)
        self.assertIn("unsupported trial records", stderr.getvalue())

    def test_trial_history_over_cap_is_refused(self):
        """Refuse an overlong history before invoking either model."""
        self.first_run()
        with (patch.object(judge_groups, "MAX_TRIAL_ROWS", 1, create=True),
              self.assertRaisesRegex(ValueError, r"trials\.jsonl.*cap.*1")):
            judge_groups.run(**self.options)
        self.assert_calls(0, 0)

    def test_trials_lock_blocks_a_second_run_before_model_calls(self):
        """Hold the trials file lock across the run's read and write operations."""
        with (patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001),
              input_identity.output_lock(self.trials),
              self.assertRaisesRegex(TimeoutError, r"trials\.jsonl\.lock")):
            judge_groups.run(**self.options)
        self.assert_calls(0, 0)
        self.assertFalse(self.verdicts.exists())

    def test_torn_jsonl_is_quarantined_before_resuming_and_appending(self):
        """Preserve complete trials and move an incomplete JSONL record byte for byte."""
        before = self.first_run()
        tail = b'{"answer":"partial\xc3'
        self.trials.write_bytes(before + tail)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            judge_groups.run(**(self.options | {"judge_model": "second"}))
        archive = self.trials.with_name("trials.torn-1.jsonl")
        self.assertEqual(archive.read_bytes(), tail)
        self.assertIn(f"{len(tail)} bytes to {archive}", stderr.getvalue())
        self.assertEqual(len(self.new_rows(before)), 12)
        self.assert_calls(0, 12)
        self.judge.reset_mock()
        judge_groups.run(**(self.options | {"judge_model": "second"}))
        self.assert_calls(0, 0)

    def test_trial_history_compacts_at_cap_and_remains_resumable(self):
        """Compact duplicate slot history while retaining existing answers and grades."""
        before = self.first_run()
        before += before.splitlines(keepends=True)[0]
        self.trials.write_bytes(before)
        with patch.object(judge_groups, "MAX_TRIAL_ROWS", 13):
            self.set_task_field("must_match", ["new grading rule"])
            judge_groups.run(**self.options)
            self.assertEqual(self.trials.with_name("trials.history-1.jsonl").read_bytes(), before)
            self.assertLessEqual(len(self.new_rows()), 13)
            self.assert_calls(0, 0)
            paths = self.trials, self.verdicts
            active = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
            judge_groups.run(**self.options)
            self.assert_calls(0, 0)
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], active)

    def test_regrade_at_cap_compacts_existing_trial_slots(self):
        """Compact at the cap when a regrade only updates slots the active history already holds."""
        before = self.first_run()
        with self.verdicts.open(newline="", encoding="utf-8") as handle:
            initial_scores = json.loads(next(csv.DictReader(handle, delimiter="\t"))["scores"])
        self.set_task_field("must_match", ["new grading rule"])
        with patch.object(judge_groups, "MAX_TRIAL_ROWS", 12):
            judge_groups.run(**self.options)
            self.assertEqual(self.trials.with_name("trials.history-1.jsonl").read_bytes(), before)
            active = self.new_rows()
            self.assertLessEqual(len(active), 12)
            self.assert_calls(0, 0)
            regraded = [rec for rec in active if rec["task_id"] == "code-0"]
            untouched = [rec for rec in active if rec["task_id"] != "code-0"]
            self.assertEqual(
                {(rec["member"], rec["task_id"], rec["trial"]) for rec in regraded},
                {(f"cand-{m}", "code-0", t) for m in (1, 2) for t in (1, 2)},
            )
            self.assertTrue(all(rec["pass"] is False for rec in regraded))
            self.assertTrue(all(rec["pass"] is True for rec in untouched))
            judge_groups.run(**self.options)
            self.assert_calls(0, 0)
            with self.verdicts.open(newline="", encoding="utf-8") as handle:
                updated_scores = json.loads(next(csv.DictReader(handle, delimiter="\t"))["scores"])
            for member in ("cand-1", "cand-2"):
                self.assertAlmostEqual(updated_scores[member]["rule_score"], 2 / 3, places=4)
                self.assertAlmostEqual(updated_scores[member]["pass_at_k"], 2 / 3, places=4)
                self.assertAlmostEqual(updated_scores[member]["pass_pow_k"], 2 / 3, places=4)
                self.assertLess(updated_scores[member]["rule_score"], initial_scores[member]["rule_score"])

    def test_distinct_trial_slot_at_cap_refuses_append(self):
        """Refuse to discard a distinct active trial slot to make room for a new one."""
        before = self.first_run()
        with (patch.object(judge_groups, "MAX_TRIAL_ROWS", 12),
              self.assertRaisesRegex(ValueError, r"trials\.jsonl.*cap of 12 rows")):
            judge_groups.run(**(self.options | {"k": 3}))
        self.assertEqual(self.trials.read_bytes(), before)
        self.assertFalse(self.trials.with_name("trials.history-1.jsonl").exists())

    def test_answer_layout_preserves_existing_identity(self):
        """Keep the answer identity when the actual baseline message layout is unchanged."""
        expected = input_identity.fingerprint("answer", {
            "skill_text_sha256": input_identity.text_sha256("skill"),
            "task_prompt_sha256": input_identity.text_sha256("task"),
            "model": "model", "provider": "provider", "temperature": 0.7, "seed": 1,
            "chat_shape": "system=skill,user=task",
        })
        self.assertEqual(judge_groups.answer_fingerprint("skill", "task", "model", "provider", 0.7, 1), expected)

    def test_answer_shape_fingerprint_matches_sent_messages(self):
        """Change the sent roles and require new answer identities."""
        self.first_run()
        client = Mock()
        judge_groups.JudgeHarness(client, "model", "judge").chat("skill", "task", 0.7, 1)
        client.chat.assert_called_once_with("model", [{"role": "system", "content": "skill"},
                                                      {"role": "user", "content": "task"}],
                                            temperature=0.7, seed=1)
        changed = [{"role": "user", "content": "skill"}, {"role": "system", "content": "task"}]
        with patch.object(judge_groups, "answer_messages", return_value=changed, create=True):
            judge_groups.run(**self.options)
            judge_groups.JudgeHarness(client, "model", "judge").chat("skill", "task", 0.7, 1)
        self.assert_calls(12, 12)
        self.assertEqual(client.chat.call_args.args[1], changed)

    def test_judge_binds_answer_hash(self):
        """Change the cached answer while keeping the rendered judge prompt fixed."""
        with patch.object(judge_groups, "make_judge_prompt", return_value="constant judge prompt"):
            self.first_run()
            records = self.new_rows()
            records[0]["answer"] = "edited actual answer"
            self.trials.write_text("".join(json.dumps(row) + "\n" for row in records))
            judge_groups.run(**self.options)
        self.assert_calls(0, 1)

    def test_structured_temperature_changes_rubric_identity(self):
        """Rejudge existing answers when only the client default temperature changes."""
        self.first_run()
        with patch("llm_client.DEFAULT_CHAT_TEMPERATURE", 0.1, create=True):
            judge_groups.run(**self.options)
            self.assert_calls(0, 12)
            self.judge.reset_mock()
            judge_groups.run(**self.options)
            self.assert_calls(0, 0)

    def test_judge_system_prompt_changes_rubric_identity(self):
        """Rejudge existing answers after a judge system prompt edit."""
        self.first_run()
        with patch.object(judge_groups, "JUDGE_SYSTEM_PROMPT", "changed judge instruction"):
            judge_groups.run(**self.options)
        self.assert_calls(0, 12)

    def test_judge_schema_changes_rubric_identity(self):
        """Rejudge existing answers after a judge schema edit."""
        self.first_run()
        schema = judge_groups.judge_schema()
        schema["properties"]["reason"]["maxLength"] += 1
        with patch.object(judge_groups, "judge_schema", return_value=schema):
            judge_groups.run(**self.options)
        self.assert_calls(0, 12)

    def test_resume_skips_done_trials(self):
        """Reuse matching trials with no append and neither model called."""
        before = self.first_run()
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)
        self.assertEqual(self.trials.read_bytes(), before)

    def test_changed_answer_model_runs_all_trials(self):
        """Generate and judge every answer under a new answer model."""
        before = self.first_run()
        row = judge_groups.run(**(self.options | {"model": "model-two"}))[0]
        self.assert_calls(12, 12)
        added = self.new_rows(before)
        self.assertEqual(len(added), 12)
        self.assertEqual({rec["model"] for rec in added}, {"model-two"})
        self.assertEqual({rec["record_version"] for rec in added}, {2})
        self.assertEqual({rec["answer"] for rec in added}, {"hello world"})
        self.assertEqual(row["model"], "model-two")

    def test_changed_provider_runs_all_trials(self):
        """Invalidate answers when the endpoint changes, even with injected callables."""
        before = self.first_run()
        base = "https://fixture-user:fixture-pass@provider.test:123/v1/"
        judge_groups.run(**(self.options | {"base_url": base}))
        self.assert_calls(12, 12)
        self.assertEqual({rec["provider"] for rec in self.new_rows(before)}, {"https://provider.test:123/v1"})

    def test_changed_temperature_runs_all_trials(self):
        """Distinguish temperatures that differ beyond the first decimal."""
        before = self.first_run()
        judge_groups.run(**(self.options | {"temperature": 0.70000001}))
        self.assert_calls(12, 12)
        self.assertEqual({rec["temperature"] for rec in self.new_rows(before)}, {0.70000001})

    def test_changed_skill_runs_only_its_trials(self):
        """Change the skill text while retaining the member id."""
        before = self.first_run()
        (self.checkouts / "cand-1").write_text("edited skill", encoding="utf-8")
        expected_prompts = {f"write a greeting module in python, variant {i}" for i in range(3)}

        def check_chat(skill_text: str, task_prompt: str, temperature: float, seed: int) -> str:
            """Require the edited skill text and current task prompts."""
            if (skill_text != "edited skill" or task_prompt not in expected_prompts
                    or temperature != 0.7 or seed not in {1, 2}):
                self.fail(f"stale chat inputs: {(skill_text, task_prompt, temperature, seed)!r}")
            return f"hello from {skill_text} ({seed})"

        def check_judge(prompt: str) -> float:
            """Require the regenerated answer from the edited skill."""
            if "hello from edited skill" not in prompt:
                self.fail(f"stale judge prompt: {prompt!r}")
            return 9.0

        self.chat.side_effect = check_chat
        self.judge.side_effect = check_judge
        judge_groups.run(**self.options)
        self.assert_calls(6, 6)
        self.assertEqual(
            {(call.args[0], call.args[1], call.args[2], call.args[3]) for call in self.chat.call_args_list},
            {("edited skill", prompt, 0.7, seed) for prompt in expected_prompts for seed in (1, 2)},
        )
        added = self.new_rows(before)
        self.assertEqual({rec["member"] for rec in added}, {"cand-1"})
        self.assertEqual({rec["rubric_score"] for rec in added}, {9.0})
        self.chat.reset_mock()
        self.judge.reset_mock()
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)

    def test_changed_task_prompt_runs_only_its_trials(self):
        """Regenerate and judge the changed prompt for both members."""
        before = self.first_run()
        self.set_task_field("prompt", "different task prompt")

        def check_chat(skill_text: str, task_prompt: str, temperature: float, seed: int) -> str:
            """Require the updated task prompt for each member."""
            if (skill_text not in {"cand-1", "cand-2"} or task_prompt != "different task prompt"
                    or temperature != 0.7 or seed not in {1, 2}):
                self.fail(f"stale chat inputs: {(skill_text, task_prompt, temperature, seed)!r}")
            return f"hello for {task_prompt} ({skill_text}, {seed})"

        def check_judge(prompt: str) -> float:
            """Require the updated task prompt in the judge prompt."""
            if "<task_prompt>\ndifferent task prompt\n</task_prompt>" not in prompt:
                self.fail(f"stale judge prompt: {prompt!r}")
            return 7.5

        self.chat.side_effect = check_chat
        self.judge.side_effect = check_judge
        judge_groups.run(**self.options)
        self.assert_calls(4, 4)
        self.assertEqual(
            {(call.args[0], call.args[1], call.args[2], call.args[3]) for call in self.chat.call_args_list},
            {(member, "different task prompt", 0.7, seed) for member in ("cand-1", "cand-2") for seed in (1, 2)},
        )
        added = self.new_rows(before)
        self.assertEqual({rec["task_id"] for rec in added}, {"code-0"})
        self.assertEqual({rec["rubric_score"] for rec in added}, {7.5})
        self.chat.reset_mock()
        self.judge.reset_mock()
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)

    def test_changed_must_match_regrades_without_model_calls(self):
        """Regrade the affected task using stored answers on an assertion change."""
        before = self.first_run()
        self.set_task_field("must_match", ["does not match"])
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)
        added = self.new_rows(before)
        self.assertEqual(len(added), 4)
        self.assertEqual({rec["task_id"] for rec in added}, {"code-0"})
        self.assertEqual({rec["pass"] for rec in added}, {False})
        self.assertEqual({rec["answer"] for rec in added}, {"hello world"})

    def test_changed_must_not_match_regrades_without_model_calls(self):
        """Recheck forbidden patterns against each stored answer of the task."""
        before = self.first_run()
        self.set_task_field("must_not_match", ["world"])
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)
        added = self.new_rows(before)
        self.assertEqual(len(added), 4)
        self.assertEqual({rec["pass"] for rec in added}, {False})

    def test_changed_rubric_rejudges_only_its_trials(self):
        """Rescore one task's answers when its rubric changes."""
        before = self.first_run()
        self.set_task_field("rubric", "must test the greeting")

        def check_judge(prompt: str) -> float:
            """Require the updated rubric on the rejudged task."""
            if "<rubric>\nmust test the greeting\n</rubric>" not in prompt or "variant 0" not in prompt:
                self.fail(f"stale judge prompt: {prompt!r}")
            return 6.5

        self.judge.side_effect = check_judge
        judge_groups.run(**self.options)
        self.assert_calls(0, 4)
        self.assertTrue(
            all("<rubric>\nmust test the greeting\n</rubric>" in call.args[0] for call in self.judge.call_args_list)
        )
        added = self.new_rows(before)
        self.assertEqual({rec["task_id"] for rec in added}, {"code-0"})
        self.assertEqual({rec["rubric_score"] for rec in added}, {6.5})
        self.judge.reset_mock()
        judge_groups.run(**self.options)
        self.assert_calls(0, 0)

    def test_changed_judge_model_rescores_existing_answers(self):
        """Rescore every unchanged answer without calling the answer model."""
        before = self.first_run()
        row = judge_groups.run(**(self.options | {"judge_model": "judge-two"}))[0]
        self.assert_calls(0, 12)
        self.assertEqual({rec["judge_model"] for rec in self.new_rows(before)}, {"judge-two"})
        self.assertEqual(row["judge_model"], "judge-two")

    def test_legacy_trials_are_kept_but_never_resumed(self):
        """Keep the legacy bytes and use only current-model trials in verdicts."""
        legacy = b'{"group_id":"1","member":"cand-1","task_id":"code-0","trial":1,"model":"old-model"}\r\n'
        self.trials.write_bytes(legacy)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            row = judge_groups.run(**self.options)[0]
        self.assert_calls(12, 12)
        self.assertEqual(stderr.getvalue().splitlines(),
                         [f"1 legacy or unsupported trial records ignored (expected record version 2 and input fingerprints): {self.trials}"])
        self.assertEqual({rec["model"] for rec in self.new_rows(legacy)}, {"model-one"})
        self.assertEqual(row["model"], "model-one")
        self.assertNotIn("old-model", self.verdicts.read_text())

    def test_legacy_trial_without_final_newline_keeps_its_bytes(self):
        """Preserve an unterminated legacy record in the quarantine archive."""
        legacy = b'{"member":"cand-1","task_id":"code-0","trial":1,"model":"old-model"}'
        self.trials.write_bytes(legacy)
        with redirect_stderr(io.StringIO()):
            judge_groups.run(**self.options)
        self.assertEqual(self.trials.with_name("trials.torn-1.jsonl").read_bytes(), legacy)
        self.assertEqual(len(self.new_rows()), 12)
        self.chat.reset_mock()
        self.judge.reset_mock()
        with redirect_stderr(io.StringIO()):
            judge_groups.run(**self.options)
        self.assert_calls(0, 0)

    def test_answer_fingerprint_binds_every_answer_input(self):
        """Distinguish each changed answer input while keeping other inputs fixed."""
        values = {"skill_text": "skill", "task_prompt": "task", "model": "model", "provider": "provider",
                  "temperature": 0.7, "seed": 1}
        base = judge_groups.answer_fingerprint(**values)
        for key, value in (("skill_text", "edited"), ("task_prompt", "new prompt"), ("model", "different"),
                           ("provider", "elsewhere"), ("temperature", 0.70000001), ("seed", 2)):
            with self.subTest(key=key):
                self.assertNotEqual(judge_groups.answer_fingerprint(**(values | {key: value})), base)

    def test_answer_fingerprint_excludes_all_grade_inputs(self):
        """Keep answer identities when rubric, assertions and judge model alone change."""
        before = self.first_run()
        self.set_task_field("rubric", "new rubric")
        self.set_task_field("must_match", ["different"])
        self.set_task_field("must_not_match", ["world"])
        judge_groups.run(**(self.options | {"judge_model": "judge-two"}))
        self.assert_calls(0, 12)
        previous = {json.loads(line)["answer_fp"] for line in before.splitlines()}
        self.assertEqual({rec["answer_fp"] for rec in self.new_rows(before)}, previous)


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
