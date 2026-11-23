#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the typed method contract in method_contract.py.

The Epitrop fixtures under contracts/epitrop/testdata/ are Epitrop's own result fixtures, replayed in both
directions: a result Epitrop accepts reads here, and one it refuses is refused here with the reason Epitrop names.
The battery fixtures under contracts/testdata/ follow skills/battery-modeling. Their validity domain is the tested
reference case of that skill, and their verifier program is a stand-in: the skill ships no helper script.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jsonschema.exceptions import ValidationError

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
    """Return the typed battery method the receipts are checked against."""
    return mc.method_from_document(_load("method-battery-spm.json"))


def _done() -> dict:
    """Return the done receipt document."""
    return _load("receipt-battery-spm-done.json")


def _blocked() -> dict:
    """Return the blocked receipt document."""
    return _load("receipt-battery-spm-blocked.json")


def _value(receipt: dict, name: str) -> dict:
    """Return the receipt's input value entry called name."""
    return next(item for item in receipt["inputs"] if item["name"] == name)


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
        self.assertIn(mc.Bound("c_rate", 0.5, 0.5), method.validity_domain)
        self.assertIn(mc.Bound("step_duration", 120, 600), method.validity_domain)

    def test_schema_root_validates_a_method(self) -> None:
        """The root schema accepts a full method and refuses unrelated values."""
        validator = mc._ContractValidator(mc.read_document(mc.METHOD_SCHEMA_PATH))
        validator.validate(_load("method-battery-spm.json"))
        for document, reason in ((None, "is not of type 'object'"), ({}, "'id' is a required property"),
                                 ({"owner": "x"}, "'owner' was unexpected")):
            with self.subTest(document=document):
                with self.assertRaisesRegex(ValidationError, reason):
                    validator.validate(document)

    def test_patterns_refuse_a_trailing_newline(self) -> None:
        """Version, unit and input name accept clean text and report the path of a final newline."""
        document = _load("method-battery-spm.json")
        step = next(item for item in document["inputs"] if item["name"] == "step_duration")
        index = document["inputs"].index(step)
        fields = (
            (document["implementation"], "version", "26.9.0.0", "$.implementation.version"),
            (step, "unit", "A", f"$.inputs[{index}].unit"),
            (step, "name", "step_duration", f"$.inputs[{index}].name"),
        )
        for container, key, value, path in fields:
            with self.subTest(path=path):
                container[key] = value
                self.assertIsInstance(mc.method_from_document(document), mc.Method)
                container[key] = value + "\n"
                with self.assertRaisesRegex(mc.ContractError, re.escape(path) + r": .* does not match"):
                    mc.method_from_document(document)
            container[key] = value

    def test_accepts_normalized_versions_and_revisions(self) -> None:
        """A normalized public release or a full lowercase revision identifies an exact version."""
        document = _load("method-battery-spm.json")
        versions = ("26.9.0.0", "1.0rc1", "2!1.0.post2.dev3", "abcdef0123" * 4, "1" * 64)
        for version in versions:
            with self.subTest(version=version):
                document["implementation"]["version"] = version
                self.assertEqual(mc.method_from_document(document).version, version)

    def test_refuses_noncanonical_versions(self) -> None:
        """Moving names, ranges, wildcards, noncanonical releases and short revisions fail."""
        document = _load("method-battery-spm.json")
        versions = ("latest", ">=26.9", "26.*", "v26.9.0.0", "2026-11-23", "01.2", "a" * 39)
        for version in versions:
            with self.subTest(version=version):
                document["implementation"]["version"] = version
                with self.assertRaisesRegex(mc.ContractError, r"\$\.implementation\.version: .* does not match"):
                    mc.method_from_document(document)
        document["implementation"]["version"] = "1" * 65
        with self.assertRaisesRegex(mc.ContractError, r"\$\.implementation\.version: .* is too long"):
            mc.method_from_document(document)

    def test_command_requires_only_the_program_to_be_nonempty(self) -> None:
        """A verifier needs a program and may pass an empty string argument."""
        document = _load("method-battery-spm.json")
        document["verifier"]["command"] = ["p", ""]
        self.assertEqual(mc.method_from_document(document).verifier_command, ("p", ""))
        for command, path in (([""], "$.verifier.command[0]"), ([], "$.verifier.command")):
            with self.subTest(command=command):
                document["verifier"]["command"] = command
                with self.assertRaisesRegex(mc.ContractError, re.escape(path) + r": .* should be non-empty"):
                    mc.method_from_document(document)

    def test_refuses_schema_breaks(self) -> None:
        """Unknown fields and result classes, bounds with no end and missing inputs are refused."""
        breaks = {
            "unknown field": (lambda d: d.update(owner="x"), r"\$: .*'owner' was unexpected"),
            "result class": (lambda d: d.update(result_class="opinion"), r"\$\.result_class: 'opinion' is not one of"),
            "bound with no end": (lambda d: d["validity_domain"].append({"input": "step_duration"}),
                                  r"\$\.validity_domain\[\d+\]: .* is not valid under any of the given schemas"),
            "no inputs": (lambda d: d.update(inputs=[]), r"\$\.inputs: \[\] should be non-empty"),
        }
        for label, (mutate, reason) in breaks.items():
            with self.subTest(label=label):
                document = _load("method-battery-spm.json")
                mutate(document)
                with self.assertRaisesRegex(mc.ContractError, reason):
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

    def test_refuses_nonfinite_bound_ends(self) -> None:
        """Python dictionaries cannot bypass the finite-number requirement on either bound end."""
        document = _load("method-battery-spm.json")
        for end, value in (("min", float("nan")), ("max", float("inf")), ("min", float("-inf"))):
            with self.subTest(end=end, value=value):
                document["validity_domain"] = [{"input": "c_rate", end: value}]
                with self.assertRaisesRegex(mc.ContractError, "validity bound of 'c_rate' has a non-finite end"):
                    mc.method_from_document(document)

    def test_refuses_boolean_bound_ends(self) -> None:
        """A boolean is refused by schema validation and by the typed definition check."""
        document = _load("method-battery-spm.json")
        document["validity_domain"] = [{"input": "c_rate", "min": True}]
        with self.assertRaisesRegex(mc.ContractError, r"\$\.validity_domain\[0\]\.min: True is not of type 'number'"):
            mc.method_from_document(document)
        method = mc.method_from_document(_load("method-battery-spm.json"))
        malformed = replace(method, validity_domain=(mc.Bound("c_rate", True, None),))
        self.assertEqual(mc._definition_reasons(malformed), ["validity bound of 'c_rate' has a non-finite end"])

    def test_input_count_boundary(self) -> None:
        """Sixty-four inputs read; sixty-five are refused."""
        document = _load("method-battery-spm.json")
        extra = [{"name": f"x{index}", "quantity": "q", "unit": "1", "required": False} for index in range(59)]
        document["inputs"] += extra
        self.assertEqual(len(mc.method_from_document(document).inputs), 64)
        document["inputs"].append({"name": "x59", "quantity": "q", "unit": "1", "required": False})
        with self.assertRaisesRegex(mc.ContractError, r"\$\.inputs: .* is too long"):
            mc.method_from_document(document)

    def test_point_bound_holds_only_at_its_point(self) -> None:
        """A bound with min equal to max holds at that value and nowhere else."""
        bound = mc.Bound("temperature", 298.15, 298.15)
        self.assertTrue(bound.holds(298.15))
        self.assertFalse(bound.holds(298.16))
        self.assertFalse(bound.holds(298.14))


