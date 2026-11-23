#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the typed method contract in method_contract.py.

The Epitrop fixtures under contracts/epitrop/testdata/ are Epitrop's own result fixtures, replayed in both
directions: a result Epitrop accepts reads here, and one it refuses is refused here with the reason Epitrop names.
The battery method under contracts/testdata/ follows skills/battery-modeling. Its validity domain is the tested
reference case of that skill, and its verifier program is a stand-in: the skill ships no helper script.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import re
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import method_contract as mc  # noqa: E402

TESTDATA = mc.CONTRACTS_DIR / "testdata"
EPITROP_FIXTURES = mc.EPITROP_DIR / "testdata" / "result"
MAX_VENDORED_ENTRIES = 64  # bounds the directory entries consumed (HISS-02)


def _bounded_paths(paths: Iterator[Path]) -> list[Path]:
    """Read at most one entry past the walk limit so extra paths fail the test."""
    entries = list(itertools.islice(paths, MAX_VENDORED_ENTRIES + 1))
    if len(entries) > MAX_VENDORED_ENTRIES:
        raise AssertionError(f"more than {MAX_VENDORED_ENTRIES} vendored entries")
    return entries


def _done_document() -> dict:
    """Return a fresh copy of Epitrop's accepted result with evidence."""
    fixture = mc.read_document(EPITROP_FIXTURES / "result-done-with-evidence.json")
    assert isinstance(fixture, dict)
    document = mc.parse_document(fixture["document"], fixture["id"])
    assert isinstance(document, dict)
    return document


def _load(name: str) -> dict:
    """Return a fresh copy of one battery fixture document."""
    document = mc.read_document(TESTDATA / name)
    assert isinstance(document, dict)
    return copy.deepcopy(document)


def _method() -> mc.Method:
    """Return the typed battery method definition."""
    return mc.method_from_document(_load("method-battery-spm.json"))


class EpitropFixtureTest(unittest.TestCase):
    """The vendored Epitrop schema and fixtures, pinned and replayed."""

    def test_vendored_files_match_the_pin(self) -> None:
        """Every vendored file has the digest vendor.json pins, and no file is unpinned."""
        pin_path = mc.EPITROP_DIR / "vendor.json"
        pin = mc.read_document(pin_path)
        assert isinstance(pin, dict)
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        on_disk = sorted(path.relative_to(mc.EPITROP_DIR).as_posix()
                         for path in _bounded_paths(mc.EPITROP_DIR.rglob("*"))
                         if path != pin_path and path.is_file())
        self.assertEqual(on_disk, sorted(pin["files"]))
        for name in on_disk:
            with self.subTest(name=name):
                digest = hashlib.sha256((mc.EPITROP_DIR / name).read_bytes()).hexdigest()
                self.assertEqual(digest, pin["files"][name])

    def test_walk_refuses_more_than_the_cap(self) -> None:
        """A walk accepts exactly the cap and consumes only one extra entry on failure."""
        at_cap = itertools.repeat(EPITROP_FIXTURES, MAX_VENDORED_ENTRIES)
        self.assertEqual(len(_bounded_paths(at_cap)), MAX_VENDORED_ENTRIES)
        over_cap = itertools.repeat(EPITROP_FIXTURES, MAX_VENDORED_ENTRIES + 2)
        with self.assertRaisesRegex(AssertionError, f"more than {MAX_VENDORED_ENTRIES} vendored entries"):
            _bounded_paths(over_cap)
        self.assertEqual(list(over_cap), [EPITROP_FIXTURES])

    def test_replays_every_fixture_in_both_directions(self) -> None:
        """A result Epitrop accepts reads here; one it refuses is refused with the reason Epitrop names."""
        fixtures = sorted(_bounded_paths(EPITROP_FIXTURES.glob("*.json")))
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
                reason = fixture["expect"]["says"]
                if path.name == "result-not-json.json":
                    reason = "not a JSON document"
                self.assertIn(reason, str(caught.exception))
        self.assertEqual(directions, {"compliant", "violation"})

    def test_typed_result_keeps_the_epitrop_fields(self) -> None:
        """The typed result carries Epitrop's field values unchanged, evidence included."""
        document = _done_document()
        result = mc.result_from_document(document)
        self.assertEqual((result.unit, result.outcome, result.head, result.summary),
                         (document["unit"], document["outcome"], document["head"], document["summary"]))
        self.assertEqual(result.evidence, (mc.Evidence(**document["evidence"][0]),))
        self.assertEqual(result.questions, ())


