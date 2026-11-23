# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Verify complete licence bodies against the repository's SPDX texts."""

import re
import stat
from itertools import zip_longest
from pathlib import Path

MAX_LICENCE_BYTES = 256 * 1024
APACHE_END = "end of terms and conditions"
PUNCTUATION = str.maketrans({
    c: '"' for c in "\"'‘’“”„‟«»‹›`"
} | dict.fromkeys("‐‑‒–—―−", "-"))
BULLET = re.compile(r"^(?:[-*•◦▪]|[0-9]+(?:\.[0-9]+)*[.)]|\((?:[a-z]|[0-9]+)\)|[a-z][.)])\s+")
METADATA = re.compile(
    r"^(?:(?:[#;*]|//)\s*)?(?:"
    r"copyright\s+(?=\(c\)|©|[0-9<\[])|©\s*|\(c\)\s+(?=[0-9<\[])"
    r"|(?:spdx-(?:filecopyrighttext|license-identifier|filecontributor)|reuse-(?:copyright|license)):"
    r")"
)
TITLE = re.compile(
    r"(?:[a-z0-9-]+(?: [a-z0-9-]+){0,7} license(?: (?:v\.? )?\d+(?:\.\d+)*)?"
    r"|mit no attribution|creative commons legal code|cc0 1\.0 universal|the unlicense)"
)
APACHE_TITLE_CONTINUATION = re.compile(
    r"(?:version 2\.0, january 2004|http://www\.apache\.org/licenses/)"
)


def read_utf8(path: Path, limit: int, *, refuse_links: bool = False) -> str:
    """Read a bounded regular file, accepting a UTF-8 BOM and reporting read failures."""
    try:
        mode = (path.lstat() if refuse_links else path.stat()).st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"{path.name} is a symlink")
        if stat.S_ISDIR(mode):
            raise ValueError(f"{path.name} is a directory")
        if not stat.S_ISREG(mode):
            raise ValueError(f"{path.name} is not a regular file")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{path.name} missing") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path.name} ({exc.strerror or exc})") from exc
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds {limit // 1024} KiB")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} has invalid UTF-8 at byte {exc.start}") from exc


def _normalised_lines(text: str) -> list[str]:
    """Normalise presentation and remove standalone copyright and SPDX/REUSE notices."""
    lines = []
    for raw in text.splitlines():
        flat = " ".join(raw.lower().translate(PUNCTUATION).split())
        flat = re.sub(r"\b((?:sub)?licen)c(e[s]?)\b", r"\1s\2", flat).replace("https://", "http://")
        if flat and not METADATA.match(flat):
            lines.append(BULLET.sub("", flat))
    return lines


def _title_lines(lines: list[str], spdx: str) -> list[str]:
    """Read only title-shaped lines in the leading block of the canonical text."""
    titles = []
    for line in lines[:4]:
        if line == spdx.lower() or TITLE.fullmatch(line):
            titles.append(line)
        elif spdx == "Apache-2.0" and titles and APACHE_TITLE_CONTINUATION.fullmatch(line):
            titles.append(line)
        else:
            break
    return titles


def _body(lines: list[str], titles: list[str], spdx: str) -> str:
    """Remove optional leading titles and undated copyright notices."""
    identifier = spdx.lower()
    for index, line in enumerate(lines):
        title_id = line.removeprefix("the ").removesuffix(" license")
        if line in titles or title_id == identifier or line.startswith("copyright "):
            continue
        return " ".join(lines[index:])
    return ""


def _first_difference(actual: list[str], expected: list[str]) -> tuple[int, str] | None:
    """Return the first zero-based differing word offset and its expected/actual words."""
    for offset, (found, want) in enumerate(zip_longest(actual, expected)):
        if found != want:
            # A final full stop may be absent on an otherwise complete text.
            if (found is not None and want is not None
                    and offset == len(actual) - 1 == len(expected) - 1
                    and found.removesuffix(".") == want.removesuffix(".")):
                continue
            found_word = "<end>" if found is None else repr(found[:80])
            want_word = "<end>" if want is None else repr(want[:80])
            return offset, f"word offset {offset} (expected {want_word}, found {found_word})"
    return None


def verify_licence(path: Path, template_dir: Path, spdx: str) -> None:
    """Raise a diagnostic unless the complete body matches the declared SPDX template."""
    text = read_utf8(path, MAX_LICENCE_BYTES, refuse_links=True)
    try:
        template = read_utf8(template_dir / f"{spdx}.txt", MAX_LICENCE_BYTES, refuse_links=True)
    except FileNotFoundError as exc:
        raise ValueError(f"LICENSES/{spdx}.txt missing; run reuse download {spdx}") from exc
    template_lines = _normalised_lines(template)
    titles = _title_lines(template_lines, spdx)
    expected = _body(template_lines, titles, spdx)
    if not expected:
        raise ValueError(f"LICENSES/{spdx}.txt has no licence body")
    actual = _body(_normalised_lines(text), titles, spdx).split()
    mismatch = _first_difference(actual, expected.split())
    if mismatch is None:
        return
    if spdx == "Apache-2.0":
        terms, marker, _ = expected.partition(APACHE_END)
        if marker:
            short_mismatch = _first_difference(actual, (terms + marker).split())
            if short_mismatch is None:
                return
            mismatch = max(mismatch, short_mismatch)
    raise ValueError(f"LICENSE differs from {spdx} at {mismatch[1]}")