class SchemaTest(unittest.TestCase):
    """Failures while loading the local schemas."""

    def test_schema_failure_names_the_file(self) -> None:
        """Invalid schema syntax and missing IDs raise ContractError with the schema path."""
        schema = mc.read_document(mc.METHOD_SCHEMA_PATH)
        assert isinstance(schema, dict)
        invalid = dict(schema, type="invalid")
        missing_id = dict(schema)
        del missing_id["$id"]
        self.addCleanup(mc._validators.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "method.schema.json"
            reason_prefix = "^" + re.escape(str(path)) + ": "
            for broken, reason in ((invalid, "invalid schema:"), (missing_id, r"missing \$id")):
                with self.subTest(reason=reason), mock.patch.object(mc, "METHOD_SCHEMA_PATH", path):
                    path.write_text(json.dumps(broken), encoding="utf-8")
                    mc._validators.cache_clear()
                    with self.assertRaisesRegex(mc.ContractError, reason_prefix + reason):
                        mc._validators()


class ReceiptTest(unittest.TestCase):
    """Reading receipts against the battery method."""

    def _refused(self, document: dict, says: str) -> None:
        """Assert the receipt is refused with a message containing says."""
        with self.assertRaises(mc.ContractError) as caught:
            mc.receipt_from_document(document, _method())
        self.assertIn(says, str(caught.exception))

    def test_reads_the_done_receipt(self) -> None:
        """A done receipt with every required input in domain and a passing verifier reads."""
        receipt = mc.receipt_from_document(_done(), _method())
        self.assertEqual(receipt.result.outcome, "done")
        self.assertEqual(receipt.verifier_outcome, "pass")
        self.assertEqual(receipt.inputs[2], mc.InputValue("c_rate", 0.5, "/h", ".slot/out/evidence/protocol.json"))
        self.assertEqual(len(receipt.result.evidence), 2)

    def test_reads_the_blocked_receipt(self) -> None:
        """A blocked receipt that asks for its missing input reads, with no verifier log."""
        receipt = mc.receipt_from_document(_blocked(), _method())
        self.assertEqual(receipt.missing, ("initial_soc",))
        self.assertEqual((receipt.verifier_outcome, receipt.verifier_evidence), ("not-run", ""))

    def test_optional_input_may_be_missing_from_a_done_run(self) -> None:
        """A missing optional input does not stop a done claim."""
        document = _done()
        document["missing"] = ["until_voltage"]
        self.assertEqual(mc.receipt_from_document(document, _method()).missing, ("until_voltage",))

    def test_domain_edges(self) -> None:
        """Values on a bound's edges read; the next value past an edge is refused."""
        document = _done()
        document["inputs"].append({"name": "until_voltage", "value": 2.5, "unit": "V",
                                   "evidence": ".slot/out/evidence/protocol.json"})
        mc.receipt_from_document(document, _method())
        _value(document, "until_voltage")["value"] = 4.2
        mc.receipt_from_document(document, _method())
        _value(document, "until_voltage")["value"] = 4.2000001
        self._refused(document, "'until_voltage' = 4.2000001 lies outside the validity domain")
        document = _done()
        _value(document, "c_rate")["value"] = 0.5000001
        self._refused(document, "'c_rate' = 0.5000001 lies outside the validity domain")

    def test_failed_run_may_record_an_out_of_domain_value(self) -> None:
        """A failed receipt records a value outside the domain; only a done claim needs the domain."""
        document = _done()
        document["result"]["outcome"] = "failed"
        document["verifier"] = {"outcome": "not-run"}
        _value(document, "c_rate")["value"] = 2.0
        self.assertEqual(mc.receipt_from_document(document, _method()).result.outcome, "failed")

    def test_refuses_insufficient_evidence(self) -> None:
        """Done claims without their inputs, verifier or cited evidence are refused."""
        cases = {
            "required input missing": (lambda d: (d["inputs"].pop(0), d.update(missing=["initial_soc"])),
                                       "the outcome must be blocked"),
            "required input unaccounted": (lambda d: d["inputs"].pop(0), "neither given nor listed as missing"),
            "verifier failed": (lambda d: d["verifier"].update(outcome="fail"), "needs a passing verifier"),
            "verifier not run": (lambda d: d.update(verifier={"outcome": "not-run"}), "needs a passing verifier"),
            "uncited evidence": (lambda d: d["inputs"][0].update(evidence=".slot/out/evidence/other.json"),
                                 "which the result does not list"),
            "verifier log not listed": (lambda d: d["verifier"].update(evidence=".slot/out/evidence/x.log"),
                                        "verifier cites"),
            "verifier ran without log": (lambda d: d["verifier"].pop("evidence"), "'evidence' is a required"),
        }
        for label, (mutate, says) in cases.items():
            with self.subTest(label=label):
                document = _done()
                mutate(document)
                self._refused(document, says)

    def test_refuses_blocked_run_that_does_not_ask(self) -> None:
        """A missing required input must be asked for by name."""
        document = _blocked()
        document["result"]["questions"] = ["ASK battery-measured-cell-soc: which soc did the cycler log"]
        self._refused(document, "no question asks for missing input 'initial_soc'")

    def test_refuses_invalid_inputs(self) -> None:
        """Undeclared inputs, wrong units, doubled values and inputs both given and missing are refused."""
        cases = {
            "undeclared": (lambda d: d["inputs"][0].update(name="pressure"), "'pressure' is not declared"),
            "wrong unit": (lambda d: d["inputs"][1].update(unit="Cel"), "is in Cel, the method reads K"),
            "given twice": (lambda d: d["inputs"].append(dict(d["inputs"][0])), "given twice"),
            "given and missing": (lambda d: d.update(missing=["c_rate"]), "both given and missing"),
            "other method": (lambda d: d.update(method="battery-dfn"), "not battery-spm-constant-current"),
            "boolean value": (lambda d: d["inputs"][0].update(value=True), "is not of type 'number'"),
        }
        for label, (mutate, says) in cases.items():
            with self.subTest(label=label):
                document = _done()
                mutate(document)
                self._refused(document, says)

    def test_refuses_results_epitrop_refuses(self) -> None:
        """The carried result is held to Epitrop's schema and its done-needs-head rule."""
        document = _done()
        document["result"]["merged"] = True
        self._refused(document, "'merged' was unexpected")
        document = _done()
        del document["result"]["head"]
        self._refused(document, "outcome done needs the head commit")


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
