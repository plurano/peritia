#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for the public-skill census."""
import tempfile
import unittest
from pathlib import Path

import census


def source(repository):
    return census.Source(repository=repository, note="test source", found_at="https://example.test")


class FakeReader:
    """In-memory API and git boundary; no test makes a network call."""

    def __init__(self, records):
        self.records = records
        self.fallback_calls = []

    def resolve_pin(self, item):
        record = self.records[item.repository]
        if "error" in record:
            raise census.SourceReadError(record["error"])
        return record["pin"]

    def repository_license(self, item):
        return self.records[item.repository].get("license", "none")

    def recursive_tree(self, item, pin):
        return self.records[item.repository]["tree"]

    def fallback_tree(self, item, pin):
        self.fallback_calls.append(item.repository)
        return self.records[item.repository]["fallback"]


def entry(path, blob, size=10):
    return {"path": path, "type": "blob", "sha": blob, "size": size}


class CensusTests(unittest.TestCase):
    def collect(self, records, sources):
        return census.collect(sources, FakeReader(records))

    def test_duplicate_blob_rows_and_reconcile(self):
        records = {
            "a/one": {
                "pin": "a" * 40,
                "license": "MIT",
                "tree": {"truncated": False, "tree": [
                    entry("first/SKILL.md", "1" * 40), entry("first/run.py", "2" * 40),
                    entry("second/SKILL.md", "3" * 40),
                ]},
            },
            "b/two": {
                "pin": "b" * 40,
                "license": "Apache-2.0",
                "tree": {"truncated": False, "tree": [entry("other/SKILL.md", "1" * 40)]},
            },
        }
        result = self.collect(records, [source("a/one"), source("b/two")])
        self.assertEqual(3, len(result.rows))
        self.assertEqual("a/one:first/SKILL.md", result.rows[2].dup_of_blob)
        self.assertEqual([".py"], result.rows[0].scripts_present)
        self.assertTrue(result.reconcile().ok)

    def test_failed_source_is_written_and_makes_run_fail(self):
        records = {
            "a/good": {"pin": "a" * 40, "license": "MIT", "tree": {"truncated": False, "tree": [entry("x/SKILL.md", "1" * 40)]}},
            "b/private": {"error": "HTTP 404"},
        }
        result = self.collect(records, [source("a/good"), source("b/private")])
        self.assertEqual(1, len(result.rows))
        self.assertEqual("HTTP 404", result.failures[0].reason)
        self.assertEqual(1, result.exit_code)
        self.assertIn("HTTP 404", census.render_tsv(result))

    def test_unlicensed_entry_is_flagged(self):
        records = {
            "a/no-license": {"pin": "a" * 40, "license": "none", "tree": {"truncated": False, "tree": [entry("x/SKILL.md", "1" * 40)]}},
        }
        result = self.collect(records, [source("a/no-license")])
        self.assertEqual("licence:none", result.rows[0].ineligible)

    def test_truncated_tree_uses_fallback(self):
        records = {
            "a/truncated": {
                "pin": "a" * 40,
                "license": "MIT",
                "tree": {"truncated": True, "tree": []},
                "fallback": [entry("x/SKILL.md", "1" * 40)],
            },
        }
        reader = FakeReader(records)
        result = census.collect([source("a/truncated")], reader)
        self.assertEqual(["a/truncated"], reader.fallback_calls)
        self.assertEqual("x/SKILL.md", result.rows[0].path)

    def test_identical_input_has_identical_tsv(self):
        records = {
            "a/one": {"pin": "a" * 40, "license": "MIT", "tree": {"truncated": False, "tree": [entry("z/SKILL.md", "2" * 40), entry("a/SKILL.md", "1" * 40)]}},
        }
        first = self.collect(records, [source("a/one")])
        second = self.collect(records, [source("a/one")])
        self.assertEqual(census.render_tsv(first), census.render_tsv(second))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "census.tsv"
            census.write_tsv(output, first)
            self.assertEqual(census.render_tsv(second), output.read_text(encoding="utf-8"))


class ManifestTests(unittest.TestCase):
    """load_sources reads JSON with exact keys; anything else fails before a network call."""

    def load(self, document):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            path.write_text(document if isinstance(document, str) else census.json.dumps(document), encoding="utf-8")
            return census.load_sources(path)

    def entry(self, repository="owner/name", **extra):
        return {"repository": repository, "note": "a note", "found_at": "https://example.test", **extra}

    def test_shipped_manifest_loads(self):
        sources = census.load_sources(Path(__file__).with_name("census-sources.json"))
        self.assertTrue(sources)
        self.assertEqual(len({item.repository.lower() for item in sources}), len(sources))

    def test_valid_entry_loads(self):
        self.assertEqual([item.repository for item in self.load({"sources": [self.entry()]})], ["owner/name"])

    def test_refused_shapes(self):
        refused = {
            "not json": "sources: []",
            "extra top-level key": {"sources": [self.entry()], "other": 1},
            "empty list": {"sources": []},
            "missing field": {"sources": [{"repository": "owner/name", "note": "n"}]},
            "unknown field": {"sources": [self.entry(extra="x")]},
            "empty value": {"sources": [self.entry(note=" ")]},
            "path traversal": {"sources": [self.entry("../owner/name")]},
            "no slash": {"sources": [self.entry("owner")]},
            "two slashes": {"sources": [self.entry("owner/name/more")]},
            "duplicate": {"sources": [self.entry("Owner/Name"), self.entry("owner/name")]},
        }
        for label, document in refused.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.load(document)

    def test_count_bound(self):
        at_bound = {"sources": [self.entry(f"owner/n{index}") for index in range(census.MAX_SOURCES)]}
        self.assertEqual(len(self.load(at_bound)), census.MAX_SOURCES)
        with self.assertRaises(ValueError):
            self.load({"sources": [self.entry(f"owner/n{index}") for index in range(census.MAX_SOURCES + 1)]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