class ResultTest(unittest.TestCase):
    """Document rules and schema validation for an Epitrop slot result."""

    def test_patterns_refuse_a_trailing_newline(self) -> None:
        """Each pattern accepts the valid value and refuses a final newline at its JSON path."""
        document = _done_document()
        document["questions"] = ["ASK epitrop-unit-1-port: which port"]
        self.assertIsInstance(mc.result_from_document(document), mc.SlotResult)
        fields = (
            (document, "head", "$.head"),
            (document["evidence"][0], "sha256", "$.evidence[0].sha256"),
            (document["questions"], 0, "$.questions[0]"),
        )
        for container, key, path in fields:
            value = container[key]
            container[key] = value + "\n"
            with self.subTest(path=path):
                with self.assertRaisesRegex(mc.ContractError, re.escape(path) + r": .* does not match"):
                    mc.result_from_document(document)
            container[key] = value

    def test_refuses_unclean_evidence_paths(self) -> None:
        """Repeated separators, dot segments and trailing slashes fail the clean-path check."""
        document = _done_document()
        for suffix in ("logs//gate.log", "./gate.log", "gate.log/"):
            path = f".slot/out/evidence/{suffix}"
            document["evidence"][0]["path"] = path
            with self.subTest(path=path):
                reason = f"result: evidence {path!r} is not a clean relative path"
                with self.assertRaisesRegex(mc.ContractError, re.escape(reason)):
                    mc.result_from_document(document)

    def test_evidence_lines_require_a_json_integer(self) -> None:
        """Evidence line counts accept integer tokens and refuse decimal and exponent tokens."""
        document = _done_document()
        evidence = document["evidence"][0]
        evidence["lines"] = mc.parse_document("3", "lines")
        self.assertEqual(mc.result_from_document(document).evidence[0].lines, 3)
        for token, value in (("3.0", "3.0"), ("1e2", "100.0"), ("1e-400", "0.0")):
            evidence["lines"] = mc.parse_document(token, "lines")
            with self.subTest(token=token):
                reason = f"result: evidence {evidence['path']!r} lines {value} is not an integer"
                with self.assertRaisesRegex(mc.ContractError, re.escape(reason)):
                    mc.result_from_document(document)

    def test_schema_wraps_a_nesting_error(self) -> None:
        """A parseable document can exceed the recursion limit during schema traversal."""
        depth = 600
        document = mc.parse_document("[" * depth + "0" + "]" * depth, "deep")
        schema = {"type": "integer"}
        for _ in range(depth):
            schema = {"type": "array", "items": schema}
        validators = mc._validators()
        nested_validator = validators["result"].evolve(schema=schema)
        with mock.patch.dict(validators, result=nested_validator):
            with self.assertRaisesRegex(mc.ContractError, "^result: nested too deep$"):
                mc.result_from_document(document)


