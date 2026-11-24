#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for the overlap detector and judge; no test calls a live model."""
import csv
import io
import json
import math
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import chdir, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import adopt_skills
import input_identity
import judge_groups
import llm_client
import overlap
from git_blob import git_blob_id
from input_identity import text_sha256

CANDIDATES_HEADER = [
    "blob_id", "source", "path", "skill_name", "functions", "domains",
    "match_clause", "licence", "licence_class", "licence_evidence", "verdict", "reason",
]


def write_candidates(folder: Path, rows: list[dict[str, str]]) -> Path:
    path = folder / "candidates.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDIDATES_HEADER, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CANDIDATES_HEADER})
    return path


def write_skill_file(folder: Path, rel_path: str, text: str) -> Path:
    target = folder / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def by_item_id(items, vectors):
    """Label ranking fixture vectors with their item IDs."""
    return {item.item_id: vectors[item.blob_id] for item in items if item.blob_id in vectors}


class OverlapRankingTests(unittest.TestCase):
    def test_nearest_k_picks_k_nearest_and_same_name(self):
        cand = overlap.Item("source/a:cand/SKILL.md", "b_cand", "plan_work", "planning skill")
        # 5 items with high similarity (ranks 1 to 5)
        top5 = [
            overlap.Item(f"source/a:top{i}/SKILL.md", f"b_top{i}", f"top_{i}", f"desc {i}")
            for i in range(1, 6)
        ]
        # 2 items with medium similarity (ranks 6 and 7)
        med2 = [
            overlap.Item(f"source/a:med{i}/SKILL.md", f"b_med{i}", f"med_{i}", f"desc med {i}")
            for i in range(1, 3)
        ]
        # 1 item with low similarity but identical normalised name ("PLAN-WORK" vs "plan_work")
        same_name = overlap.Item("source/a:same/SKILL.md", "b_same", "PLAN-WORK", "low similarity same name")

        all_items = [cand] + top5 + med2 + [same_name]

        # Vectors crafted so top5 are nearest, med2 are further, same_name is furthest
        embeddings = {
            "b_cand": [1.0, 0.0, 0.0],
            "b_top1": [0.99, 0.1, 0.0],
            "b_top2": [0.95, 0.2, 0.0],
            "b_top3": [0.90, 0.3, 0.0],
            "b_top4": [0.85, 0.4, 0.0],
            "b_top5": [0.80, 0.5, 0.0],
            "b_med1": [0.50, 0.5, 0.0],
            "b_med2": [0.40, 0.5, 0.0],
            "b_same": [0.10, 0.9, 0.0],
        }

        pairs = overlap.find_candidate_pairs([cand], all_items, by_item_id(all_items, embeddings), k=5)
        paired_ids = {p.b if p.a == cand.item_id else p.a for p in pairs}

        # Nearest 5 and same-name item are included
        for t in top5:
            self.assertIn(t.item_id, paired_ids)
        self.assertIn(same_name.item_id, paired_ids)

        # Non-matching medium similarity items beyond rank 5 are excluded
        for m in med2:
            self.assertNotIn(m.item_id, paired_ids)

        # Same-name item has name_match True
        same_pair = next(p for p in pairs if same_name.item_id in (p.a, p.b))
        self.assertTrue(same_pair.name_match)

    def test_ranking_finds_different_words_duplicate(self):
        cand1 = overlap.Item("source/a:plan/SKILL.md", "b1", "plan-feature",
                             "Create implementation plans for a feature before code")
        cand2 = overlap.Item("source/b:spec/SKILL.md", "b2", "spec-to-tasks",
                             "Decompose specification requirements into bite-sized units of work")
        other1 = overlap.Item("source/c:audio/SKILL.md", "b3", "audio-equalizer",
                              "Equalize audio frequencies and balance audio track volume")
        other2 = overlap.Item("source/d:db/SKILL.md", "b4", "db-migration",
                              "Run schema migrations on PostgreSQL databases safely")

        # cand1 and cand2 cosine similarity is 0.75 (< 0.9 threshold)
        embeddings = {
            "b1": [1.0, 0.0, 0.0],
            "b2": [0.75, 0.6614, 0.0],
            "b3": [0.10, 0.20, 0.97],
            "b4": [0.05, 0.10, 0.99],
        }

        all_items = [cand1, cand2, other1, other2]
        pairs = overlap.find_candidate_pairs([cand1], all_items, by_item_id(all_items, embeddings), k=5)
        pair = next((p for p in pairs if {p.a, p.b} == {cand1.item_id, cand2.item_id}), None)
        self.assertIsNotNone(pair)
        self.assertAlmostEqual(pair.cosine, 0.75, places=2)
        self.assertLess(pair.cosine, 0.90)

        # When judged same_use=True, they form a group
        rows = [{
            "a": pair.a, "b": pair.b, "cosine": f"{pair.cosine:.4f}",
            "rank": str(pair.rank), "name_match": "false", "same_use": "true",
            "reason": "both generate implementation task plans", "model": "fake",
        }]
        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 1)
        self.assertIn(cand1.item_id, groups[0]["members"])
        self.assertIn(cand2.item_id, groups[0]["members"])

    def test_self_and_same_blob_pairs_never_appear(self):
        cand = overlap.Item("source/a:cand/SKILL.md", "blob_shared", "cand", "desc")
        same_blob = overlap.Item("source/b:dup/SKILL.md", "blob_shared", "cand_dup", "desc")
        distinct = overlap.Item("source/c:other/SKILL.md", "blob_other", "other", "desc")

        all_items = [cand, same_blob, distinct]
        embeddings = {
            "blob_shared": [1.0, 0.0],
            "blob_other": [0.99, 0.01],
        }

        pairs = overlap.find_candidate_pairs([cand], all_items, by_item_id(all_items, embeddings), k=5)
        for p in pairs:
            self.assertNotEqual(p.a, p.b)
            self.assertNotEqual(p.item_a.blob_id, p.item_b.blob_id)

        paired_targets = {p.b if p.a == cand.item_id else p.a for p in pairs}
        self.assertNotIn(cand.item_id, paired_targets)
        self.assertNotIn(same_blob.item_id, paired_targets)
        self.assertIn(distinct.item_id, paired_targets)

    def test_pair_judged_once(self):
        cand1 = overlap.Item("s:c1/SKILL.md", "b1", "skill1", "desc 1")
        cand2 = overlap.Item("s:c2/SKILL.md", "b2", "skill2", "desc 2")
        all_items = [cand1, cand2]
        embeddings = {"b1": [1.0, 0.0], "b2": [0.9, 0.1]}

        pairs = overlap.find_candidate_pairs([cand1, cand2], all_items, by_item_id(all_items, embeddings), k=5)
        self.assertEqual(len(pairs), 1)

        calls = []

        def fake_judge(prompt: str) -> dict[str, object]:
            calls.append(prompt)
            return {"same_use": True, "reason": "identical"}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_overlap = tmp / "overlap.tsv"
            out_groups = tmp / "groups.tsv"
            cand_tsv = write_candidates(tmp, [
                {"blob_id": "b1", "source": "s", "path": "c1/SKILL.md", "skill_name": "skill1", "verdict": "candidate"},
                {"blob_id": "b2", "source": "s", "path": "c2/SKILL.md", "skill_name": "skill2", "verdict": "candidate"},
            ])
            checkouts = tmp / "checkouts"
            write_skill_file(checkouts, "s/c1/SKILL.md", "---\nname: skill1\ndescription: desc 1\n---\nbody")
            write_skill_file(checkouts, "s/c2/SKILL.md", "---\nname: skill2\ndescription: desc 2\n---\nbody")

            overlap.run(
                candidates_tsv=cand_tsv,
                checkouts=checkouts,
                skills_dir=tmp / "skills",
                overlap_out=out_overlap,
                groups_out=out_groups,
                embed_fn=lambda texts: [embeddings["b1"], embeddings["b2"]],
                judge_fn=fake_judge,
                judge_model="fake-model",
            )
            self.assertEqual(len(calls), 1)

    def test_normalised_ranking_equals_old_ranking_on_fixed_vector_set(self):
        cand = overlap.Item("cand", "b_cand", "cand_name", "cand desc")
        targets = [
            overlap.Item(f"t{i}", f"b_t{i}", f"name_{i}", f"desc_{i}")
            for i in range(1, 6)
        ]
        all_items = [cand] + targets
        fixed_vectors = {
            "b_cand": [3.0, 1.0, -2.0, 4.0],
            "b_t1": [1.5, 0.5, -1.0, 2.0],
            "b_t2": [2.0, -1.0, 1.0, 3.0],
            "b_t3": [-1.0, 0.5, 2.0, 1.0],
            "b_t4": [-3.0, -1.0, 2.0, -4.0],
            "b_t5": [0.2, 5.0, 1.0, -0.5],
        }

        def old_cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb)

        cand_v = fixed_vectors["b_cand"]
        old_ranked = []
        for t in targets:
            c = old_cosine(cand_v, fixed_vectors[t.blob_id])
            old_ranked.append((c, t.item_id))
        old_ranked.sort(key=lambda x: (-x[0], x[1]))

        pairs = overlap.find_candidate_pairs([cand], all_items, by_item_id(all_items, fixed_vectors), k=5)
        self.assertEqual(len(pairs), len(targets))
        pairs_by_rank = sorted(pairs, key=lambda p: p.rank)
        for i, pair in enumerate(pairs_by_rank):
            expected_cos, expected_id = old_ranked[i]
            paired_id = pair.b if pair.a == cand.item_id else pair.a
            self.assertEqual(paired_id, expected_id)
            self.assertEqual(pair.rank, i + 1)
            self.assertAlmostEqual(pair.cosine, expected_cos, places=6)


