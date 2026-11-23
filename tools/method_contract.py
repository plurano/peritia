#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Typed reader for the Epitrop slot result, the result half of the Peritia method contract.

The result's shape is Epitrop's own schema, vendored at the commit contracts/epitrop/vendor.json pins
from https://github.com/tekmora/epitrop (EUPL-1.2). The schema decides shape; this module adds the one
rule Epitrop's reader states outside its schema and that needs no workspace: a done result needs its
head commit. Documents are parsed strictly: no NaN or Infinity, no number too large to hold, no key
given twice.
"""
from __future__ import annotations

import functools
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
EPITROP_DIR = CONTRACTS_DIR / "epitrop"
EPITROP_RESULT_SCHEMA_PATH = EPITROP_DIR / "result.schema.json"
MAX_DOCUMENT_BYTES = 1 << 20  # a contract document is small; refuse anything larger
MAX_REPORTED_ERRORS = 16  # bounds the schema errors read from one document (HISS-02)
DONE = "done"


class ContractError(ValueError):
    """A document breaks the contract; the message lists every reason found."""


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
    try:
        return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float,
                          object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeDecodeError) as err:
        raise ContractError(f"{origin}: not a JSON document: {err}") from err


def read_document(path: Path) -> object:
    """Read and parse one contract document from path."""
    try:
        with path.open("rb") as handle:
            text = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as err:
        raise ContractError(f"{path}: cannot read: {err}") from err
    return parse_document(text, str(path))


@functools.cache
def _validators() -> dict[str, Draft202012Validator]:
    """Compile the Epitrop result validator once, refusing a broken schema."""
    result_schema = read_document(EPITROP_RESULT_SCHEMA_PATH)
    if not isinstance(result_schema, dict):
        raise ContractError("a contract schema is not a JSON object")
    Draft202012Validator.check_schema(result_schema)
    registry = Registry().with_resources([(result_schema["$id"], Resource.from_contents(result_schema))])
    return {"result": Draft202012Validator(result_schema, registry=registry)}


def _checked(kind: str, document: object) -> dict:
    """Return document once it passes the kind's schema; else raise ContractError naming each error."""
    errors = itertools.islice(_validators()[kind].iter_errors(document), MAX_REPORTED_ERRORS)
    reasons = [f"{error.json_path}: {error.message}" for error in errors]
    if reasons:
        raise ContractError(f"{kind}: " + "; ".join(reasons))
    if not isinstance(document, dict):
        raise ContractError(f"{kind}: not a JSON object")
    return document


def _typed_result(document: dict) -> SlotResult:
    """Build the typed slot result from a document that passed Epitrop's result schema."""
    return SlotResult(
        unit=document["unit"],
        outcome=document["outcome"],
        head=document.get("head", ""),
        summary=document["summary"],
        evidence=tuple(Evidence(item["path"], item["sha256"], item["lines"])
                       for item in document.get("evidence", ())),
        questions=tuple(document.get("questions", ())),
    )


def result_from_document(document: object) -> SlotResult:
    """Return the typed Epitrop slot result a document states, or raise ContractError.

    Epitrop's reader also checks the run's unit id and each evidence file on disk; a result read here
    is one that reader already accepted, so those checks stay in Epitrop.
    """
    result = _typed_result(_checked("result", document))
    if result.outcome == DONE and not result.head:
        raise ContractError("result: outcome done needs the head commit")
    return result
