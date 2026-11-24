# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Identify inference inputs and preserve incompatible resume data."""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO, TextIO, TypeVar
from urllib.parse import SplitResult, urlsplit, urlunsplit

FINGERPRINT_VERSION = 1
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
MAX_RECORD_BYTES = 16 * 1024 * 1024
COPY_BLOCK_BYTES = 64 * 1024
TSV_DELIMITER = "\t"
Row = TypeVar("Row")
Key = TypeVar("Key")


def canonical_json(value: Any) -> str:
    """Serialize canonical JSON with Python's round-tripping float representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def text_sha256(text: str) -> str:
    """Hash the exact UTF-8 encoding of text."""
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprint(kind: str, fields: dict) -> str:
    """Hash a versioned set of inputs for a named operation."""
    return "sha256:" + text_sha256(canonical_json({"kind": kind, "version": FINGERPRINT_VERSION, **fields}))


def _split_base_url(base_url: str, option: str) -> tuple[SplitResult, str, int | None]:
    """Parse the endpoint and keep any parser diagnostics private."""
    message = f"invalid base URL for {option}"
    try:
        url = urlsplit(base_url)
        host, port = url.hostname, url.port
    except ValueError:
        raise ValueError(message) from None
    if not url.scheme or not host or "?" in base_url or "#" in base_url:
        raise ValueError(message)
    return url, host, port


def validate_base_url(base_url: str, option: str = "--base-url (PERITIA_LLM_BASE_URL)") -> str:
    """Return a usable transport URL or identify the setting that supplied it."""
    _split_base_url(base_url, option)
    return base_url


def resolve_base_urls(
    base_url: str | None, embed_base_url: str | None = None, *, inherit: bool = True,
) -> tuple[str, str]:
    """Resolve and validate chat and embedding endpoints using the same CLI and API defaults."""
    if base_url:
        chat = validate_base_url(base_url, "--base-url")
    else:
        chat = os.environ.get("PERITIA_LLM_BASE_URL", "") if inherit else ""
        if chat:
            validate_base_url(chat, "PERITIA_LLM_BASE_URL")
    embed = chat if embed_base_url is None else validate_base_url(embed_base_url, "--embed-base-url")
    return chat, embed


def provider_id(base_url: str | None) -> str:
    """Keep only the endpoint scheme, host, port and path."""
    if not base_url:
        return ""
    url, host, port = _split_base_url(base_url, "--base-url (PERITIA_LLM_BASE_URL)")
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))


def bounded_history(path: Path, rows: Iterable[Row], cap: int) -> Iterator[Row]:
    """Read at most the configured number of rows and detect a longer history."""
    iterator = iter(rows)
    yield from islice(iterator, cap)
    sentinel = object()
    if next(iterator, sentinel) is not sentinel:
        raise ValueError(f"{path}: history exceeds cap of {cap} rows")


def _lock_operation(handle: BinaryIO, *, acquire: bool) -> None:
    """Try or release the exclusive lock using the host's file-locking API."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def output_lock(path: Path) -> Iterator[None]:
    """Lock the output's stable sibling for a bounded wait. Linux tests do not exercise Windows locking."""
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    attempts = 1 + int(LOCK_WAIT_SECONDS / LOCK_POLL_SECONDS)
    with lock_path.open("a+b") as handle:
        for _ in range(attempts):
            try:
                _lock_operation(handle, acquire=True)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(LOCK_POLL_SECONDS, remaining))
            else:
                try:
                    yield
                finally:
                    _lock_operation(handle, acquire=False)
                return
    raise TimeoutError(f"{lock_path}: timed out waiting for output lock after {LOCK_WAIT_SECONDS:g}s")


def _archive_name(path: Path, kind: str, index: int) -> Path:
    """Format the numbered sibling archive path for an output."""
    return path.with_name(f"{path.stem}.{kind}-{index}{path.suffix}")


def _reserve_archive(path: Path, kind: str) -> Path:
    """Reserve an unused sibling archive name exclusively."""
    for n in range(1, 100):
        archive = _archive_name(path, kind, n)
        try:
            with archive.open("xb"):
                return archive
        except FileExistsError:
            continue
    raise ValueError(f"{path}: all 99 {kind} archive names are taken")


def _link_archive(path: Path, kind: str) -> Path | None:
    """Hard-link path to the first unused archive name, or return None if unsupported."""
    for n in range(1, 100):
        archive = _archive_name(path, kind, n)
        try:
            os.link(path, archive)
        except FileExistsError:
            continue
        except OSError:
            return None
        return archive
    raise ValueError(f"{path}: all 99 {kind} archive names are taken")


