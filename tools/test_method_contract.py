#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the typed Epitrop slot result reader in method_contract.py.

The Epitrop fixtures under contracts/epitrop/testdata/ are Epitrop's own result fixtures, replayed in both
directions: a result Epitrop accepts reads here, and one it refuses is refused here with the reason Epitrop names.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import method_contract as mc  # noqa: E402

EPITROP_FIXTURES = mc.EPITROP_DIR / "testdata" / "result"
MAX_VENDORED_FILES = 64  # bounds the vendored files walked (HISS-02)


class EpitropFixtureTest(unittest.TestCase):
    """The vendored Epitrop schema and fixtures, pinned and replayed."""

    def test_vendored_files_match_the_pin(self) -> None:
        """Every vendored file has the digest vendor.json pins, and no file is unpinned."""
        pin = mc.read_document(mc.EPITROP_DIR / "vendor.json")
        assert isinstance(pin, dict)
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        on_disk = sorted(path.relative_to(mc.EPITROP_DIR).as_posix()
                         for path in list(mc.EPITROP_DIR.rglob("*.json"))[:MAX_VENDORED_FILES]
                         if path.name != "vendor.json")
        self.assertEqual(on_disk, sorted(pin["files"]))
        for name, digest in pin["files"].items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((mc.EPITROP_DIR / name).read_bytes()).hexdigest(), digest)

    def test_replays_every_fixture_in_both_directions(self) -> None:
        """A result Epitrop accepts reads here; one it refuses is refused with the reason Epitrop names."""
        fixtures = sorted(EPITROP_FIXTURES.glob("*.json"))[:MAX_VENDORED_FILES]
        directions = set()
        for path in fixtures:
            fixture = mc.read_document(path)
            assert isinstance(fixture, dict)
            directions.add(fixture["direction"])
            with self.subTest(fixture=fixture["id"]):
                if fixture["direction"] == "compliant":
                    result = mc.result_from_document(mc.parse_document(fixture["document"], fixture["id"]))
                    self.assertIn(result.outcome, {"done", "blocked", "failed"})
                    continue
                with self.assertRaises(mc.ContractError) as caught:
                    mc.result_from_document(mc.parse_document(fixture["document"], fixture["id"]))
                self.assertIn(fixture["expect"]["says"], str(caught.exception))
        self.assertEqual(directions, {"compliant", "violation"})

    def test_typed_result_keeps_the_epitrop_fields(self) -> None:
        """The typed result carries Epitrop's field values unchanged, evidence included."""
        fixture = mc.read_document(EPITROP_FIXTURES / "result-done-with-evidence.json")
        assert isinstance(fixture, dict)
        document = mc.parse_document(fixture["document"], fixture["id"])
        assert isinstance(document, dict)
        result = mc.result_from_document(document)
        self.assertEqual((result.unit, result.outcome, result.head, result.summary),
                         (document["unit"], document["outcome"], document["head"], document["summary"]))
        self.assertEqual(result.evidence, (mc.Evidence(**document["evidence"][0]),))
        self.assertEqual(result.questions, ())


class DocumentTest(unittest.TestCase):
    """Parsing contract documents before any schema runs."""

    def test_refuses_numbers_json_does_not_have(self) -> None:
        """NaN, Infinity, overflowing numbers and doubled keys are refused before any check runs."""
        for label, text in {"nan": '{"value": NaN}', "infinity": '{"value": -Infinity}',
                            "overflow": '{"value": 1e400}', "doubled key": '{"value": 1, "value": 2}'}.items():
            with self.subTest(label=label):
                with self.assertRaises(mc.ContractError):
                    mc.parse_document(text, label)

    def test_refuses_oversized_and_unreadable_documents(self) -> None:
        """A document over the size cap and a missing file are refused."""
        with self.assertRaisesRegex(mc.ContractError, "larger than"):
            mc.parse_document(" " * (mc.MAX_DOCUMENT_BYTES + 1), "big")
        with self.assertRaisesRegex(mc.ContractError, "cannot read"):
            mc.read_document(mc.CONTRACTS_DIR / "absent.json")


if __name__ == "__main__":
    unittest.main()
