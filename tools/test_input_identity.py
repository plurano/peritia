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
from input_identity import (
    fingerprint,
    open_fingerprinted_tsv,
    output_lock,
    provider_id,
    text_sha256,
)
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

    def test_output_lock_times_out_on_second_open(self):
        """Refuse a second holder while allowing unrelated files to proceed."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            with (patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001),
                  output_lock(path), output_lock(path.with_name("other.tsv")),
                  self.assertRaisesRegex(TimeoutError, r"rows\.tsv\.lock.*timed out")):
                with output_lock(path):
                    self.fail("second lock holder entered")

    def test_output_lock_releases_on_exception(self):
        """Acquire the same output again after its prior holder raises."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            with self.assertRaisesRegex(RuntimeError, "fixture failure"), output_lock(path):
                raise RuntimeError("fixture failure")
            with patch.object(input_identity, "LOCK_WAIT_SECONDS", 0.001), output_lock(path):
                self.assertTrue(path.with_name("rows.tsv.lock").is_file())

    def test_torn_tsv_tail_is_quarantined_at_record_boundary(self):
        """Keep exact bytes of valid multiline rows, then append a parseable row."""
        original = b'id\tfp\r\nfirst\t"complete\r\nrecord"\r\n\r\n'
        for tail in (b"torn", b'torn\t"unclosed\nfield\n', b"torn\t\xc3\n"):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "rows.tsv"
                path.write_bytes(original + tail)
                stderr = io.StringIO()
                with redirect_stderr(stderr), output_lock(path):
                    log = open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                    self.assertEqual(path.read_bytes(), original)
                    log.append({"id": "next", "fp": "valid\rvalue"}, "next", {})
                archive = path.with_name("rows.torn-1.tsv")
                self.assertEqual(archive.read_bytes(), tail)
                self.assertIn(f"{len(tail)} bytes to {archive}", stderr.getvalue())
                with path.open(newline="", encoding="utf-8") as data:
                    rows = list(csv.reader(data, delimiter="\t", strict=True))
                self.assertEqual(rows, [["id", "fp"], ["first", "complete\r\nrecord"], [],
                                        ["next", "valid\rvalue"]])

    def test_tsv_record_byte_limit_refuses_oversized_scan_and_append(self):
        """Keep the existing file intact when a record exceeds the scan's byte limit."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\tfp\n"
            path.write_bytes(original)
            with patch.object(input_identity, "MAX_RECORD_BYTES", 12), output_lock(path):
                log = open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                with self.assertRaisesRegex(ValueError, r"rows\.tsv.*12 bytes"):
                    log.append({"id": "next", "fp": "café café"}, "next", {})
                self.assertEqual(path.read_bytes(), original)
                oversized = original + b'x\t"long\nrecord"\n'
                path.write_bytes(oversized)
                with self.assertRaisesRegex(ValueError, r"rows\.tsv.*12 bytes"):
                    open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                self.assertEqual(path.read_bytes(), oversized)

    def test_jsonl_preserves_malformed_interior_before_last_complete_record(self):
        """Quarantine only the malformed suffix after the last valid JSON value."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.jsonl"
            original = b'{"id":"one"}\ninvalid\n{"id":"two"}\n'
            tail = b'{"id":\n'
            path.write_bytes(original + tail)
            with output_lock(path), redirect_stderr(io.StringIO()):
                log = input_identity.open_jsonl_history(path, 4)
                self.assertEqual(log.count, 3)
                log.append({"id": "three"}, "three", {})
            self.assertEqual(path.read_bytes(), original + b'{"id": "three"}\n')
            self.assertEqual(path.with_name("rows.torn-1.jsonl").read_bytes(), tail)

    def test_migration_works_without_hard_links(self):
        """Archive legacy data on a filesystem without hard-link support."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\nold\n"
            path.write_bytes(original)
            with patch("os.link", side_effect=OSError("hard links unsupported")), output_lock(path):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
            self.assertEqual(path.with_name("rows.legacy-1.tsv").read_bytes(), original)
            self.assertEqual(path.read_bytes(), b"id\tfp\n")

    def test_failed_legacy_rename_preserves_source_and_retry_works(self):
        """Keep every legacy row when moving the source fails before replacement."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\nlegacy-a\nlegacy-b\n"
            path.write_bytes(original)
            with output_lock(path):
                with (patch("os.replace", side_effect=OSError("rename failed")),
                      self.assertRaisesRegex(OSError, r"rows\.tsv.*rows\.legacy-1\.tsv")):
                    open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                self.assertEqual(path.read_bytes(), original)
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
            self.assertEqual(path.with_name("rows.legacy-1.tsv").read_bytes(), original)
            self.assertEqual(path.read_bytes(), b"id\tfp\n")

    def test_rename_error_preserves_published_archive(self):
        """Keep archived bytes when a filesystem reports an ambiguous rename outcome."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            archive = path.with_name("rows.legacy-1.tsv")
            original = b"id\nlegacy-a\nlegacy-b\n"
            path.write_bytes(original)
            replace = os.replace

            def ambiguous_result(source, destination):
                """Move the data, then simulate an error reported after publication."""
                replace(source, destination)
                raise OSError("rename acknowledgement lost")

            with (output_lock(path), patch("os.replace", side_effect=ambiguous_result),
                  self.assertRaisesRegex(OSError, r"rows\.tsv.*rows\.legacy-1\.tsv")):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
            self.assertEqual(archive.read_bytes(), original)

    def test_legacy_count_is_bounded(self):
        """Reject excess legacy rows before modifying the source file."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            original = b"id\na\nb\n"
            path.write_bytes(original)
            with output_lock(path), self.assertRaisesRegex(ValueError, r"rows\.tsv.*cap.*1"):
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
            with redirect_stderr(stderr), output_lock(path):
                log = open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                log.append({"id": "a", "fp": "first"}, "a", {})
                log = open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
                log.append({"id": "b", "fp": "second"}, "b", {})
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
            with output_lock(path), self.assertRaisesRegex(ValueError, "99"):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
            self.assertEqual(path.read_bytes(), b"id\na\n")

    def test_incompatible_header_is_refused(self):
        """Report both schemas when a fingerprinted header is incompatible."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.tsv"
            path.write_text("fp\tid\n", encoding="utf-8")
            with output_lock(path), self.assertRaisesRegex(ValueError, r"\['fp', 'id'\].*\['id', 'fp'\]"):
                open_fingerprinted_tsv(path, ["id", "fp"], "fp", max_rows=3)
            self.assertEqual(path.read_bytes(), b"fp\tid\n")


if __name__ == "__main__":
    unittest.main()