class OverlapGroupAndJudgeTests(unittest.TestCase):
    def test_groups_are_connected_components(self):
        item_a = overlap.Item("a", "ba", "a", "desc")
        item_b = overlap.Item("b", "bb", "b", "desc")
        item_c = overlap.Item("skills/c", "bc", "c", "desc", is_existing=True)
        item_d = overlap.Item("d", "bd", "d", "desc")
        item_e = overlap.Item("e", "be", "e", "desc")
        item_f = overlap.Item("f", "bf", "f", "desc")
        item_g = overlap.Item("g", "bg", "g", "desc")

        all_items = [item_a, item_b, item_c, item_d, item_e, item_f, item_g]
        rows = [
            {"a": "a", "b": "b", "same_use": "true"},
            {"a": "b", "b": "skills/c", "same_use": "true"},
            {"a": "d", "b": "e", "same_use": "true"},
            {"a": "f", "b": "g", "same_use": "false"},
        ]

        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 2)

        # Group 1: a, b, skills/c (contains existing skill)
        self.assertEqual(groups[0]["members"], "a,b,skills/c")
        self.assertEqual(groups[0]["edges"], "a|b;b|skills/c")
        self.assertEqual(groups[0]["has_existing"], "true")

        # Group 2: d, e (no existing skill)
        self.assertEqual(groups[1]["members"], "d,e")
        self.assertEqual(groups[1]["edges"], "d|e")
        self.assertEqual(groups[1]["has_existing"], "false")

    def test_edges_column_lists_exact_same_use_pairs(self):
        item_x = overlap.Item("x", "bx", "x", "desc x")
        item_y = overlap.Item("y", "by", "y", "desc y")
        item_z = overlap.Item("z", "bz", "z", "desc z")
        all_items = [item_x, item_y, item_z]
        rows = [
            {"a": "x", "b": "y", "same_use": "true"},
            {"a": "y", "b": "z", "same_use": "true"},
            {"a": "x", "b": "z", "same_use": "false"},
        ]
        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members"], "x,y,z")
        self.assertEqual(groups[0]["edges"], "x|y;y|z")
        self.assertNotIn("x|z", groups[0]["edges"].split(";"))

    def test_judge_system_prompt_and_schema(self):
        self.assertIn("same_use is true only when either skill could replace the other", overlap.JUDGE_SYSTEM_PROMPT)
        self.assertIn("Related topics, different steps of one workflow", overlap.JUDGE_SYSTEM_PROMPT)
        schema = overlap.judge_schema()
        self.assertEqual(schema["required"], ["same_use", "reason"])
        self.assertEqual(schema["properties"]["same_use"]["type"], "boolean")
        self.assertEqual(schema["properties"]["reason"]["type"], "string")

    def test_unjudged_answers_recorded_not_guessed(self):
        calls = []

        def failing_judge(prompt: str):
            calls.append(prompt)
            raise OSError("model unavailable")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cand_tsv = write_candidates(tmp, [
                {"blob_id": "b1", "source": "s", "path": "c1/SKILL.md", "skill_name": "c1", "verdict": "candidate"},
                {"blob_id": "b2", "source": "s", "path": "c2/SKILL.md", "skill_name": "c2", "verdict": "candidate"},
            ])
            checkouts = tmp / "checkouts"
            write_skill_file(checkouts, "s/c1/SKILL.md", "---\nname: c1\ndescription: d1\n---\n")
            write_skill_file(checkouts, "s/c2/SKILL.md", "---\nname: c2\ndescription: d2\n---\n")

            out_overlap = tmp / "overlap.tsv"
            out_groups = tmp / "groups.tsv"

            overlap.run(
                candidates_tsv=cand_tsv,
                checkouts=checkouts,
                skills_dir=tmp / "skills",
                overlap_out=out_overlap,
                groups_out=out_groups,
                embed_fn=lambda texts: [[1.0, 0.0], [0.99, 0.01]],
                judge_fn=failing_judge,
                judge_model="fail-model",
            )
            # Failed twice before giving up
            self.assertEqual(len(calls), overlap.ATTEMPTS)

            with out_overlap.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["same_use"], "unjudged")

            # Unjudged pairs are not assumed true; no groups formed
            with out_groups.open(newline="", encoding="utf-8") as handle:
                group_rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(group_rows), 0)

            rep = overlap.summary(out_overlap, out_groups, cand_tsv)
            self.assertIn("unjudged pairs: 1", rep)
            self.assertIn("overlap-free candidates: 2", rep)

    def test_build_groups_filters_pairs_outside_item_set(self):
        item_a = overlap.Item("a", "ba", "a", "desc a")
        item_c = overlap.Item("c", "bc", "c", "desc c")
        item_d = overlap.Item("d", "bd", "d", "desc d")
        # "dropped" is not in all_items
        all_items = [item_a, item_c, item_d]
        rows = [
            {"a": "a", "b": "dropped", "same_use": "true"},
            {"a": "dropped", "b": "c", "same_use": "true"},
            {"a": "a", "b": "d", "same_use": "true"},
        ]
        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members"], "a,d")
        self.assertEqual(groups[0]["edges"], "a|d")


