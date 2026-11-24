#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for the census classifier; no test calls a model."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import classify
import input_identity

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

    def setUp(self):
        """Create two skills, an output path, and a recording model stub."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.out = self.folder / "resume.tsv"
        self.items = [classify.Item(str(i), "a/b", f"{name}/SKILL.md", name)
                      for i, name in enumerate(("x", "y"), 1)]
        for item in self.items:
            write_skill(self.folder, item.source, item.path, item.skill_name)
        self.asked = []

    def ask(self, text):
        """Record the input and return valid labels."""
        self.asked.append(text)
        return GOOD

    def classify(self, model="model-a", **options):
        """Classify the current items and return the new model calls."""
        self.asked.clear()
        classify.run(self.items, self.folder, self.out, self.ask, model, **options)
        return self.asked.copy()

    def test_unchanged_inputs_skip_all_asks(self):
        """Reuse all classifications for identical inputs."""
        self.assertEqual(self.classify(), ["x", "y"])
        original = self.out.read_bytes()
        self.assertEqual(self.classify(), [])
        self.assertEqual(self.out.read_bytes(), original)

    def test_structured_temperature_changes_classification_identity(self):
        """Reuse a classification until the client changes its default temperature."""
        self.classify()
        self.assertEqual(self.classify(), [])
        with patch("llm_client.DEFAULT_CHAT_TEMPERATURE", 0.1, create=True):
            self.assertEqual(self.classify(), ["x", "y"])
            self.assertEqual(self.classify(), [])

    def test_run_rejects_query_in_provider_before_inference(self):
        """Refuse a provider URL whose query could differ from the identity."""
        with self.assertRaisesRegex(ValueError, "invalid base URL for --base-url"):
            self.classify(provider="https://models.invalid/v1?x=1")
        self.assertFalse(self.out.exists())

    def test_changed_model_asks_for_each_item(self):
        """Append current model rows and report each blob once."""
        self.classify()
        original = self.out.read_bytes()
        self.assertEqual(self.classify("model-b"), ["x", "y"])
        self.assertTrue(self.out.read_bytes().startswith(original))
        self.assertIn("rows=2 ok=2 error=0 models=model-b", classify.summary(self.out))

    def test_return_to_original_model_restores_latest_rows(self):
        """Ensure the latest rows belong to the requested model after switching back."""
        self.classify()
        self.classify("model-b")
        self.classify()
        self.assertIn("rows=2 ok=2 error=0 models=model-a", classify.summary(self.out))

    def test_changed_content_and_blob_asks_only_for_that_item(self):
        """Classify changed content at the same checkout path."""
        self.classify()
        item = self.items[0]
        write_skill(self.folder, item.source, item.path, "changed")
        self.items[0] = classify.Item("replacement", item.source, item.path, item.skill_name)
        self.assertEqual(self.classify(), ["changed"])

    def test_changed_sent_text_asks_even_with_old_blob_id(self):
        """Bind reuse to the actual text when a checkout changes before its census."""
        self.classify()
        item = self.items[0]
        write_skill(self.folder, item.source, item.path, "changed")
        self.assertEqual(self.classify(), ["changed"])

    def test_changed_provider_asks_for_each_item(self):
        """Treat two endpoint paths as different providers."""
        self.classify(provider="https://models.invalid/a/v1")
        self.assertEqual(self.classify(provider="https://models.invalid/b/v1"), ["x", "y"])

    def test_changed_system_prompt_asks_for_each_item(self):
        """Reclassify every item after a system prompt edit."""
        self.classify()
        with patch.object(classify, "SYSTEM_PROMPT", "new instructions"):
            self.assertEqual(self.classify(), ["x", "y"])

    def test_changed_schema_asks_for_each_item(self):
        """Reclassify every item after a schema boundary changes."""
        self.classify()
        with patch.object(classify, "MAX_LABELS", 4):
            self.assertEqual(self.classify(), ["x", "y"])

    def test_changed_schema_name_asks_for_each_item(self):
        """Reclassify each item when the schema name alone changes."""
        self.classify()
        with patch.object(classify, "LABEL_SCHEMA_NAME", "renamed"):
            self.assertEqual(self.classify(), ["x", "y"])

    def test_changed_taxonomy_asks_for_each_item(self):
        """Reclassify every item after a label joins the taxonomy."""
        self.classify()
        with patch.dict(classify.DOMAINS, {"new-domain": "A new domain"}):
            self.assertEqual(self.classify(), ["x", "y"])

    def test_legacy_classifications_are_preserved_and_rerun(self):
        """Move the legacy TSV byte for byte before reclassifying every item."""
        fields = [field for field in classify.OUTPUT_FIELDS if field != "input_fp"]
        with self.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerow({"blob_id": "1", "model": "old-model", "status": "ok"})
        original = self.out.read_bytes()
        self.assertEqual(self.classify(), ["x", "y"])
        self.assertEqual(self.out.with_name("resume.legacy-1.tsv").read_bytes(), original)
        self.assertIn("rows=2 ok=2 error=0 models=model-a", classify.summary(self.out))

    def test_migration_holds_output_lock_until_new_rows_are_written(self):
        """Block a competing run inside migration and preserve both generations."""
        original = b"blob_id\tmodel\n1\told-a\n2\told-b\n"
        self.out.write_bytes(original)
        archive = input_identity._archive_output

        def migrate(path, kind):
            """Try to acquire the same output lock before renaming the legacy TSV."""
            with self.assertRaisesRegex(TimeoutError, r"resume\.tsv\.lock"):
                with input_identity.output_lock(path):
                    self.fail("migration did not hold the output lock")
            return archive(path, kind)

        def ask(text):
            """Verify that inference still runs inside the same lock."""
            with self.assertRaisesRegex(TimeoutError, r"resume\.tsv\.lock"):
                with input_identity.output_lock(self.out):
                    self.fail("inference did not hold the output lock")
            return self.ask(text)

        with (patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001),
              patch.object(input_identity, "_archive_output", side_effect=migrate)):
            classify.run(self.items, self.folder, self.out, ask, "model-a")
        self.assertEqual(self.out.with_name("resume.legacy-1.tsv").read_bytes(), original)
        self.assertEqual(self.asked, ["x", "y"])
        self.assertEqual(set(classify.latest_classifications(self.out)), {"1", "2"})

    def test_compacts_full_history_and_resumes_latest_rows(self):
        """Archive all prior rows and keep one active classification per blob."""
        with patch.object(classify, "MAX_ROWS", 3):
            self.classify()
            self.classify("model-b", limit=1)
            original = self.out.read_bytes()
            self.assertEqual(self.classify("model-b"), ["y"])
            self.assertEqual(self.out.with_name("resume.history-1.tsv").read_bytes(), original)
            active = self.out.read_bytes()
            mtime = self.out.stat().st_mtime_ns
            self.assertEqual(len(active.splitlines()), 3)
            self.assertEqual(self.classify("model-b"), [])
            self.assertEqual((self.out.read_bytes(), self.out.stat().st_mtime_ns), (active, mtime))

    def test_full_distinct_history_refuses_append_without_writing(self):
        """Refuse compaction when all slots are already needed for the latest rows."""
        with patch.object(classify, "MAX_ROWS", 2):
            self.classify()
            original = self.out.read_bytes(), self.out.stat().st_mtime_ns
            with self.assertRaisesRegex(ValueError, r"resume\.tsv.*cap of 2 rows"):
                self.classify("model-b")
            self.assertEqual((self.out.read_bytes(), self.out.stat().st_mtime_ns), original)
            self.assertFalse(self.out.with_name("resume.history-1.tsv").exists())

    def test_compaction_resumes_after_archive_before_active_publish(self):
        """Recover the staged latest rows if publication fails after the archive rename."""
        replace = input_identity.os.replace

        def interrupt(source, target):
            """Fail only when installing the already staged compacted output."""
            if Path(source).name.endswith(".compacting"):
                raise OSError("interrupted compaction")
            replace(source, target)

        with patch.object(classify, "MAX_ROWS", 3):
            self.classify()
            self.classify("model-b", limit=1)
            original = self.out.read_bytes()
            with (patch.object(input_identity.os, "replace", side_effect=interrupt),
                  self.assertRaisesRegex(OSError, "interrupted compaction")):
                self.classify("model-b")
            self.assertFalse(self.out.exists())
            self.assertEqual(self.out.with_name("resume.history-1.tsv").read_bytes(), original)
            self.assertEqual(self.classify("model-b"), [])
            self.assertFalse(self.out.with_name(".resume.tsv.compacting").exists())
            self.assertIn("rows=2 ok=2 error=0 models=model-b", classify.summary(self.out))

    def test_unreadable_skill_can_resume_after_the_file_returns(self):
        """Retry a read failure because no skill text went to the model."""
        item = self.items[0]
        (self.folder / "a__b" / item.path).unlink()
        self.assertEqual(self.classify(), ["y"])
        write_skill(self.folder, item.source, item.path, item.skill_name)
        self.assertEqual(self.classify(), ["x"])

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
