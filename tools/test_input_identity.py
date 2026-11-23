#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the shared input fingerprint and TSV migration contracts."""
import csv
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from input_identity import fingerprint, open_fingerprinted_tsv, provider_id, text_sha256


class InputIdentityTests(unittest.TestCase):
    """Check fingerprint stability and legacy file preservation."""

    def test_canonical_fingerprint(self):
        """Hash canonical Unicode JSON independent of input key order."""
        serialized = '{"kind":"example","name":"café","temperature":0.7,"version":1}'
        expected = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        self.assertEqual(fingerprint("example", {"temperature": 0.7, "name": "café"}), expected)
        self.assertEqual(fingerprint("example", {"name": "café", "temperature": 0.7}), expected)
        self.assertNotEqual(fingerprint("example", {"temperature": 0.70000001, "name": "café"}), expected)
        self.assertNotEqual(fingerprint("other", {"temperature": 0.7, "name": "café"}), expected)
        self.assertEqual(text_sha256("café"), hashlib.sha256("café".encode("utf-8")).hexdigest())

    def test_provider_removes_unbound_url_parts(self):
        """Keep the endpoint identity and discard user info, query and fragment."""
        self.assertEqual(provider_id(None), "")
        self.assertEqual(provider_id(""), "")
        self.assertEqual(provider_id("https://u:p@Example.test:123/v1//?key=hidden#fragment"),
                         "https://example.test:123/v1")
        self.assertEqual(provider_id("http://[::1]:8000/"), "http://[::1]:8000")

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
