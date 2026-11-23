#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Typed reader for the Peritia method and receipt contracts in tools/contracts/.

A method definition states what one executable method computes, from which inputs in which units,
under which model, with which exact tool version, where its result holds and how it is checked. A
receipt binds one run of that method to the Epitrop slot result of that run and carries it unchanged.
The result's shape is Epitrop's own schema, vendored at the commit contracts/epitrop/vendor.json pins
from https://github.com/tekmora/epitrop (EUPL-1.2).

The schemas decide shape. This module adds what a schema cannot state: each input is declared once,
a validity bound names a declared input and has finite ends with min at most max, and input names a
receipt refers to must be declared. For results it mirrors Epitrop's done-needs-head rule, the checks
on evidence path text, and the integer representation of evidence line counts. Epitrop's workspace
policy covers the run's unit id, the 64 KiB result-file cap, symlinks, and evidence file checks.
A receipt claims done only with every required input, every value inside the validity domain and a
passing verifier. A required input the run did not have makes it blocked, with a question naming it.
The receipt reader checks that the result satisfies its document-level evidence rules and lists each
cited path. It cannot prove that a cited file contains the stated input value or verifier outcome, or
that an unlisted optional input was unused. Follow-up work will bind each input value and the verifier
outcome to an evidence digest.
Documents are parsed as strict UTF-8 JSON with finite numbers and unique keys.
"""
from __future__ import annotations

import functools
import itertools
import json
import math
import posixpath
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator
from referencing import Registry, Resource
from regress import Regex, RegressError

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
METHOD_SCHEMA_PATH = CONTRACTS_DIR / "method.schema.json"
RECEIPT_SCHEMA_PATH = CONTRACTS_DIR / "receipt.schema.json"
EPITROP_DIR = CONTRACTS_DIR / "epitrop"
EPITROP_RESULT_SCHEMA_PATH = EPITROP_DIR / "result.schema.json"
MAX_DOCUMENT_BYTES = 1 << 20  # a contract document is small; refuse anything larger
MAX_REPORTED_ERRORS = 16  # bounds the schema errors read from one document (HISS-02)
DONE = "done"
BLOCKED = "blocked"
EVIDENCE_PREFIX = ".slot/out/evidence/"
_FLEET_QUESTION_ID_PREFIX = re.compile(r"^[a-z0-9][a-z0-9._]*-[a-z0-9._-]*: ")


class ContractError(ValueError):
    """A document breaks the contract; the message lists every reason found."""


@dataclass(frozen=True)
class Input:
    """One input a method reads: its name, physical quantity, unit and whether a run needs it."""

    name: str
    quantity: str
    unit: str
    required: bool


@dataclass(frozen=True)
class Bound:
    """Where a method's result holds for one input, in its unit, with inclusive ends.

    Comparisons use IEEE 754 binary64 for decimal and exponent JSON tokens (RFC 8259,
    section 6) and integers for integer tokens. For example, 0.80000000000000001 reads
    as 0.8 and meets a 0.8 point bound.
    """

    input: str
    min: float | None
    max: float | None

    def holds(self, value: float) -> bool:
        """Return whether value lies inside this bound."""
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


@dataclass(frozen=True)
class Method:
    """A typed method definition, read from a document that passed the method schema."""

    id: str
    skill: str
    task: str
    inputs: tuple[Input, ...]
    conventions: tuple[str, ...]
    model: str
    assumptions: tuple[str, ...]
    tool: str
    version: str
    validity_domain: tuple[Bound, ...]
    verifier_command: tuple[str, ...]
    acceptance: str
    result_class: str

    def input(self, name: str) -> Input | None:
        """Return the declared input called name, or None when the method has none."""
        return next((item for item in self.inputs if item.name == name), None)


@dataclass(frozen=True)
class Evidence:
    """One evidence entry of an Epitrop slot result: a file, its SHA-256 and its line count."""

    path: str
    sha256: str
    lines: int


@dataclass(frozen=True)
class SlotResult:
    """An Epitrop slot result with the field names of Epitrop's slot.Result."""

    unit: str
    outcome: str
    head: str
    summary: str
    evidence: tuple[Evidence, ...]
    questions: tuple[str, ...]