def _copy_archive(path: Path, archive: Path) -> None:
    """Stage a synced copy of path into a sibling temporary file and replace archive."""
    fd, raw_name = tempfile.mkstemp(prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent)
    os.close(fd)
    temporary = Path(raw_name)
    try:
        shutil.copyfile(path, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, archive)
    except OSError as error:
        if temporary.exists():
            archive.unlink(missing_ok=True)
        raise OSError(f"{path}: could not archive output to {archive}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _archive_output(path: Path, kind: str) -> Path:
    """Archive an active output via hard link or staged copy without removing path."""
    linked = _link_archive(path, kind)
    if linked is not None:
        return linked
    archive = _reserve_archive(path, kind)
    _copy_archive(path, archive)
    return archive


@contextmanager
def staged_output(path: Path, write: Callable[[BinaryIO], None]) -> Iterator[Path]:
    """Stage a complete file beside its destination, ready for atomic replacement."""
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            yield temporary
        finally:
            handle.close()
            temporary.unlink(missing_ok=True)


def digest_file(handle: BinaryIO) -> str:
    """Hash an open file and rewind it for a subsequent read."""
    handle.seek(0)
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
    handle.seek(0)
    return digest


def file_sha256(path: Path) -> str | None:
    """Return the current file's digest, or None when publication has yet to create it."""
    try:
        with path.open("rb") as handle:
            return digest_file(handle)
    except FileNotFoundError:
        return None


def _quarantine_tail(path: Path, boundary: int) -> None:
    """Persist the exact torn suffix before truncating the active output."""
    with path.open("r+b") as handle:
        length = handle.seek(0, os.SEEK_END) - boundary
        if not length:
            return
        handle.seek(boundary)
        archive = _reserve_archive(path, "torn")
        with archive.open("wb") as copy:
            for offset in range(0, length, COPY_BLOCK_BYTES):
                block = min(COPY_BLOCK_BYTES, length - offset)
                data = handle.read(block)
                if len(data) != block:
                    raise OSError(f"{path}: short read while quarantining {archive}")
                copy.write(data)
            copy.flush()
            os.fsync(copy.fileno())
        handle.truncate(boundary)
    print(f"{path}: quarantined {length} bytes to {archive}", file=sys.stderr)


@dataclass
class _TsvLines:
    """Track byte offsets while reading CSV's universal, untranslated physical lines."""

    handle: TextIO
    path: Path
    size: int
    position: int = 0
    boundary: int = 0

    def read(self) -> Iterator[str]:
        """Feed complete UTF-8 physical lines within a per-record byte cap."""
        for _ in range(self.size):
            remaining = MAX_RECORD_BYTES - (self.position - self.boundary)
            line = self.handle.readline(remaining + 1)
            length = len(line.encode("utf-8"))
            if length > remaining:
                raise ValueError(f"{self.path}: TSV record exceeds cap of {MAX_RECORD_BYTES} bytes")
            if not line.endswith(("\n", "\r")):
                return
            self.position += length
            yield line


def _tsv_prefix(path: Path, cap: int) -> tuple[list[str] | None, int, int]:
    """Return header, row count and end offset of the complete TSV prefix."""
    count = 0
    header = None
    with path.open(newline="", encoding="utf-8", errors="surrogateescape") as handle:
        source = _TsvLines(handle, path, os.fstat(handle.fileno()).st_size)
        rows = csv.reader(source.read(), delimiter=TSV_DELIMITER, strict=True)
        try:
            header = next(rows, None)
            source.boundary = source.position
            for row in rows:
                source.boundary = source.position
                count += bool(row)
                if count > cap:
                    raise ValueError(f"{path}: history exceeds cap of {cap} rows")
        except (UnicodeEncodeError, csv.Error):
            pass
    return header, count, source.boundary


def _jsonl_prefix(path: Path, cap: int) -> tuple[int, int]:
    """Return the line count and offset through the last newline-terminated JSON value."""
    count = boundary = 0
    with path.open("rb") as handle:
        for index in range(1, cap + 2):
            line = handle.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_BYTES:
                raise ValueError(f"{path}: JSONL record exceeds cap of {MAX_RECORD_BYTES} bytes")
            if not line.endswith(b"\n"):
                break
            if index > cap:
                raise ValueError(f"{path}: history exceeds cap of {cap} rows")
            try:
                json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            count, boundary = index, handle.tell()
    return count, boundary


def _pending_compaction(path: Path) -> Path:
    """Locate the staged active history used to recover an interrupted compaction."""
    return path.with_name(f".{path.name}.compacting")


def _finish_compaction(path: Path) -> None:
    """Complete a compaction interrupted after archiving the old active file."""
    pending = _pending_compaction(path)
    if pending.is_file():
        if path.exists():
            pending.unlink()
        else:
            os.replace(pending, path)


@dataclass
class AppendLog:
    """Track the active record count and enforce its cap under the caller's lock."""

    path: Path
    cap: int
    count: int
    encode: Callable[[dict], bytes]
    header: bytes = b""

    def _compact(self, payload: bytes, key: Key, latest: Mapping[Key, dict]) -> None:
        """Archive the active log and publish the staged latest records."""
        retained = len(latest) + (key not in latest)
        if retained > self.cap:
            raise ValueError(f"{self.path}: latest history exceeds cap of {self.cap} rows")

        def write(handle: BinaryIO) -> None:
            """Write the header and all retained records to the staged log."""
            handle.write(self.header)
            for previous_key, row in latest.items():
                if previous_key != key:
                    handle.write(self.encode(row))
            handle.write(payload)

        pending = _pending_compaction(self.path)
        with staged_output(self.path, write) as temporary:
            os.replace(temporary, pending)
            _archive_output(self.path, "history")
            os.replace(pending, self.path)
        self.count = retained

    def append(self, row: dict, key: Key, latest: Mapping[Key, dict]) -> None:
        """Append one complete record, compacting active history when required."""
        payload = self.encode(row)
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError(f"{self.path}: record exceeds cap of {MAX_RECORD_BYTES} bytes")
        if self.count >= self.cap:
            self._compact(payload, key, latest)
            return
        with self.path.open("ab") as handle:
            handle.write(payload)
        self.count += 1


@dataclass
class _TsvSink:
    """Accept whole records from csv.writer and emit UTF-8 bytes with LF separators."""

    emit: Callable[[bytes], object]

    def write(self, record: str) -> None:
        """Use LF as the record terminator while keeping CR quoted inside fields."""
        self.emit((record[:-2] + "\n").encode())


def write_tsv(
    emit: Callable[[bytes], object], fields: Sequence[str], rows: Iterable[dict], *, header: bool = True,
) -> None:
    """Serialize TSV with common quoting and byte encoding for logs and snapshots."""
    writer = csv.DictWriter(_TsvSink(emit), fieldnames=fields, delimiter=TSV_DELIMITER, lineterminator="\r\n")
    if header:
        writer.writeheader()
    writer.writerows(rows)


def tsv_sha256(fields: Sequence[str], rows: Iterable[dict]) -> str:
    """Hash the published encoding without materializing or staging the TSV."""
    digest = hashlib.sha256()
    write_tsv(digest.update, fields, rows)
    return digest.hexdigest()


def _tsv_record(fields: Sequence[str], row: dict | None = None) -> bytes:
    """Encode a header or one TSV record within the reader's CSV field limit."""
    out = io.BytesIO()
    if row is None:
        write_tsv(out.write, fields, ())
    else:
        if any(len(str(value)) > csv.field_size_limit() for value in row.values()):
            raise ValueError(f"TSV field exceeds cap of {csv.field_size_limit()} characters")
        write_tsv(out.write, fields, (row,), header=False)
    return out.getvalue()


def _prepare_tsv_history(path: Path, fields: list[str], fp_field: str, cap: int) -> tuple[int, bool]:
    """Archive legacy data or repair the current TSV and count its complete rows."""
    if not path.exists():
        return 0, True
    found, count, boundary = _tsv_prefix(path, cap)
    if found is None or found == fields:
        _quarantine_tail(path, boundary)
        return count, found is None
    if fp_field in found:
        raise ValueError(f"{path}: TSV header {found!r} differs from expected {fields!r}")
    archive = _archive_output(path, "legacy")
    label = "row" if count == 1 else "rows"
    print(f"{path}: moved {count} {label} to {archive} (legacy TSV)", file=sys.stderr)
    return 0, True


def open_fingerprinted_tsv(path: Path, fields: list[str], fp_field: str, *, max_rows: int) -> AppendLog:
    """Prepare a fingerprinted TSV under its output lock and return its append log."""
    _finish_compaction(path)
    count, needs_header = _prepare_tsv_history(path, fields, fp_field, max_rows)
    header = _tsv_record(fields)
    if needs_header:
        with staged_output(path, lambda handle: handle.write(header)) as temporary:
            os.replace(temporary, path)
    return AppendLog(path, max_rows, count, partial(_tsv_record, fields), header)


def _jsonl_record(record: dict) -> bytes:
    """Encode one trial as a newline-terminated JSON record."""
    return (json.dumps(record) + "\n").encode()


def open_jsonl_history(path: Path, cap: int) -> AppendLog:
    """Repair the JSONL tail under its output lock and return its append log."""
    _finish_compaction(path)
    count = 0
    if path.exists():
        count, boundary = _jsonl_prefix(path, cap)
        _quarantine_tail(path, boundary)
    return AppendLog(path, cap, count, _jsonl_record)