class OverlapResumeTests(unittest.TestCase):
    """Exercise overlap judgment reuse through successive runs."""

    def setUp(self):
        """Write three skills with stable item IDs and distinct descriptions."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.out = self.folder / "overlap.tsv"
        self.groups = self.folder / "groups.tsv"
        self.checkouts = self.folder / "checkouts"
        self.rows = [
            {"blob_id": f"b{i}", "source": "s", "path": f"c{i}/SKILL.md",
             "skill_name": f"c{i}", "verdict": "candidate"}
            for i in range(1, 4)
        ]
        for i in range(1, 4):
            write_skill_file(self.checkouts, f"s/c{i}/SKILL.md", f"---\nname: c{i}\ndescription: desc{i}\n---\n")
        self.candidates = write_candidates(self.folder, self.rows)
        self.judge_calls = []

    def run_overlap(self, model="first", *, same_use=True, unjudged_skill=None, embed_fn=None, **kwargs):
        """Run overlap with three identical vectors and record the judge prompts."""
        self.judge_calls.clear()

        def judge(prompt):
            """Record the prompt and return the selected same-use value."""
            self.judge_calls.append(prompt)
            if same_use is None or f"name: {unjudged_skill}\n" in prompt:
                return {}
            return {"same_use": same_use, "reason": "fixture"}

        return overlap.run(
            candidates_tsv=self.candidates,
            checkouts=self.checkouts,
            skills_dir=self.folder / "skills",
            overlap_out=self.out,
            groups_out=self.groups,
            embed_fn=embed_fn or (lambda texts: [[1.0, 0.0] for _ in texts]),
            judge_fn=judge,
            judge_model=model,
            **kwargs,
        )

    def test_smaller_k_excludes_historical_group_and_unjudged_pair(self):
        """Project both group edges and standalone summary onto the current top k."""
        for same_use, old_groups, old_unjudged in ((True, 1, 0), (False, 0, 0), (None, 0, 1)):
            with self.subTest(same_use=same_use):
                self.out.unlink(missing_ok=True)

                def judge(prompt, *, same_use=same_use):
                    """Use the selected judgment for c2-c3 and mark other pairs distinct."""
                    if "name: c1\n" in prompt:
                        return {"same_use": False, "reason": "distinct"}
                    return {} if same_use is None else {"same_use": same_use, "reason": "same"}

                with patch("overlap.judge_one", wraps=overlap.judge_one) as requested:
                    overlap.run(self.candidates, self.checkouts, self.folder / "skills", self.out, self.groups,
                                lambda texts: [[1.0, 0.0] for _ in texts], judge, "judge", k=2)
                    self.assertEqual(requested.call_count, 3)
                self.assertIn(f"groups: {old_groups}", overlap.summary(self.out, self.groups, self.candidates))
                self.assertIn(f"unjudged pairs: {old_unjudged}", overlap.summary(self.out, self.groups, self.candidates))
                before = self.out.read_bytes()
                self.assertEqual(self.run_overlap(model="judge", k=1), (2, 0))
                self.assertEqual(self.judge_calls, [])
                self.assertEqual(self.out.read_bytes(), before)
                self.assertIn("unjudged pairs: 0", overlap.summary(self.out, self.groups, self.candidates))

    def test_new_embedding_model_projects_retained_pair_geometry(self):
        """Use current cosine and rank for a pair previously cached at rank two."""
        cache = self.folder / "embed.json"
        self.assertEqual(self.run_overlap(k=2, cache_path=cache, embed_model="before"), (3, 1))
        retained = ("s:c2/SKILL.md", "s:c3/SKILL.md")
        history = overlap.load_done_pairs(self.out)
        self.assertEqual((history[retained]["cosine"], history[retained]["rank"]), ("1.0000", "2"))
        before = self.out.read_bytes()
        with patch.object(overlap, "build_groups", wraps=overlap.build_groups) as build:
            self.assertEqual(self.run_overlap(k=1, cache_path=cache, embed_model="after",
                                              embed_fn=lambda _: [[0, 1], [1, 0], [0.8, 0.6]]), (2, 1))
        rows = {(r["a"], r["b"]): r for r in build.call_args.args[0]}
        self.assertNotIn(("s:c1/SKILL.md", "s:c2/SKILL.md"), rows)
        self.assertEqual((rows[retained]["cosine"], rows[retained]["rank"]), ("0.8000", "1"))
        self.assertEqual(self.judge_calls, [])
        self.assertEqual(self.out.read_bytes(), before)

    def test_embedding_model_change_at_fixed_k_drops_old_edge(self):
        """Exclude a former edge even when only the embedding model changes."""
        cache = self.folder / "embed.json"
        self.assertEqual(self.run_overlap(k=1, cache_path=cache, embed_model="before"), (2, 1))
        old_edge = "s:c1/SKILL.md|s:c2/SKILL.md"
        self.assertIn(old_edge, self.groups.read_text())
        before = self.out.read_bytes()
        self.assertEqual(self.run_overlap(k=1, cache_path=cache, embed_model="after",
                                          embed_fn=lambda _: [[0, 1], [1, 0], [0.8, 0.6]]), (2, 1))
        self.assertNotIn(old_edge, self.groups.read_text())
        self.assertEqual(len(self.judge_calls), 1)
        self.assertNotIn("name: c1\n", self.judge_calls[0])
        self.assertTrue(self.out.read_bytes().startswith(before))

    def test_pair_user_prompt_change_rejudges_unchanged_blobs(self):
        """Invalidate a judgment when only its rendered user prompt changes."""
        self.run_overlap()
        render = overlap.make_judge_prompt
        with patch.object(overlap, "make_judge_prompt", side_effect=lambda a, b: render(a, b) + " changed"):
            self.assertEqual(self.run_overlap(same_use=False), (3, 0))
        self.assertEqual(len(self.judge_calls), 3)

    def test_structured_temperature_changes_pair_identity(self):
        """Reuse judgments until the client changes its default temperature."""
        self.run_overlap()
        self.run_overlap()
        self.assertEqual(self.judge_calls, [])
        with patch("llm_client.DEFAULT_CHAT_TEMPERATURE", 0.1, create=True):
            self.run_overlap()
            self.assertEqual(len(self.judge_calls), 3)
            self.run_overlap()
            self.assertEqual(self.judge_calls, [])

    def test_pair_history_over_cap_is_refused(self):
        """Detect excess pair history before reusing any rows."""
        self.run_overlap()
        with (patch.object(overlap, "MAX_PAIRS", 2),
              self.assertRaisesRegex(ValueError, r"overlap\.tsv.*cap.*2")):
            overlap.load_done_pairs(self.out)

    def test_overlap_output_lock_blocks_inference_and_publication(self):
        """Hold the history lock for the whole overlap run."""
        with (patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001),
              input_identity.output_lock(self.out),
              self.assertRaisesRegex(TimeoutError, r"overlap\.tsv\.lock")):
            self.run_overlap()
        self.assertEqual(self.judge_calls, [])
        self.assertFalse(self.groups.exists())

    def test_pair_history_compacts_at_cap(self):
        """Retain all archived rows and resume the compacted pair judgments."""
        with patch.object(overlap, "MAX_PAIRS", 5):
            self.run_overlap()
            self.run_overlap(model="second", k=1)
            original = self.out.read_bytes()
            write_skill_file(self.checkouts, "s/c3/SKILL.md", "---\nname: c3\ndescription: Edited\n---\n")
            self.run_overlap(model="second", k=1)
            self.assertEqual(len(self.judge_calls), 1)
            self.assertEqual(self.out.with_name("overlap.history-1.tsv").read_bytes(), original)
            self.assertEqual(len(self.out.read_bytes().splitlines()), 4)
            paths = (self.out, self.groups, overlap._current_pairs_path(self.groups), overlap._generation_path(self.groups))
            before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
            self.run_overlap(model="second", k=1)
            self.assertEqual(self.judge_calls, [])
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_distinct_pair_history_at_cap_refuses_append(self):
        """Compact existing pair updates at cap and keep full distinct pair history intact on overflow."""
        with patch.object(overlap, "MAX_PAIRS", 3):
            self.run_overlap()
            self.assertEqual(self.run_overlap(model="second"), (3, 1))
            self.assertTrue(self.out.with_name("overlap.history-1.tsv").is_file())
            self.rows.append({"blob_id": "b4", "source": "s", "path": "c4/SKILL.md",
                              "skill_name": "c4", "verdict": "candidate"})
            write_skill_file(self.checkouts, "s/c4/SKILL.md", "---\nname: c4\ndescription: desc4\n---\n")
            write_candidates(self.folder, self.rows)
            before = self.out.read_bytes(), self.out.stat().st_mtime_ns
            with self.assertRaisesRegex(ValueError, r"overlap\.tsv.*cap of 3 rows"):
                self.run_overlap(model="second")
            self.assertEqual((self.out.read_bytes(), self.out.stat().st_mtime_ns), before)
            self.assertFalse(self.out.with_name("overlap.history-4.tsv").exists())

    def test_publication_interruptions_leave_no_readable_mixed_generation(self):
        """Refuse both an incomplete first generation and a stale manifest on update."""
        rerun = r"python3 tools/overlap\.py run"
        replace_file = os.replace
        for failure_point in ("pairs", "manifest"):
            with self.subTest(failure_point=failure_point):
                pairs = overlap._current_pairs_path(self.groups)
                manifest = overlap._generation_path(self.groups)
                target = pairs if failure_point == "pairs" else manifest
                same_use = failure_point == "manifest"

                def interrupt(source, destination, target=target):
                    """Fail before replacing the selected component."""
                    if destination == target:
                        raise OSError("interrupted publication")
                    replace_file(source, destination)

                with (patch.object(overlap.os, "replace", side_effect=interrupt),
                      self.assertRaisesRegex(OSError, "interrupted publication")):
                    self.run_overlap(model=failure_point, same_use=same_use)
                for read in (lambda: overlap.summary(self.out, self.groups),
                             lambda: judge_groups.load_groups(self.groups),
                             lambda: adopt_skills._read_group_rows(self.groups)):
                    with self.assertRaisesRegex(ValueError, rerun):
                        read()
                self.run_overlap(model=failure_point, same_use=same_use)
                self.assertEqual(self.judge_calls, [])
                self.assertIn(f"groups: {int(same_use)}", overlap.summary(self.out, self.groups))

    def test_summary_refuses_hash_mismatch_in_either_published_file(self):
        """Verify both published byte streams even when they remain valid TSV."""
        self.run_overlap()
        for path in (self.groups, overlap._current_pairs_path(self.groups)):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                with self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run"):
                    overlap.summary(self.out, self.groups)
                path.write_bytes(before)
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups))

    def test_groups_and_pair_snapshot_require_manifest(self):
        """Reject an otherwise complete generation after the manifest is lost."""
        self.run_overlap()
        overlap._generation_path(self.groups).unlink()
        with self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run"):
            overlap.summary(self.out, self.groups)

    def test_generation_manifest_version_rejects_bool_and_non_int(self):
        """Accept integer manifest version and reject Boolean, float and string values."""
        self.run_overlap()
        manifest = overlap._generation_path(self.groups)
        valid = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups))
        for bad_version in (True, False, "1", 1.0, None):
            with self.subTest(version=bad_version):
                manifest.write_text(json.dumps(valid | {"version": bad_version}) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run"):
                    overlap.summary(self.out, self.groups)
        manifest.write_text(json.dumps(valid) + "\n", encoding="utf-8")
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups))

    def test_resume_skips_judged_pairs(self):
        """Reuse all three judgments when inputs match and preserve the TSV bytes."""
        self.assertEqual(self.run_overlap(), (3, 1))
        original = self.out.read_bytes()
        self.assertEqual(self.run_overlap(), (3, 1))
        self.assertEqual(self.judge_calls, [])
        self.assertEqual(self.out.read_bytes(), original)

    def test_changed_judge_model_rejudges_every_pair(self):
        """Rejudge each pair with the new model and exclude stale edges from groups."""
        self.assertEqual(self.run_overlap(), (3, 1))
        original = self.out.read_bytes()
        self.assertEqual(self.run_overlap(model="second", same_use=False), (3, 0))
        self.assertEqual(len(self.judge_calls), 3)
        self.assertTrue(self.out.read_bytes().startswith(original))
        pairs = overlap.load_done_pairs(self.out)
        self.assertEqual({row["model"] for row in pairs.values()}, {"second"})

    def test_changed_judge_provider_rejudges_every_pair(self):
        """Rejudge pairs when the same model routes to another endpoint."""
        self.assertEqual(self.run_overlap(provider="https://one.test/v1"), (3, 1))
        self.assertEqual(self.run_overlap(provider="https://two.test/v1", same_use=False), (3, 0))
        self.assertEqual(len(self.judge_calls), 3)

    def test_changed_system_prompt_and_schema_rejudge_every_pair(self):
        """Rejudge pairs when the instructions or response schema changes."""
        self.assertEqual(self.run_overlap(), (3, 1))
        original = self.out.read_bytes()
        schema = overlap.judge_schema()
        schema["properties"]["reason"]["maxLength"] = 100
        for name, replacement in (
            ("JUDGE_SYSTEM_PROMPT", "New judge instructions"),
            ("judge_schema", lambda: schema),
        ):
            with self.subTest(input=name):
                self.out.write_bytes(original)
                with patch.object(overlap, name, replacement):
                    self.assertEqual(self.run_overlap(same_use=False), (3, 0))
                self.assertEqual(len(self.judge_calls), 3)

    def test_changed_pair_seed_rejudges_every_pair(self):
        """Rejudge pairs when the seed alone changes."""
        self.assertEqual(self.run_overlap(), (3, 1))
        with patch.object(llm_client, "DEFAULT_CHAT_SEED", 2):
            self.assertEqual(self.run_overlap(), (3, 1))
        self.assertEqual(len(self.judge_calls), 3)

    def test_changed_skill_rejudges_only_its_pairs(self):
        """Rejudge two prompts after changing one description under the same ID."""
        self.assertEqual(self.run_overlap(), (3, 1))
        write_skill_file(self.checkouts, "s/c1/SKILL.md", "---\nname: c1\ndescription: revised\n---\n")
        self.assertEqual(self.run_overlap(same_use=False), (3, 1))
        self.assertEqual(len(self.judge_calls), 2)
        self.assertTrue(all("description: revised" in prompt for prompt in self.judge_calls))
        with self.groups.open(newline="", encoding="utf-8") as handle:
            groups = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["edges"] for row in groups], ["s:c2/SKILL.md|s:c3/SKILL.md"])

    def test_body_edit_rejudges_pairs_even_with_unchanged_prompt(self):
        """Rejudge pairs whose on-disk skill blob changed behind the candidate ID."""
        self.assertEqual(self.run_overlap(), (3, 1))
        write_skill_file(self.checkouts, "s/c1/SKILL.md", "---\nname: c1\ndescription: desc1\n---\nEdited body.\n")
        self.assertEqual(self.run_overlap(same_use=False), (3, 1))
        self.assertEqual(len(self.judge_calls), 2)

    def test_legacy_overlap_tsv_is_archived_without_reusing_edges(self):
        """Archive the original header and row byte-for-byte before rejudging."""
        original = (
            b"a\tb\tcosine\trank\tname_match\tsame_use\treason\tmodel\r\n"
            b"s:c1/SKILL.md\ts:c2/SKILL.md\t0.9\t1\tfalse\ttrue\tlegacy\told-model\r\n"
        )
        self.out.write_bytes(original)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_overlap(same_use=False), (3, 0))
        self.assertEqual(len(self.judge_calls), 3)
        self.assertEqual(self.out.with_name("overlap.legacy-1.tsv").read_bytes(), original)
        self.assertNotIn(b"old-model", self.out.read_bytes())

    def test_resumed_run_with_dropped_candidate_excludes_dropped_from_groups_and_summary(self):
        """Keep historical rows while limiting groups and counts to current items."""
        self.assertEqual(self.run_overlap(unjudged_skill="c2"), (3, 1))
        self.assertIn("unjudged pairs: 2", overlap.summary(self.out, self.groups, self.candidates))
        self.rows[1]["verdict"] = "refused"
        write_candidates(self.folder, self.rows)
        original = self.out.read_bytes()
        self.assertEqual(self.run_overlap(), (1, 1))
        self.assertEqual(self.judge_calls, [])
        self.assertEqual(self.out.read_bytes(), original)
        with self.groups.open(newline="", encoding="utf-8") as handle:
            groups = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["members"] for row in groups], ["s:c1/SKILL.md,s:c3/SKILL.md"])
        report = overlap.summary(self.out, self.groups, self.candidates)
        self.assertIn("candidates: 2", report)
        self.assertIn("overlap-free candidates: 0", report)
        self.assertIn("unjudged pairs: 0", report)

    def test_summary_counts_one_row_per_pair_after_rejudging(self):
        """Replace historical unjudged counts with the latest status per pair."""
        self.assertEqual(self.run_overlap(same_use=None), (3, 0))
        self.assertEqual(self.run_overlap(model="second", same_use=None), (3, 0))
        self.assertEqual(len(self.out.read_text(encoding="utf-8").splitlines()), 7)
        report = overlap.summary(self.out, self.groups, self.candidates)
        self.assertIn("unjudged pairs: 3", report)

    def test_separate_manifests_for_same_stem_and_legacy_manifest_compatibility(self):
        """Keep distinct manifests per full groups filename and verify legacy fallback hashes."""
        groups_csv = self.folder / "groups.csv"
        self.assertEqual(self.run_overlap(same_use=True), (3, 1))
        overlap.run(
            candidates_tsv=self.candidates, checkouts=self.checkouts, skills_dir=self.folder / "skills",
            overlap_out=self.folder / "overlap-csv.tsv", groups_out=groups_csv,
            embed_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            judge_fn=lambda _prompt: {"same_use": False, "reason": "distinct"}, judge_model="csv-judge",
        )
        tsv_manifest = self.folder / "groups.tsv.generation.json"
        csv_manifest = self.folder / "groups.csv.generation.json"
        self.assertTrue(tsv_manifest.is_file())
        self.assertTrue(csv_manifest.is_file())
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups, self.candidates))
        self.assertIn("groups: 0", overlap.summary(self.folder / "overlap-csv.tsv", groups_csv, self.candidates))
        self.assertEqual(len(judge_groups.load_groups(self.groups)), 1)
        self.assertEqual(len(adopt_skills._read_group_rows(groups_csv)), 0)

        legacy_manifest = self.folder / "groups.generation.json"
        tsv_manifest.replace(legacy_manifest)
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups, self.candidates))

        csv_manifest.unlink()
        with self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run"):
            overlap.summary(self.folder / "overlap-csv.tsv", groups_csv, self.candidates)

        overlap.run(
            candidates_tsv=self.candidates, checkouts=self.checkouts, skills_dir=self.folder / "skills",
            overlap_out=self.folder / "overlap-csv.tsv", groups_out=groups_csv,
            embed_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            judge_fn=lambda _prompt: {"same_use": False, "reason": "distinct"}, judge_model="csv-judge",
        )
        self.assertTrue(csv_manifest.is_file())
        self.assertTrue(legacy_manifest.is_file())
        self.assertIn("groups: 1", overlap.summary(self.out, self.groups, self.candidates))

        tsv_manifest.write_text('{"version":1,"groups_sha256":"bad","pairs_sha256":"bad"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run"):
            overlap.summary(self.out, self.groups, self.candidates)


class OverlapEmbeddingIdentityTests(unittest.TestCase):
    """Exercise embedding namespace selection and legacy cache migration."""

    def setUp(self):
        """Create an empty cache path and a stable skill item."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / "embeddings.json"
        self.item = overlap.Item("item1", "b1", "name1", "desc1")
        self.embed_calls = []

    def embed(self, texts):
        """Record all requested texts and return a three-dimensional vector for each."""
        self.embed_calls.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    def load_cache_json(self):
        """Read the stored cache document for structural assertions."""
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def test_interleaved_cache_writers_keep_both_vectors_and_namespaces(self):
        """Persist another run's additions made after this run reads the cache."""
        second = replace(self.item, item_id="second", description="second description")

        def interleave(texts):
            """Save two new vectors to disk before the outer writer can save."""
            overlap.get_embeddings([second], lambda _: [[1.0, 0.0, 0.0]], self.cache, model="embed")
            overlap.get_embeddings([second], lambda _: [[0.0, 1.0, 0.0]], self.cache, model="other")
            return self.embed(texts)

        overlap.get_embeddings([self.item], interleave, self.cache, model="embed")
        with patch.object(overlap, "save_cache", wraps=overlap.save_cache) as save:
            result = overlap.get_embeddings([self.item, second], self.embed, self.cache, model="embed")
            save.assert_not_called()
        self.assertEqual(result, {"item1": [0.1, 0.2, 0.3], "second": [1.0, 0.0, 0.0]})
        self.assertEqual(len(self.load_cache_json()["namespaces"]), 2)

    def test_cache_lock_blocks_independent_writer(self):
        """Prevent a writer from publishing while an independent holder locks the cache."""
        with (patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001),
              input_identity.output_lock(self.cache),
              self.assertRaisesRegex(TimeoutError, r"embeddings\.json\.lock")):
            overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.assertFalse(self.cache.exists())

    def test_conflicting_cache_writers_keep_the_published_vector(self):
        """Reject a different vector for the same namespace and input hash."""
        namespace = overlap.EmbeddingSpace("embed", "", overlap.EMBED_PREPROCESSING).key(3)

        def interleave(texts):
            """Publish another vector for the same input before the outer save."""
            overlap.get_embeddings([self.item], lambda _: [[1.0, 0.0, 0.0]], self.cache, model="embed")
            return self.embed(texts)

        with self.assertRaisesRegex(ValueError, rf"{namespace}.*conflicting vector"):
            overlap.get_embeddings([self.item], interleave, self.cache, model="embed")
        result = overlap.load_embeddings(self.cache, [self.item], "embed", "")
        self.assertEqual(result, {"item1": [1.0, 0.0, 0.0]})

    def test_embeddings_cache_hit_makes_no_call(self):
        """Reuse a matching vector without contacting the embedding model."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.embed_calls.clear()
        res = overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(res, {self.item.item_id: [0.1, 0.2, 0.3]})

    def test_changed_embed_model_keeps_both_namespaces(self):
        """Treat model names as independent cache identities and retain the original vectors."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="first")
        first = self.load_cache_json()
        self.embed_calls.clear()
        overlap.get_embeddings([self.item], self.embed, self.cache, model="second")
        self.assertEqual(self.embed_calls, ["name1: desc1"])
        namespaces = self.load_cache_json()["namespaces"]
        self.assertEqual({ns["model"] for ns in namespaces.values()}, {"first", "second"})
        for key, namespace in first["namespaces"].items():
            self.assertEqual(namespaces[key], namespace)

    def test_changed_provider_keeps_both_namespaces(self):
        """Treat the provider endpoint as part of the embedding namespace."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", provider="https://one.test/v1")
        self.embed_calls.clear()
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", provider="https://two.test/v1")
        self.assertEqual(self.embed_calls, ["name1: desc1"])
        self.assertEqual(len(self.load_cache_json()["namespaces"]), 2)

    def test_changed_embed_text_under_same_blob_misses(self):
        """Cache new text independently and retain the old input's vector."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.embed_calls.clear()
        revised = replace(self.item, description="revised")
        overlap.get_embeddings([revised], self.embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, ["name1: revised"])
        self.assertEqual(overlap.load_embeddings(self.cache, [self.item], "embed", ""),
                         {self.item.item_id: [0.1, 0.2, 0.3]})
        keys = next(iter(self.load_cache_json()["namespaces"].values()))["vectors"]
        self.assertEqual(set(keys), {text_sha256("name1: revised"), text_sha256("name1: desc1")})

    def test_same_census_blob_uses_both_current_embeddings(self):
        """Retain the untouched vector and separate pair and copy evidence after an edit."""
        checkouts = self.cache.parent / "checkouts"
        a, b = "owner/a", "owner/b"
        candidates = write_candidates(self.cache.parent, [
            {"source": source, "path": "SKILL.md", "blob_id": "shared", "verdict": "candidate"}
            for source in (a, b)
        ])
        for source in (a, b):
            write_skill_file(checkouts, f"{source.replace('/', '__')}/SKILL.md",
                             "---\nname: review\ndescription: unchanged\n---\n")
        items = overlap.load_candidates(candidates, checkouts)
        def embed(texts):
            """Return orthogonal vectors for the two same-name inputs."""
            self.embed_calls.extend(texts)
            return [[1.0, 0.0] if text.endswith("unchanged") else [0.0, 1.0] for text in texts]

        overlap.get_embeddings(items, embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, ["review: unchanged"])
        write_skill_file(checkouts, "owner__b/SKILL.md", "---\nname: review\ndescription: edited\n---\n")

        self.embed_calls.clear()
        items = overlap.load_candidates(candidates, checkouts)
        vectors = overlap.get_embeddings(items, embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, ["review: edited"])
        self.assertEqual(vectors, {items[0].item_id: [1.0, 0.0], items[1].item_id: [0.0, 1.0]})
        self.assertEqual([pair.cosine for pair in overlap.find_candidate_pairs(items, items, vectors)], [0.0])
        by_id = {item.item_id: item for item in items}
        self.assertEqual(judge_groups.collapse_copies(list(by_id), by_id, {}, vectors, {a: 1, b: 2}),
                         (list(by_id), {}))

    def test_blob_keyed_version_two_cache_reuses_exact_text(self):
        """Read a previously stored text fingerprint even when the item has a different blob ID."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        data = self.load_cache_json()
        vectors = next(iter(data["namespaces"].values()))["vectors"]
        vectors["old-blob-id"] = vectors.pop(text_sha256(overlap.make_embed_text(self.item)))
        self.cache.write_text(json.dumps(data))
        self.embed_calls.clear()
        self.assertEqual(overlap.get_embeddings([self.item], self.embed, self.cache, model="embed"),
                         {self.item.item_id: [0.1, 0.2, 0.3]})
        self.assertEqual(self.embed_calls, [])

    def test_swapped_text_keyed_entries_miss_while_legacy_blob_entry_hits(self):
        """Refuse text-keyed entries whose key and text_sha256 disagree while keeping legacy hits."""
        second = replace(self.item, item_id="item2", blob_id="b2", skill_name="name2", description="desc2")

        def distinct_embed(texts):
            """Return distinct unit vectors per input text."""
            self.embed_calls.extend(texts)
            return [[1.0, 0.0, 0.0] if "name1" in text else [0.0, 1.0, 0.0] for text in texts]

        overlap.get_embeddings([self.item, second], distinct_embed, self.cache, model="embed")
        key_a = text_sha256(overlap.make_embed_text(self.item))
        key_b = text_sha256(overlap.make_embed_text(second))
        data = self.load_cache_json()
        vectors = next(iter(data["namespaces"].values()))["vectors"]
        vectors[key_a]["text_sha256"], vectors[key_b]["text_sha256"] = key_b, key_a
        self.cache.write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(overlap.load_embeddings(self.cache, [self.item, second], "embed", ""), {})
        self.embed_calls.clear()
        repaired = overlap.get_embeddings([self.item, second], distinct_embed, self.cache, model="embed")
        self.assertEqual(len(self.embed_calls), 2)
        self.assertEqual(repaired, {"item1": [1.0, 0.0, 0.0], "item2": [0.0, 1.0, 0.0]})
        self.assertEqual(overlap.load_embeddings(self.cache, [self.item, second], "embed", ""), repaired)

        data = self.load_cache_json()
        vectors = next(iter(data["namespaces"].values()))["vectors"]
        vectors.pop(key_a)
        vectors[key_b]["text_sha256"] = key_a
        self.cache.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(overlap.load_embeddings(self.cache, [self.item], "embed", ""), {})

        vectors.pop(key_b)
        vectors["e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"] = {"text_sha256": key_a, "vector": [1.0, 0.0, 0.0]}
        self.cache.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(overlap.load_embeddings(self.cache, [self.item], "embed", ""), {"item1": [1.0, 0.0, 0.0]})

    def test_explicit_dims_reject_wrong_response_length(self):
        """Reject a three-dimensional response when the caller requests two dimensions."""
        args = overlap.parse_args([
            "run", "--base-url", "https://one.test/v1", "--judge-model", "judge",
            "--embed-model", "embed", "--embed-dims", "2",
        ])
        with self.assertRaises(ValueError):
            overlap.get_embeddings([self.item], self.embed, self.cache, model=args.embed_model, embed_dims=args.embed_dims)
        self.assertFalse(self.cache.exists())

    def test_inferred_dims_reject_wrong_response_length(self):
        """Reject an incompatible response for a new item in an existing namespace."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        before = self.cache.read_bytes()
        new = overlap.Item("item2", "b2", "name2", "desc2")
        with self.assertRaises(ValueError):
            overlap.get_embeddings([new], lambda texts: [[1.0, 0.0]], self.cache, model="embed")
        self.assertEqual(self.cache.read_bytes(), before)

    def test_mixed_response_lengths_rejected_before_save(self):
        """Reject differing vector lengths within the first response batch."""
        new = overlap.Item("item2", "b2", "name2", "desc2")
        with self.assertRaises(ValueError):
            overlap.get_embeddings([self.item, new], lambda texts: [[1.0], [1.0, 0.0]], self.cache, model="embed")
        self.assertFalse(self.cache.exists())

    def test_multiple_matching_dims_require_explicit_choice(self):
        """Refuse an implicit choice between matching namespaces of different dimensions."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", embed_dims=3)
        overlap.get_embeddings([self.item], lambda texts: [[1.0, 0.0]], self.cache, model="embed", embed_dims=2)
        self.assertEqual(overlap.load_embeddings(self.cache, [self.item], "embed", "", 2),
                         {self.item.item_id: [1.0, 0.0]})
        with self.assertRaisesRegex(ValueError, "--embed-dims"):
            overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")

    def test_changed_preprocessing_uses_new_namespace(self):
        """Call embeddings again when only the preprocessing version changes."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.embed_calls.clear()
        with patch.object(overlap, "EMBED_PREPROCESSING", "other-preprocessing"):
            overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, ["name1: desc1"])
        self.assertEqual(len(self.load_cache_json()["namespaces"]), 2)

    def test_invalid_namespace_keys_and_dimensions_are_skipped(self):
        """Warn about invalid metadata before inferring the one valid dimension."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        data = self.load_cache_json()
        valid = next(iter(data["namespaces"].values()))
        space = overlap.EmbeddingSpace(valid["model"], valid["provider"], valid["preprocessing"])
        damaged = {"bad-key": valid, space.key(2.0): valid | {"dims": 2.0},
                   space.key(True): valid | {"dims": True}}
        data["namespaces"].update(damaged)
        self.cache.write_text(json.dumps(data))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(overlap.load_embeddings(self.cache, [self.item], "embed", ""),
                             {self.item.item_id: [0.1, 0.2, 0.3]})
        for key in damaged:
            self.assertIn(key, stderr.getvalue())

    def test_bool_embedding_dims_refused(self):
        """Reject Boolean values passed directly to the dimensions option."""
        with self.assertRaisesRegex(ValueError, "--embed-dims"):
            overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", embed_dims=True)

    def test_zero_embedding_dims_refused_with_empty_candidates(self):
        """Reject zero CLI dimensions even when no vector gets sent or validated."""
        root = self.cache.parent
        args = ["run", "--candidates", str(write_candidates(root, [])), "--checkouts", str(root),
                "--skills-dir", str(root / "skills"), "--out", str(root / "out.tsv"),
                "--groups", str(root / "groups.tsv"), "--base-url", "https://embed.test/v1",
                "--embed-model", "embed", "--judge-model", "judge", "--embed-dims", "0"]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(overlap.main(args), 1)
        self.assertIn("--embed-dims must be a positive integer", stderr.getvalue())
        self.assertFalse((root / "out.tsv").exists())

    def test_overlap_cli_reports_malformed_cached_vector(self):
        """Report a malformed vector with its cache path and input hash."""
        self.write_damaged_cache()
        args = ["run", "--candidates", str(write_candidates(self.cache.parent, [])),
                "--skills-dir", str(self.cache.parent / "skills"), "--out", str(self.cache.parent / "out.tsv"),
                "--groups", str(self.cache.parent / "groups.tsv"), "--cache", str(self.cache), "--base-url", "https://embed.test/v1",
                "--embed-model", "embed", "--judge-model", "judge"]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(overlap.main(args), 1)
        self.assertIn(str(self.cache), stderr.getvalue())
        self.assertIn(text_sha256(overlap.make_embed_text(self.item)), stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_overlap_cli_transport_failure_uses_safe_endpoint(self):
        """Report only the safe endpoint when an embedding request fails."""
        root = self.cache.parent
        write_skill_file(root, "owner__repo/SKILL.md", "---\nname: fixture\n---\n")
        candidates = write_candidates(root, [
            {"source": "owner/repo", "path": "SKILL.md", "blob_id": "dummy", "verdict": "candidate"},
        ])
        base = "https://fixture-user:fixture-pass@example.test:8443/v1"
        failure = urllib.error.URLError(f"{base}?fixture-query#fixture-fragment")
        args = ["run", "--candidates", str(candidates), "--checkouts", str(root),
                "--skills-dir", str(root / "skills"), "--out", str(root / "out.tsv"),
                "--groups", str(root / "groups.tsv"), "--base-url", base, "--embed-model", "embed",
                "--judge-model", "judge"]
        stderr = io.StringIO()
        with (patch.object(llm_client.urllib.request, "urlopen", side_effect=failure),
              redirect_stderr(stderr)):
            self.assertEqual(overlap.main(args), 1)
        self.assertIn("https://example.test:8443/v1/embeddings", stderr.getvalue())
        self.assertNotIn("fixture-", stderr.getvalue())

    def write_damaged_cache(self):
        """Write a selected namespace containing one invalid vector value."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", provider="https://embed.test/v1")
        data = self.load_cache_json()
        key = text_sha256(overlap.make_embed_text(self.item))
        next(iter(data["namespaces"].values()))["vectors"][key]["vector"] = {"bad": True}
        self.cache.write_text(json.dumps(data))

    def test_adoption_cli_reports_malformed_cached_vector(self):
        """Use the separate embedding endpoint and report malformed data without a traceback."""
        self.write_damaged_cache()
        args = ["--candidates", "unused", "--groups", "unused", "--census", "unused",
                "--checkouts", str(self.cache.parent), "--root", str(self.cache.parent),
                "--include-grouped", "--cache", str(self.cache), "--base-url", "https://chat.test/v1",
                "--embed-base-url", "https://embed.test/v1", "--embed-model", "embed"]
        stderr = io.StringIO()
        with (patch.object(adopt_skills, "inputs", return_value=([], {}, {}, {}, [], {}, [])),
              redirect_stderr(stderr)):
            self.assertEqual(adopt_skills.main(args), 1)
        self.assertIn(str(self.cache), stderr.getvalue())
        self.assertIn(text_sha256(overlap.make_embed_text(self.item)), stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_grouped_adoption_refuses_missing_or_incompatible_namespace(self):
        """Point grouped adoption to the cache-producing command when its space is unavailable."""
        overlap.get_embeddings([self.item], self.embed, self.cache, model="other-model",
                               provider="https://embed.test/v1")
        for cache in (self.cache, None, self.cache.with_name("missing.json")):
            with (self.subTest(cache=cache),
                  self.assertRaisesRegex(ValueError, r"python3 tools/overlap\.py run --cache .* --embed-model embed"),
                  patch.object(adopt_skills, "inputs", return_value=([], {}, {}, {}, [], {}, []))):
                adopt_skills.run(None, None, None, self.cache.parent, self.cache.parent, include_grouped=True,
                                 cache=cache, embed_model="embed", embed_base_url="https://embed.test/v1")

    def test_adoption_shared_endpoint_default_and_unused_cache(self):
        """Use the environment for grouped runs and ignore a cache for ungrouped runs."""
        root = self.cache.parent
        (root / "REUSE.toml").write_text("")
        (root / "CREDITS.md").write_text("")
        overlap.get_embeddings([self.item], self.embed, self.cache, model="embed", provider="https://embed.test/v1")
        with (patch.object(adopt_skills, "inputs", return_value=([], {}, {}, {}, [], {}, [])),
              patch.dict(os.environ, {"PERITIA_LLM_BASE_URL": "https://embed.test/v1",
                                      "PERITIA_LLM_EMBED_MODEL": ""})):
            self.assertEqual(adopt_skills.run(None, None, None, root, root, cache=self.cache)[0], [])
            self.assertEqual(adopt_skills.run(None, None, None, root, root, include_grouped=True,
                                              cache=self.cache, embed_model="embed")[0], [])
            args = ["--candidates", "unused", "--groups", "unused", "--census", "unused",
                    "--checkouts", str(root), "--root", str(root), "--cache", str(self.cache)]
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(adopt_skills.main(args), 0)

    def test_ungrouped_adoption_ignores_invalid_inherited_endpoint(self):
        """Ignore the unused environment endpoint while still validating explicit options."""
        root = self.cache.parent
        (root / "REUSE.toml").write_text("")
        (root / "CREDITS.md").write_text("")
        args = ["--candidates", "unused", "--groups", "unused", "--census", "unused",
                "--checkouts", str(root), "--root", str(root)]
        with (patch.object(adopt_skills, "inputs", return_value=([], {}, {}, {}, [], {}, [])),
              patch.dict(os.environ, {"PERITIA_LLM_BASE_URL": "invalid"}),
              patch("sys.stdout", new_callable=io.StringIO)):
            self.assertEqual(adopt_skills.run(None, None, None, root, root)[0], [])
            self.assertEqual(adopt_skills.main(args), 0)
            with self.assertRaisesRegex(ValueError, "for --base-url$"):
                adopt_skills.run(None, None, None, root, root, base_url="")
            for option in ("--base-url", "--embed-base-url"):
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    adopt_skills.main([*args, option, "invalid"])
                self.assertIn(f"invalid base URL for {option}", stderr.getvalue())

    def test_legacy_cache_preserves_unbound_vectors_without_reusing_them(self):
        """Re-embed a legacy entry and preserve the old vector as unbound history."""
        legacy = {"b1": [9.0, 9.0, 9.0]}
        self.cache.write_text(json.dumps(legacy), encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = overlap.get_embeddings([self.item], self.embed, self.cache, model="embed")
        self.assertEqual(self.embed_calls, ["name1: desc1"])
        self.assertEqual(result, {self.item.item_id: [0.1, 0.2, 0.3]})
        self.assertEqual(self.load_cache_json()["version"], 2)
        self.assertEqual(self.load_cache_json()["legacy_unbound"], legacy)
        self.assertIn(str(self.cache), stderr.getvalue())

    def test_self_pair_excluded_from_groups(self):
        """A same_use pair of one item with itself forms no group."""
        item_q = overlap.Item("q", "bq", "q", "desc q")
        item_x = overlap.Item("x", "bx", "x", "desc x")
        item_y = overlap.Item("y", "by", "y", "desc y")
        all_items = [item_q, item_x, item_y]
        rows = [
            {"a": "q", "b": "q", "same_use": "true"},
            {"a": "x", "b": "y", "same_use": "true"},
        ]
        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members"], "x,y")
        self.assertNotIn("q", groups[0]["members"].split(","))

    def test_groups_ordered_by_smallest_member(self):
        """Groups sort by their smallest member, not by the order of the rows."""
        item_b = overlap.Item("b", "bb", "b", "desc b")
        item_c = overlap.Item("c", "bc", "c", "desc c")
        item_y = overlap.Item("y", "by", "y", "desc y")
        item_z = overlap.Item("z", "bz", "z", "desc z")
        all_items = [item_b, item_c, item_y, item_z]
        rows = [
            {"a": "y", "b": "z", "same_use": "true"},
            {"a": "b", "b": "c", "same_use": "true"},
        ]
        groups = overlap.build_groups(rows, all_items)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["group_id"], "1")
        self.assertEqual(groups[0]["members"], "b,c")
        self.assertEqual(groups[1]["group_id"], "2")
        self.assertEqual(groups[1]["members"], "y,z")


class OverlapEmbeddingAndCacheTests(unittest.TestCase):
    def test_git_blob_id_matches_git_at_chunk_boundaries(self):
        """Match Git for empty and chunk-boundary blobs without reading local Git configuration."""
        # Computed by feeding zero bytes of each size to git hash-object --no-filters --stdin.
        blob_ids = {
            0: "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
            65536: "c97c12f9b0a24bfc19c74a2b265a97c924137775",
            65537: "939fd365a7bd709d2dcc70a2d87cff3a6cf1c347",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SKILL.md"
            for size, expected in blob_ids.items():
                with self.subTest(size=size):
                    data = bytes(size)
                    path.write_bytes(data)
                    self.assertEqual(git_blob_id(data), expected)
                    self.assertEqual(overlap._read_candidate_skill(path)[1], expected)

    def test_overlap_cli_rejects_invalid_endpoint(self):
        """Name the endpoint option without exposing any part of rejected URLs."""
        for authority, suffix in (("host.test:fixture-port", ""), ("[fixture-authority]", ""),
                                  ("host.test", "?fixture-query"), ("host.test", "#fixture-fragment")):
            url = f"https://fixture-user:fixture-pass@{authority}/fixture-path{suffix}"
            stderr = io.StringIO()
            with self.subTest(url=url):
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    overlap.parse_args(["run", "--base-url", url, "--embed-model", "embed", "--judge-model", "judge"])
                self.assertIn("invalid base URL for --base-url", stderr.getvalue())
                self.assertNotIn("fixture-", stderr.getvalue())
                self.assertNotIn("host.test", stderr.getvalue())

    def test_batching_never_exceeds_64(self):
        """Split a hundred input texts into batches of sixty-four or fewer."""
        items = [
            overlap.Item(f"item{i}", f"b{i}", f"name{i}", f"desc{i}")
            for i in range(100)
        ]
        batch_sizes = []

        def fake_embed(texts: list[str]) -> list[list[float]]:
            """Record batch size and return one vector per text."""
            batch_sizes.append(len(texts))
            self.assertLessEqual(len(texts), overlap.MAX_BATCH_SIZE)
            return [[1.0, 0.0] for _ in texts]

        res = overlap.get_embeddings(items, fake_embed)
        self.assertEqual(len(res), 100)
        self.assertEqual(batch_sizes, [64, 36])

    def test_confinement_and_checkout_reading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            checkouts = tmp / "checkouts"
            # Outside file
            write_skill_file(tmp, "secret.md", "secret")
            # Escaping candidate
            cand_tsv = write_candidates(tmp, [
                {"blob_id": "b1", "source": "s", "path": "../secret.md", "skill_name": "esc", "verdict": "candidate"},
                {"blob_id": "b2", "source": "s", "path": "sub/SKILL.md", "skill_name": "valid", "verdict": "candidate"},
                {"blob_id": "b3", "source": "s", "path": "ref/SKILL.md", "skill_name": "refused", "verdict": "refused"},
            ])
            write_skill_file(checkouts, "s/sub/SKILL.md", "---\nname: valid\ndescription: " + "A" * 800 + "\n---\n")

            candidates = overlap.load_candidates(cand_tsv, checkouts)
            # Only valid candidate row is loaded
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].skill_name, "valid")
            # Description bounded to 600 characters
            self.assertEqual(len(candidates[0].description), overlap.MAX_DESC_CHARS)

    def test_block_scalar_reaches_item_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skill_folder = skills_dir / "my-skill"
            skill_folder.mkdir(parents=True)
            skill_md = (
                "---\n"
                "name: my-skill\n"
                "description: >\n"
                "  First line of description\n"
                "  second line of description.\n"
                "---\n\n"
                "# my-skill\n"
            )
            (skill_folder / "SKILL.md").write_text(skill_md, encoding="utf-8")
            items = overlap.load_existing_skills(skills_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].skill_name, "my-skill")
            self.assertEqual(items[0].description, "First line of description second line of description.")