@dataclass(frozen=True)
class InputValue:
    """One value a run used, with the evidence entry it was read from."""

    name: str
    value: float
    unit: str
    evidence: str


@dataclass(frozen=True)
class Receipt:
    """A typed method receipt: the run's slot result, its inputs and what its verifier said."""

    method: str
    result: SlotResult
    inputs: tuple[InputValue, ...]
    missing: tuple[str, ...]
    verifier_outcome: str
    verifier_evidence: str


def _refuse_constant(name: str) -> float:
    """Refuse NaN and Infinity, which JSON does not have and every bound check would pass."""
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    """Parse a JSON number, refusing one too large to hold as a finite float."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not finite")
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a key given twice, which readers resolve differently."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"key {key!r} is given twice")
        seen[key] = value
    return seen


def parse_document(text: str | bytes, origin: str) -> object:
    """Parse one contract document from JSON text; origin names it in errors."""
    if len(text) > MAX_DOCUMENT_BYTES:
        raise ContractError(f"{origin}: larger than {MAX_DOCUMENT_BYTES} bytes")
    if isinstance(text, str):
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as err:
            raise ContractError(f"{origin}: not a JSON document: {err}") from err
        if size > MAX_DOCUMENT_BYTES:
            raise ContractError(f"{origin}: larger than {MAX_DOCUMENT_BYTES} bytes")
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float,
                          object_pairs_hook=_unique_keys)
    except RecursionError as err:
        raise ContractError(f"{origin}: nested too deep") from err
    except ValueError as err:
        raise ContractError(f"{origin}: not a JSON document: {err}") from err


def read_document(path: Path) -> object:
    """Read and parse one byte-capped document. Local repository schema reads have no deadline."""
    try:
        with path.open("rb") as handle:
            text = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as err:
        raise ContractError(f"{path}: cannot read: {err}") from err
    return parse_document(text, str(path))


@functools.lru_cache(maxsize=32)
def _compile_pattern(pattern: str) -> Regex:
    """Compile a JSON Schema pattern with ECMA-262 semantics."""
    return Regex(pattern)


def _pattern(_validator: Validator, pattern: str, instance: object, _schema: object) -> Iterator[ValidationError]:
    """Validate string instances using the JSON Schema regex dialect."""
    if isinstance(instance, str) and _compile_pattern(pattern).find(instance) is None:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


_ContractValidator = validators.extend(Draft202012Validator, validators={"pattern": _pattern})


@functools.cache
def _validators() -> dict[str, Validator]:
    """Compile the method, receipt and Epitrop result validators once, refusing a broken schema."""
    schemas = {}
    resources = []
    for path in (METHOD_SCHEMA_PATH, RECEIPT_SCHEMA_PATH, EPITROP_RESULT_SCHEMA_PATH):
        schema = read_document(path)
        if not isinstance(schema, dict):
            raise ContractError(f"{path}: not a JSON object")
        try:
            _ContractValidator.check_schema(schema)
            schema_id = schema["$id"]
        except SchemaError as err:
            raise ContractError(f"{path}: invalid schema: {err.message}") from err
        except KeyError as err:
            raise ContractError(f"{path}: missing $id") from err
        schemas[path] = schema
        resources.append((schema_id, Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    return {
        "method": _ContractValidator(schemas[METHOD_SCHEMA_PATH], registry=registry),
        "receipt": _ContractValidator(schemas[RECEIPT_SCHEMA_PATH], registry=registry),
        "result": _ContractValidator(schemas[EPITROP_RESULT_SCHEMA_PATH], registry=registry),
    }


def _schema_reason(kind: str, document: object, error: ValidationError) -> str:
    """Use the receipt's finite-number diagnostic when the schema refuses a boolean value."""
    if kind == "receipt" and isinstance(error.instance, bool) and isinstance(document, dict):
        match tuple(error.absolute_path):
            case ("inputs", index, "value"):
                name = document["inputs"][index].get("name")
                if isinstance(name, str):
                    return f"{error.json_path}: input {name!r} = {error.instance} is not a finite number"
    return f"{error.json_path}: {error.message}"


