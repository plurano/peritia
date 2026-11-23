# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Identify inference inputs and preserve incompatible resume data."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from collections.abc import Iterable, Iterator
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO, TextIO, TypeVar
from urllib.parse import urlsplit, urlunsplit

FINGERPRINT_VERSION = 1
MAX_LEGACY_TSV_ROWS = 500_000
Row = TypeVar("Row")


def canonical_json(value: Any) -> str:
    """Serialize canonical JSON with Python's round-tripping float representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def text_sha256(text: str) -> str:
    """Hash the exact UTF-8 encoding of text."""
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprint(kind: str, fields: dict) -> str:
    """Hash a versioned set of inputs for a named operation."""
    return "sha256:" + text_sha256(canonical_json({"kind": kind, "version": FINGERPRINT_VERSION, **fields}))


def validate_base_url(base_url: str) -> str:
    """Return a usable transport URL or report its endpoint without userinfo."""
    endpoint = base_url.partition("?")[0].partition("#")[0]
    scheme, separator, address = endpoint.partition("//")
    if not separator:
        scheme, address = "", endpoint
    authority, slash, path = address.partition("/")
    display = scheme + separator + authority.rsplit("@", 1)[-1] + slash + path
    try:
        url = urlsplit(base_url)
        host, _port = url.hostname, url.port
    except ValueError as error:
        raise ValueError(f"invalid base URL {display!r}") from error
    if not url.scheme or not host or "?" in base_url or "#" in base_url:
        raise ValueError(f"invalid base URL {display!r}: expected a scheme and host with no query or fragment")
    return base_url


def resolve_base_urls(base_url: str | None, embed_base_url: str | None = None) -> tuple[str, str]:
    """Resolve and validate chat and embedding endpoints using the same CLI and API defaults."""
    chat = base_url or os.environ.get("PERITIA_LLM_BASE_URL", "")
    if chat:
        validate_base_url(chat)
    embed = validate_base_url(embed_base_url) if embed_base_url else chat
    return chat, embed


def provider_id(base_url: str | None) -> str:
    """Keep only the endpoint scheme, host, port and path."""
    if not base_url:
        return ""
    url = urlsplit(validate_base_url(base_url))
    host = url.hostname
    if ":" in host:
        host = f"[{host}]"
    if url.port is not None:
        host = f"{host}:{url.port}"
    return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))


def bounded_history(path: Path, rows: Iterable[Row], cap: int) -> Iterator[Row]:
    """Read at most the configured number of rows and detect a longer history."""
    iterator = iter(rows)
    yield from islice(iterator, cap)
    sentinel = object()
    if next(iterator, sentinel) is not sentinel:
        raise ValueError(f"{path}: history exceeds cap of {cap} rows")


def append_separator(handle: BinaryIO) -> bytes:
    """Return the separator needed to preserve an unterminated append-log tail."""
    if handle.seek(0, os.SEEK_END):
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) != b"\n":
            return b"\n"
    return b""


def _read_tsv_header(path: Path) -> list[str]:
    """Read the first TSV record from a migration source or archive."""
    with path.open(newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle, delimiter="\t"), [])


def _reserve_archive(path: Path) -> Path:
    """Reserve an unused sibling archive name without clobbering another writer."""
    for n in range(1, 100):
        archive = path.with_name(f"{path.stem}.legacy-{n}{path.suffix}")
        try:
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return archive
    raise ValueError(f"{path}: all 99 legacy archive names are taken")


def _archive_legacy_tsv(path: Path, count: int, legacy_header: list[str]) -> None:
    """Atomically archive legacy bytes on the same filesystem and check for races."""
    archive = _reserve_archive(path)
    try:
        found_header = _read_tsv_header(path)
    except OSError as error:
        archive.unlink(missing_ok=True)
        raise OSError(f"{path}: could not migrate TSV to {archive}: {error}") from error
    if found_header != legacy_header:
        archive.unlink(missing_ok=True)
        raise ValueError(f"{path}: TSV changed before migration to {archive}")
    try:
        os.replace(path, archive)
    except OSError as error:
        # A failed acknowledgement can leave the source bytes in the archive.
        raise OSError(f"{path}: could not migrate TSV to {archive}: {error}") from error
    if _read_tsv_header(archive) != legacy_header:
        raise ValueError(f"{path}: concurrent migration moved current data to {archive}")
    label = "row" if count == 1 else "rows"
    print(f"{path}: moved {count} {label} to {archive} (legacy TSV)", file=sys.stderr)


def _tsv_append_mode(path: Path, fields: list[str], fp_field: str, max_rows: int) -> str:
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
        count = sum(bool(row) for row in bounded_history(path, rows, max_rows))
    _archive_legacy_tsv(path, count, header)
    return "x"


def _open_tsv_output(path: Path, mode: str) -> TextIO:
    """Open a new TSV or put a line boundary before the next append."""
    if mode == "x":
        return path.open("x", newline="", encoding="utf-8")
    binary = path.open("a+b")
    try:
        separator = append_separator(binary)
        if separator:
            binary.write(separator)
            binary.flush()
        return io.TextIOWrapper(binary, newline="", encoding="utf-8")
    except OSError:
        binary.close()
        raise


def open_fingerprinted_tsv(
    path: Path, fields: list[str], fp_field: str, *, max_rows: int = MAX_LEGACY_TSV_ROWS,
) -> tuple[TextIO, csv.DictWriter]:
    """Return an open TSV handle and writer after validating or creating the schema."""
    mode = _tsv_append_mode(path, fields, fp_field, max_rows)
    handle = _open_tsv_output(path, mode)
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
    if mode == "x":
        try:
            writer.writeheader()
            handle.flush()
        except OSError:
            handle.close()
            raise
    return handle, writer