class MethodDefinitionTest(unittest.TestCase):
    """Reading method definitions."""

    def test_reads_the_battery_method(self) -> None:
        """The battery definition reads into typed fields."""
        method = _method()
        self.assertEqual(method.id, "battery-spm-constant-current")
        self.assertEqual(method.version, "26.9.0.0")
        self.assertEqual(method.result_class, "numerical-result")
        self.assertEqual(method.input("c_rate"), mc.Input("c_rate", "constant-current rate per nominal capacity",
                                                          "/h", True))
        self.assertIsNone(method.input("voltage"))
        self.assertIn(mc.Bound("c_rate", None, 0.5), method.validity_domain)

    def test_refuses_schema_breaks(self) -> None:
        """Unknown fields, moving versions, unknown result classes and open bounds are refused."""
        breaks = {
            "unknown field": lambda d: d.update(owner="x"),
            "version range": lambda d: d["implementation"].update(version=">=26.9"),
            "moving version": lambda d: d["implementation"].update(version="latest"),
            "result class": lambda d: d.update(result_class="opinion"),
            "bound with no end": lambda d: d["validity_domain"].append({"input": "step_duration"}),
            "no inputs": lambda d: d.update(inputs=[]),
            "empty verifier": lambda d: d["verifier"].update(command=[]),
        }
        for label, mutate in breaks.items():
            with self.subTest(label=label):
                document = _load("method-battery-spm.json")
                mutate(document)
                with self.assertRaises(mc.ContractError):
                    mc.method_from_document(document)

    def test_refuses_inconsistent_definitions(self) -> None:
        """A bound on an undeclared input, an inverted bound and a doubled input are refused."""
        cases = {
            "undeclared input": (lambda d: d["validity_domain"].append({"input": "pressure", "max": 1}),
                                 "undeclared input 'pressure'"),
            "min above max": (lambda d: d["validity_domain"][0].update(min=0.9), "min above max"),
            "input twice": (lambda d: d["inputs"].append(dict(d["inputs"][0])), "declared twice"),
            "two bounds": (lambda d: d["validity_domain"].append({"input": "c_rate", "min": 0}), "two validity"),
        }
        for label, (mutate, says) in cases.items():
            with self.subTest(label=label):
                document = _load("method-battery-spm.json")
                mutate(document)
                with self.assertRaisesRegex(mc.ContractError, says):
                    mc.method_from_document(document)

    def test_input_count_boundary(self) -> None:
        """Sixty-four inputs read; sixty-five are refused."""
        document = _load("method-battery-spm.json")
        extra = [{"name": f"x{index}", "quantity": "q", "unit": "1", "required": False} for index in range(59)]
        document["inputs"] += extra
        self.assertEqual(len(mc.method_from_document(document).inputs), 64)
        document["inputs"].append({"name": "x59", "quantity": "q", "unit": "1", "required": False})
        with self.assertRaises(mc.ContractError):
            mc.method_from_document(document)

    def test_point_bound_holds_only_at_its_point(self) -> None:
        """A bound with min equal to max holds at that value and nowhere else."""
        bound = mc.Bound("temperature", 298.15, 298.15)
        self.assertTrue(bound.holds(298.15))
        self.assertFalse(bound.holds(298.16))
        self.assertFalse(bound.holds(298.14))


class DocumentTest(unittest.TestCase):
    """Parsing contract documents before any schema runs."""

    def test_refuses_numbers_json_does_not_have(self) -> None:
        """NaN, Infinity, overflowing numbers and doubled keys are refused before any check runs."""
        cases = (
            ("nan", '{"value": NaN}', "NaN is not a JSON number"),
            ("infinity", '{"value": -Infinity}', "-Infinity is not a JSON number"),
            ("overflow", '{"value": 1e400}', "1e400 is not finite"),
            ("doubled key", '{"value": 1, "value": 2}', "key 'value' is given twice"),
        )
        for label, text, reason in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(mc.ContractError, f"{label}: not a JSON document: {reason}"):
                    mc.parse_document(text, label)

    def test_document_size_counts_utf8_bytes(self) -> None:
        """Bytes and multibyte text pass at the cap and fail at one byte over it."""
        value = "é" * ((mc.MAX_DOCUMENT_BYTES - 2) // 2)
        text = f'"{value}"'
        encoded = text.encode("utf-8")
        self.assertEqual(len(encoded), mc.MAX_DOCUMENT_BYTES)
        for at_cap, extra in ((text, " "), (encoded, b" ")):
            with self.subTest(input_type=type(at_cap).__name__):
                self.assertEqual(mc.parse_document(at_cap, "at cap"), value)
                with self.assertRaisesRegex(mc.ContractError, f"over cap: larger than {mc.MAX_DOCUMENT_BYTES} bytes"):
                    mc.parse_document(at_cap + extra, "over cap")

    def test_refuses_bom_and_non_utf8_bytes(self) -> None:
        """A BOM fails for text and bytes, and bytes always decode as UTF-8."""
        bom = '\ufeff{"value": 1}'
        for text in (bom, bom.encode("utf-8")):
            with self.subTest(input_type=type(text).__name__):
                with self.assertRaisesRegex(mc.ContractError, "bom: not a JSON document: .*UTF-8 BOM"):
                    mc.parse_document(text, "bom")
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                with self.assertRaisesRegex(mc.ContractError, "encoding: not a JSON document: .*can't decode byte"):
                    mc.parse_document("{}".encode(encoding), "encoding")

    def test_parse_wraps_a_nesting_error(self) -> None:
        """Excessive JSON nesting raises ContractError at the parse boundary."""
        text = "[" * 100_000 + "]" * 100_000
        with self.assertRaisesRegex(mc.ContractError, "^deep: nested too deep$"):
            mc.parse_document(text, "deep")

    def test_refuses_an_unreadable_document(self) -> None:
        """A missing file is refused with read context."""
        with self.assertRaisesRegex(mc.ContractError, "cannot read"):
            mc.read_document(mc.CONTRACTS_DIR / "absent.json")


if __name__ == "__main__":
    unittest.main()