def _checked(kind: str, document: object) -> dict:
    """Return document once it passes the kind's schema; else raise ContractError naming each error."""
    try:
        errors = itertools.islice(_validators()[kind].iter_errors(document), MAX_REPORTED_ERRORS)
        reasons = [_schema_reason(kind, document, error) for error in errors]
    except RecursionError as err:
        raise ContractError(f"{kind}: nested too deep") from err
    except (RegressError, UnicodeError) as err:
        raise ContractError(f"{kind}: cannot validate pattern: {err}") from err
    if reasons:
        raise ContractError(f"{kind}: " + "; ".join(reasons))
    if not isinstance(document, dict):
        raise ContractError(f"{kind}: not a JSON object")
    return document


def _checked_evidence(entries: Sequence[dict]) -> tuple[Evidence, ...]:
    """Check and convert the at most 32 evidence entries allowed by the schema."""
    seen: set[str] = set()
    evidence: list[Evidence] = []
    for item in entries:
        path = item["path"]
        lines = item["lines"]
        if path in seen:
            raise ContractError(f"result: evidence {path!r} is listed twice")
        seen.add(path)
        if path.startswith("/") or path != posixpath.normpath(path):
            raise ContractError(f"result: evidence {path!r} is not a clean relative path")
        if not path.startswith(EVIDENCE_PREFIX):
            raise ContractError(f"result: evidence {path!r} is not under {EVIDENCE_PREFIX}")
        if type(lines) is not int:
            raise ContractError(f"result: evidence {path!r} lines {lines} is not an integer")
        evidence.append(Evidence(path, item["sha256"], lines))
    return tuple(evidence)


def _definition_reasons(method: Method) -> list[str]:
    """Return why a schema-valid definition is still inconsistent, or nothing when it is sound."""
    reasons = []
    names = [item.name for item in method.inputs]
    if len(set(names)) != len(names):
        reasons.append("an input name is declared twice")
    bounded = [bound.input for bound in method.validity_domain]
    if len(set(bounded)) != len(bounded):
        reasons.append("an input has two validity bounds")
    for bound in method.validity_domain:
        if method.input(bound.input) is None:
            reasons.append(f"validity bound names undeclared input {bound.input!r}")
        if any(isinstance(end, bool) or (isinstance(end, float) and not math.isfinite(end))
               for end in (bound.min, bound.max)):
            reasons.append(f"validity bound of {bound.input!r} has a non-finite end")
        elif bound.min is not None and bound.max is not None and bound.min > bound.max:
            reasons.append(f"validity bound of {bound.input!r} has min above max")
    return reasons


def method_from_document(document: object) -> Method:
    """Return the typed method a definition document states, or raise ContractError."""
    document = _checked("method", document)
    method = Method(
        id=document["id"],
        skill=document["skill"],
        task=document["task"],
        inputs=tuple(Input(**item) for item in document["inputs"]),
        conventions=tuple(document.get("conventions", ())),
        model=document["model"]["name"],
        assumptions=tuple(document["model"]["assumptions"]),
        tool=document["implementation"]["tool"],
        version=document["implementation"]["version"],
        validity_domain=tuple(Bound(item["input"], item.get("min"), item.get("max"))
                              for item in document["validity_domain"]),
        verifier_command=tuple(document["verifier"]["command"]),
        acceptance=document["verifier"]["acceptance"],
        result_class=document["result_class"],
    )
    reasons = _definition_reasons(method)
    if reasons:
        raise ContractError(f"method {method.id}: " + "; ".join(reasons))
    return method


def result_from_document(document: object) -> SlotResult:
    """Validate the document and convert it to a typed Epitrop slot result."""
    checked = _checked("result", document)
    outcome = checked["outcome"]
    head = checked.get("head", "")
    if outcome == DONE and not head:
        raise ContractError("result: outcome done needs the head commit")
    return SlotResult(
        unit=checked["unit"],
        outcome=outcome,
        head=head,
        summary=checked["summary"],
        evidence=_checked_evidence(checked.get("evidence", ())),
        questions=tuple(checked.get("questions", ())),
    )


