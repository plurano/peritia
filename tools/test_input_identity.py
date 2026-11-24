# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the shared input fingerprint and TSV migration contracts."""
import csv
import hashlib
import io
import os
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import input_identity
from input_identity import fingerprint, open_fingerprinted_tsv, provider_id, text_sha256
from llm_client import LLMClient


class InputIdentityTests(unittest.TestCase):
    """Check fingerprint stability and legacy file preservation."""

    def test_canonical_fingerprint(self):
        """Hash canonical Unicode JSON independent of input key order."""
        serialized = '{"kind":"example","name":"café","temperature":0.7,"version":1}'
        expected = "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
        self.assertEqual(fingerprint("example", {"temperature": 0.7, "name": "café"}), expected)
        self.assertEqual(fingerprint("example", {"name": "café", "temperature": 0.7}), expected)
        self.assertNotEqual(fingerprint("example", {"temperature": 0.70000001, "name": "café"}), expected)
        self.assertNotEqual(fingerprint("other", {"temperature": 0.7, "name": "café"}), expected)
        self.assertEqual(text_sha256("café"), hashlib.sha256("café".encode()).hexdigest())

    def test_structured_client_sends_effective_generation_defaults(self):
        """Check the structured request settings at the transport boundary."""
        client = LLMClient("https://example.test/v1")
        schema = {"type": "object"}
        messages = [{"role": "user", "content": "request"}]
        reply = {"choices": [{"message": {"content": "{}"}}]}
        with (patch("llm_client.DEFAULT_CHAT_TEMPERATURE", 0.25, create=True),
              patch("llm_client.DEFAULT_CHAT_SEED", 42, create=True),
              patch.object(client, "_post", return_value=reply) as post):
            self.assertEqual(client.chat_json("model", messages, schema, "test_schema"), {})
        post.assert_called_once_with("/chat/completions", {
            "model": "model", "messages": messages, "temperature": 0.25, "seed": 42, "stream": False,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "test_schema", "strict": False, "schema": schema}},
        })

    def test_provider_strips_userinfo(self):
        """Keep the endpoint identity while stripping userinfo."""
        self.assertEqual(provider_id(None), "")
        self.assertEqual(provider_id(""), "")
        self.assertEqual(provider_id("https://u:p@Example.test:123/v1//"),
                         "https://example.test:123/v1")
        self.assertEqual(provider_id("http://[::1]:8000/"), "http://[::1]:8000")

    def test_invalid_provider_urls_are_redacted(self):
        """Keep all URL material out of validation failures and their tracebacks."""
        for authority, suffix in (
            ("example.test:fixture-port", ""), ("[fixture-authority]", ""),
            ("example.test", "?fixture-query"), ("example.test", "#fixture-fragment"),
            ("example.test", "?"), ("example.test", "#"),
        ):
            with self.subTest(authority=authority, suffix=suffix):
                url = f"https://fixture-user:fixture-pass@{authority}/fixture-path{suffix}"
                with self.assertRaisesRegex(ValueError, "--base-url") as error:
                    provider_id(url)
                rendered = "".join(traceback.format_exception(error.exception))
                self.assertNotIn("fixture-", rendered)
                self.assertNotIn("example.test", rendered)
        with self.assertRaisesRegex(ValueError, "--base-url"):
            provider_id("//example.test/v1")

    def test_endpoint_errors_identify_the_supplied_setting(self):
        """Name an inherited endpoint variable or the explicit embedding option."""
        with (patch.dict(os.environ, {"PERITIA_LLM_BASE_URL": "invalid"}),
              self.assertRaisesRegex(ValueError, "for PERITIA_LLM_BASE_URL$")):
            input_identity.resolve_base_urls(None)
        with self.assertRaisesRegex(ValueError, "for --embed-base-url$"):
            input_identity.resolve_base_urls("https://example.test/v1", "")

    def test_torn_tsv_tail_is_separated(self):
        """Preserve a partial row and start the next record on a new line."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\tfp\ntorn"
            path.write_bytes(original)
            handle, writer = open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            with handle:
                writer.writerow({"id": "next", "fp": "valid"})
            self.assertEqual(path.read_bytes(), original + b"\nnext\tvalid\n")

    def test_migration_works_without_hard_links(self):
        """Archive legacy data on a filesystem without hard-link support."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\nold\n"
            path.write_bytes(original)
            with patch("os.link", side_effect=OSError("hard links unsupported")):
                handle, _ = open_fingerprinted_tsv(path, ["id", "fp"], "fp")
                handle.close()
            self.assertEqual(path.with_name("rows.legacy-1.tsv").read_bytes(), original)
            self.assertEqual(path.read_bytes(), b"id\tfp\n")

    def test_stale_migrator_retains_completed_migration(self):
        """Detect a migration completed since the legacy header was read."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            archive = path.with_name("rows.legacy-1.tsv")
            original = b"id\nlegacy\n"
            current = b"id\tfp\ncurrent\tvalue\n"
            path.write_bytes(original)
            migrate = input_identity._archive_legacy_tsv

            def race(*args):
                """Simulate another writer completing the migration first."""
                archive.write_bytes(original)
                path.write_bytes(current)
                return migrate(*args)

            with (patch.object(input_identity, "_archive_legacy_tsv", side_effect=race),
                  self.assertRaisesRegex(ValueError, r"rows\.tsv.*rows\.legacy-2\.tsv")):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(path.read_bytes(), current)

    def test_migration_checks_archived_header_after_replace(self):
        """Preserve the bytes if another writer wins between checking and replacing."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            archive = path.with_name("rows.legacy-1.tsv")
            competing_archive = path.with_name("rows.legacy-2.tsv")
            original = b"id\nlegacy\n"
            current = b"id\tfp\ncurrent\tvalue\n"
            path.write_bytes(original)
            replace = os.replace

            def race(source, destination):
                """Complete another migration just before the first replacement."""
                competing_archive.write_bytes(original)
                path.write_bytes(current)
                replace(source, destination)

            with (patch("os.replace", side_effect=race),
                  self.assertRaisesRegex(ValueError, r"rows\.tsv.*rows\.legacy-1\.tsv")):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            self.assertEqual(archive.read_bytes(), current)
            self.assertEqual(competing_archive.read_bytes(), original)

    def test_rename_error_preserves_published_archive(self):
        """Keep archived bytes when a filesystem reports an ambiguous rename outcome."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            archive = path.with_name("rows.legacy-1.tsv")
            original = b"id\nlegacy\n"
            path.write_bytes(original)
            replace = os.replace

            def ambiguous_result(source, destination):
                """Move the data, then simulate an error reported after publication."""
                replace(source, destination)
                raise OSError("rename acknowledgement lost")

            with (patch("os.replace", side_effect=ambiguous_result),
                  self.assertRaisesRegex(OSError, r"rows\.tsv.*rows\.legacy-1\.tsv")):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            self.assertEqual(archive.read_bytes(), original)

    def test_legacy_count_is_bounded(self):
        """Reject excess legacy rows before modifying the source file."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\na\nb\n"
            path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, r"rows\.tsv.*cap.*1"):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=1)
            self.assertEqual(path.read_bytes(), original)

    def test_legacy_is_preserved_and_fresh_header_appends(self):
        """Archive the exact legacy bytes at the first free suffix and append current rows."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            existing_archive = path.with_name("rows.legacy-1.tsv")
            existing_archive.write_bytes(b"kept")
            legacy = b'id\r\n"one\r\nmore"\r\n'
            path.write_bytes(legacy)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                handle, writer = open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            with handle:
                writer.writerow({"id": "a", "fp": "first"})
            handle, writer = open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            with handle:
                writer.writerow({"id": "b", "fp": "second"})
            archive = path.with_name("rows.legacy-2.tsv")
            self.assertEqual(archive.read_bytes(), legacy)
            self.assertEqual(existing_archive.read_bytes(), b"kept")
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            self.assertIn(f"{path}", stderr.getvalue())
            self.assertIn(f"{archive}", stderr.getvalue())
            self.assertIn("1 row", stderr.getvalue())
            with path.open(newline="", encoding="utf-8") as data:
                self.assertEqual(list(csv.reader(data, delimiter="\t")),
                                 [["id", "fp"], ["a", "first"], ["b", "second"]])

    def test_legacy_rename_limit(self):
        """Refuse a hundredth backup while preserving the original file."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            path.write_bytes(b"id\na\n")
            for n in range(1, 100):
                path.with_name(f"rows.legacy-{n}.tsv").touch()
            with self.assertRaisesRegex(ValueError, "99"):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            self.assertEqual(path.read_bytes(), b"id\na\n")

    def test_incompatible_header_is_refused(self):
        """Report both schemas when a fingerprinted header is incompatible."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            path.write_text("fp\tid\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\['fp', 'id'\].*\['id', 'fp'\]"):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp")
            self.assertEqual(path.read_bytes(), b"fp\tid\n")


if __name__ == "__main__":
    unittest.main()