class OverlapSummaryTests(unittest.TestCase):
    def test_fingerprinted_history_without_pair_snapshot_is_refused(self):
        """Avoid reporting historical ranks as if they were the current run."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlap.tsv"
            path.write_text("\t".join(overlap.OVERLAP_FIELDS) + "\n")
            with self.assertRaisesRegex(ValueError, "current pair snapshot missing"):
                overlap.summary(path, path.with_name("groups.tsv"))

    def test_summary_reports_all_five_metrics(self):
        """Retain the summary metrics for legacy overlap and group outputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_overlap = tmp / "overlap.tsv"
            out_groups = tmp / "groups.tsv"
            cand_tsv = write_candidates(tmp, [
                {"blob_id": "b1", "source": "s", "path": "c1/SKILL.md", "verdict": "candidate"},
                {"blob_id": "b2", "source": "s", "path": "c2/SKILL.md", "verdict": "candidate"},
                {"blob_id": "b3", "source": "s", "path": "c3/SKILL.md", "verdict": "candidate"},
            ])

            legacy_fields = [field for field in overlap.OVERLAP_FIELDS if field != "judge_fp"]
            with out_overlap.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=legacy_fields, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerow({"a": "s:c1/SKILL.md", "b": "s:c2/SKILL.md", "same_use": "true"})
                writer.writerow({"a": "s:c2/SKILL.md", "b": "s:c3/SKILL.md", "same_use": "unjudged"})

            with out_groups.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=overlap.GROUPS_FIELDS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerow({"group_id": "1", "members": "s:c1/SKILL.md,s:c2/SKILL.md", "edges": "s:c1/SKILL.md|s:c2/SKILL.md", "has_existing": "false"})

            rep = overlap.summary(out_overlap, out_groups, cand_tsv)
            self.assertIn("candidates: 3", rep)
            self.assertIn("overlap-free candidates: 1", rep)
            self.assertIn("groups: 1", rep)
            self.assertIn("groups with an existing skill: 0", rep)
            self.assertIn("unjudged pairs: 1", rep)

    def test_summary_default_skills_dir_matches_explicit_path_for_removed_skill(self):
        """Resolve None and Path('skills') identically when filtering removed existing skills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            write_skill_file(tmp, "skills/kept/SKILL.md", "---\nname: kept\ndescription: kept\n---\n")
            cand_tsv = write_candidates(tmp, [
                {"blob_id": "b1", "source": "s", "path": "c1/SKILL.md", "verdict": "candidate"},
                {"blob_id": "b2", "source": "s", "path": "c2/SKILL.md", "verdict": "candidate"},
            ])
            groups = [
                {"group_id": "1", "members": "s:c1/SKILL.md,skills/removed",
                 "edges": "s:c1/SKILL.md|skills/removed", "has_existing": "true"},
                {"group_id": "2", "members": "s:c2/SKILL.md,skills/kept",
                 "edges": "s:c2/SKILL.md|skills/kept", "has_existing": "true"},
            ]
            pairs = [
                dict.fromkeys(overlap.OVERLAP_FIELDS, "")
                | {"a": "s:c1/SKILL.md", "b": "skills/removed", "same_use": "unjudged"},
                dict.fromkeys(overlap.OVERLAP_FIELDS, "")
                | {"a": "s:c2/SKILL.md", "b": "skills/kept", "same_use": "true"},
            ]
            out_overlap = tmp / "overlap.tsv"
            out_groups = tmp / "groups.tsv"
            overlap._write_overlap_generation(out_groups, groups, pairs)

            with chdir(tmp):
                implicit = overlap.summary(out_overlap, out_groups, cand_tsv, skills_dir=None)
                explicit = overlap.summary(out_overlap, out_groups, cand_tsv, skills_dir=Path("skills"))
                self.assertEqual(implicit, explicit)
                self.assertIn("overlap-free candidates: 1", implicit)
                self.assertIn("groups: 1", implicit)
                self.assertIn("groups with an existing skill: 1", implicit)
                self.assertIn("unjudged pairs: 0", implicit)

                shutil.rmtree(tmp / "skills")
                no_dir_implicit = overlap.summary(out_overlap, out_groups, cand_tsv, skills_dir=None)
                no_dir_explicit = overlap.summary(out_overlap, out_groups, cand_tsv, skills_dir=Path("skills"))
                self.assertEqual(no_dir_implicit, no_dir_explicit)
                self.assertIn("groups: 2", no_dir_implicit)
                self.assertIn("unjudged pairs: 1", no_dir_implicit)


if __name__ == "__main__":
    unittest.main()