def _input_reasons(method: Method, inputs: tuple[InputValue, ...], evidence: set[str]) -> list[str]:
    """Return why the receipt's input values do not fit the method's declared inputs."""
    reasons = []
    names = [value.name for value in inputs]
    if len(set(names)) != len(names):
        reasons.append("an input value is given twice")
    for value in inputs:
        declared = method.input(value.name)
        if declared is None:
            reasons.append(f"input {value.name!r} is not declared by method {method.id}")
        elif value.unit != declared.unit:
            reasons.append(f"input {value.name!r} is in {value.unit}, the method reads {declared.unit}")
        if isinstance(value.value, float) and not math.isfinite(value.value):
            reasons.append(f"input {value.name!r} = {value.value} is not a finite number")
        if value.evidence not in evidence:
            reasons.append(f"input {value.name!r} cites {value.evidence!r}, which the result does not list")
    return reasons


def _missing_reasons(method: Method, receipt: Receipt) -> list[str]:
    """Return why the receipt's missing list does not account for the method's required inputs."""
    reasons = []
    supplied = {value.name for value in receipt.inputs}
    for name in receipt.missing:
        if method.input(name) is None:
            reasons.append(f"missing input {name!r} is not declared by method {method.id}")
        if name in supplied:
            reasons.append(f"input {name!r} is both given and missing")
    for item in method.inputs:
        if item.required and item.name not in supplied and item.name not in receipt.missing:
            reasons.append(f"required input {item.name!r} is neither given nor listed as missing")
    return reasons


def _question_body(question: str) -> str:
    """Strip the kind and an optional hyphenated fleet ID, preserving input labels."""
    text = question.partition(" ")[2]
    return _FLEET_QUESTION_ID_PREFIX.sub("", text, count=1)


def _claim_reasons(method: Method, receipt: Receipt) -> list[str]:
    """Return why the receipt's outcome is not backed by its inputs and verifier."""
    reasons = []
    absent = [name for name in receipt.missing if (declared := method.input(name)) is not None and declared.required]
    if absent and receipt.result.outcome != BLOCKED:
        reasons.append(f"required inputs {absent} are missing, so the outcome must be blocked")
    for name in absent:
        asked = re.compile(rf"(?<![\w.]){re.escape(name)}(?!\w|\.\w)")
        if not any(asked.search(_question_body(question)) for question in receipt.result.questions):
            reasons.append(f"no question asks for missing input {name!r}")
    if receipt.result.outcome != DONE:
        return reasons
    if receipt.verifier_outcome != "pass":
        reasons.append(f"outcome done needs a passing verifier, it is {receipt.verifier_outcome}")
    for value in receipt.inputs:
        bound = next((item for item in method.validity_domain if item.input == value.name), None)
        if bound is not None and not bound.holds(value.value):
            reasons.append(f"input {value.name!r} = {value.value} lies outside the validity domain")
    return reasons


def receipt_from_document(document: object, method: Method) -> Receipt:
    """Return the typed receipt a document states for method, or raise ContractError.

    The reader enforces the result's document-level evidence rules and checks that every cited
    path is listed in the result. It cannot prove that a cited file holds the stated input value
    or verifier outcome, or that an unlisted optional input was unused. Follow-up work will bind
    each input value and the verifier outcome to an evidence digest.
    """
    document = _checked("receipt", document)
    result = result_from_document(document["result"])
    verifier = document["verifier"]
    receipt = Receipt(
        method=document["method"],
        result=result,
        inputs=tuple(InputValue(**item) for item in document["inputs"]),
        missing=tuple(document["missing"]),
        verifier_outcome=verifier["outcome"],
        verifier_evidence=verifier.get("evidence", ""),
    )
    evidence = {entry.path for entry in result.evidence}
    reasons = [] if receipt.method == method.id else [f"receipt is for method {receipt.method}, not {method.id}"]
    reasons += _input_reasons(method, receipt.inputs, evidence)
    if receipt.verifier_evidence and receipt.verifier_evidence not in evidence:
        reasons.append(f"verifier cites {receipt.verifier_evidence!r}, which the result does not list")
    reasons += _missing_reasons(method, receipt)
    reasons += _claim_reasons(method, receipt)
    if reasons:
        raise ContractError(f"receipt for {method.id}: " + "; ".join(reasons))
    return receipt
