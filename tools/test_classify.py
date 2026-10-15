#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for the census classifier; no test calls a model."""
import csv
import tempfile
import unittest
from pathlib import Path

import classify

CENSUS_FIELDS = ["source", "pin", "path", "skill_name", "blob_id", "dup_of_blob", "failure_reason"]


def write_census(folder, rows):
    path = Path(folder) / "census.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CENSUS_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CENSUS_FIELDS})
    return path


def write_skill(folder, source, path, text="---\nname: x\n---\nbody\n"):
    target = Path(folder) / source.replace("/", "__") / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


GOOD = {"functions": ["coding", "testing"], "domains": ["go-service-framework"]}


class LabelTests(unittest.TestCase):
    def test_schema_enums_match_the_label_lists(self):
        properties = classify.label_schema()["properties"]
        self.assertEqual(set(properties["functions"]["items"]["enum"]), set(classify.FUNCTIONS))
        self.assertEqual(set(properties["domains"]["items"]["enum"]), set(classify.DOMAINS))
        self.assertEqual(properties["functions"]["maxItems"], classify.MAX_LABELS)

    def test_valid_answer_is_deduplicated_in_order(self):
        answer = {"functions": ["security", "security", "review-audit"], "domains": ["other"]}
        self.assertEqual(classify.checked_labels(answer), (["security", "review-audit"], ["other"]))

    def test_refused_answers(self):
        refused = {
            "not an object": ["coding"],
            "unknown function": {"functions": ["hacking"], "domains": ["other"]},
            "unknown domain": {"functions": ["coding"], "domains": ["mars"]},
            "empty functions": {"functions": [], "domains": ["other"]},
            "missing domains": {"functions": ["coding"]},
            "non-text label": {"functions": [1], "domains": ["other"]},
            "four labels": {"functions": ["coding", "testing", "security", "media"], "domains": ["other"]},
        }
        for label, answer in refused.items():
            with self.subTest(label):
                self.assertIsNone(classify.checked_labels(answer))

    def test_three_labels_is_the_bound(self):
        answer = {"functions": ["coding", "testing", "security"], "domains": ["other"]}
        self.assertIsNotNone(classify.checked_labels(answer))


class AskTests(unittest.TestCase):
    def test_second_attempt_is_used(self):
        answers = iter([{"functions": ["bogus"], "domains": ["other"]}, GOOD])
        self.assertEqual(classify.classify_one(lambda text: next(answers), "t"), (["coding", "testing"], ["go-service-framework"]))

    def test_two_failures_give_none(self):
        calls = []

        def ask(text):
            calls.append(text)
            raise OSError("connection refused")
        self.assertIsNone(classify.classify_one(ask, "t"))
        self.assertEqual(len(calls), classify.ATTEMPTS)


class ReadTests(unittest.TestCase):
    def test_unique_items_skip_duplicates_and_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            census = write_census(folder, [
                {"source": "a/b", "path": "x/SKILL.md", "skill_name": "x", "blob_id": "1"},
                {"source": "a/b", "path": "y/SKILL.md", "skill_name": "y", "blob_id": "1", "dup_of_blob": "a/b:x/SKILL.md"},
                {"source": "c/d", "failure_reason": "HTTP 404"},
            ])
            self.assertEqual([item.skill_name for item in classify.unique_items(census)], ["x"])

    def test_path_leaving_the_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            write_skill(folder, "a/b", "x/SKILL.md")
            Path(folder, "secret.md").write_text("secret", encoding="utf-8")
            escaping = classify.Item("1", "a/b", "../secret.md", "x")
            with self.assertRaises(ValueError):
                classify.skill_text(Path(folder), escaping)
            inside = classify.Item("1", "a/b", "x/SKILL.md", "x")
            self.assertIn("body", classify.skill_text(Path(folder), inside))

    def test_text_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            write_skill(folder, "a/b", "x/SKILL.md", "y" * (classify.MAX_SKILL_CHARS + 50))
            text = classify.skill_text(Path(folder), classify.Item("1", "a/b", "x/SKILL.md", "x"))
            self.assertEqual(len(text), classify.MAX_SKILL_CHARS)


class RunTests(unittest.TestCase):
    def test_run_writes_model_and_status_and_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("x", "y"):
                write_skill(folder, "a/b", f"{name}/SKILL.md")
            items = [classify.Item("1", "a/b", "x/SKILL.md", "x"), classify.Item("2", "a/b", "y/SKILL.md", "y")]
            out = Path(folder) / "out.tsv"
            first = classify.run(items[:1], Path(folder), out, lambda text: GOOD, "model-a")
            self.assertEqual(first["ok"], 1)
            asked = []
            second = classify.run(items, Path(folder), out, lambda text: asked.append(text) or {"functions": ["x"]}, "model-a")
            self.assertEqual((second["ok"], second["error"], len(asked)), (0, 1, classify.ATTEMPTS))
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual([(row["blob_id"], row["status"], row["model"]) for row in rows],
                             [("1", "ok", "model-a"), ("2", "error", "model-a")])
            self.assertEqual(rows[0]["functions"], "coding;testing")

    def test_summary_names_zero_hit_domains(self):
        with tempfile.TemporaryDirectory() as folder:
            write_skill(folder, "a/b", "x/SKILL.md")
            out = Path(folder) / "out.tsv"
            classify.run([classify.Item("1", "a/b", "x/SKILL.md", "x")], Path(folder), out, lambda text: GOOD, "m")
            report = classify.summary(out)
            self.assertIn("rows=1 ok=1 error=0 models=m", report)
            self.assertIn("domain go-service-framework 1", report)
            gaps = report.splitlines()[-1]
            self.assertIn("windows-platform", gaps)
            self.assertNotIn("go-service-framework", gaps)
            self.assertNotIn("other", gaps)


if __name__ == "__main__":
    unittest.main()
