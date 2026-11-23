#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Identify inference inputs and preserve incompatible resume data."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

FINGERPRINT_VERSION = 1


def canonical_json(value: Any) -> str:
    """Serialize canonical JSON with Python's round-tripping float representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def text_sha256(text: str) -> str:
    """Hash the exact UTF-8 encoding of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(kind: str, fields: dict) -> str:
    """Hash a versioned set of inputs for a named operation."""
    return "sha256:" + text_sha256(canonical_json({"kind": kind, "version": FINGERPRINT_VERSION, **fields}))


def provider_id(base_url: str | None) -> str:
    """Keep only the endpoint scheme, host, port and path."""
    if not base_url:
        return ""
    url = urlsplit(base_url)
    host = url.hostname
    if host is None:
        raise ValueError("base URL must include a host")
    if ":" in host:
        host = f"[{host}]"
    if url.port is not None:
        host = f"{host}:{url.port}"
    return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))


def _archive_legacy_tsv(path: Path, count: int) -> None:
    """Move legacy TSV bytes to the first available archive without clobbering another file."""
    for n in range(1, 100):
        archive = path.with_name(f"{path.stem}.legacy-{n}{path.suffix}")
        try:
            archive.hardlink_to(path)
        except FileExistsError:
            continue
        path.unlink()
        label = "row" if count == 1 else "rows"
        print(f"{path}: moved {count} {label} to {archive} (legacy TSV)", file=sys.stderr)
        return
    raise ValueError(f"{path}: all 99 legacy archive names are taken")


def _tsv_append_mode(path: Path, fields: list[str], fp_field: str) -> str:
    """Select append or create mode after checking and migrating the header."""
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError:
        return "x"
    with handle:
        rows = csv.reader(handle, delimiter="\t")
        header = next(rows, [])
        if header == fields:
            return "a"
        if fp_field in header:
            raise ValueError(f"{path}: TSV header {header!r} differs from expected {fields!r}")
        count = sum(1 for row in rows if row)
    _archive_legacy_tsv(path, count)
    return "x"


def open_fingerprinted_tsv(path: Path, fields: list[str], fp_field: str) -> tuple[TextIO, csv.DictWriter]:
    """Return an open TSV handle and writer after validating or creating the schema."""
    mode = _tsv_append_mode(path, fields, fp_field)
    handle = path.open(mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
    if mode == "x":
        try:
            writer.writeheader()
            handle.flush()
        except OSError:
            handle.close()
            raise
    return handle, writer
