# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Verify complete licence bodies against the repository's SPDX texts."""

import re
import stat
from collections.abc import Iterator
from itertools import islice, zip_longest
from pathlib import Path

try:
    from licence_policy import normalise_licence
except ImportError:
    from tools.licence_policy import normalise_licence

MAX_LICENCE_BYTES = 256 * 1024
LICENCE_NOTICES_FILE = "skills/licence-notices.yaml"
NOTICE_PREFIXES = ("Copyright ", "©")
APACHE_END = "end of terms and conditions"
PUNCTUATION = str.maketrans({
    c: '"' for c in "\"'‘’“”„‟«»‹›`"
} | dict.fromkeys("‐‑‒–—―−", "-"))
BULLET = re.compile(r"^[-*•◦▪]\s+")
_LABEL = r"[a-z]|[0-9]+(?:\.[0-9]+)*"
CLAUSE_LABEL = re.compile(rf"(?<!\S)(?:\(({_LABEL})\)|({_LABEL})[.)])(?=\s|$)")
_COMMENT = r"(?:(?P<comment><!--|/\*|//|--|[#;*])\s*)?"
METADATA = re.compile(
    rf"^{_COMMENT}(?P<kind>spdx-(?:filecopyrighttext|license-identifier|filecontributor)"
    r"|reuse-(?:copyright|license)):\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_IDENTIFIER_METADATA = frozenset({"spdx-license-identifier", "reuse-license"})
_COMMENT_END = {"/*": "*/", "<!--": "-->"}
TITLE = re.compile(
    r"(?:[a-z0-9-]+(?: [a-z0-9-]+){0,7} license(?: (?:v\.? )?\d+(?:\.\d+)*)?"
    r"|mit no attribution|creative commons legal code|cc0 1\.0 universal|the unlicense)"
)
APACHE_TITLE_CONTINUATION = re.compile(
    r"(?:version 2\.0, january 2004|http://www\.apache\.org/licenses/)"
)


class LicenceBodyDifference(ValueError):
    """A compared, nonempty licence body differs from the selected SPDX template."""


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


def _validate_metadata(match: re.Match[str], spdx: str) -> None:
    """Require standalone identifier metadata to agree with the declared SPDX ID."""
    if match["kind"].casefold() in _IDENTIFIER_METADATA:
        found = match["value"].removesuffix(_COMMENT_END.get(match["comment"], "")).strip()
        found_id, _, found_known = normalise_licence(found)
        declared_id, _, declared_known = normalise_licence(spdx)
        if not (found_known and declared_known and found_id == declared_id):
            raise ValueError(f"LICENSE metadata names {found or '<empty>'}; declared {spdx}")


def is_licence_notice(line: str, *, body_span: str = "") -> bool:
    """Recognise notice prefixes outside the space-padded template body."""
    return line.startswith(NOTICE_PREFIXES) and (
        not body_span or f" {_body_comparison_text(_normalise_body_line(line))} " not in body_span
    )


def _collapsed_licence_lines(text: str) -> Iterator[str]:
    """Yield whitespace-collapsed lines without altering case or punctuation."""
    for raw in islice(text.splitlines(), MAX_LICENCE_BYTES):
        if line := " ".join(raw.split()):
            yield line


def _notice_lines(text: str, *, body_span: str = "") -> frozenset[str]:
    """Collect exact notice lines for comparison with the template or register."""
    return frozenset(
        line for line in _collapsed_licence_lines(text) if is_licence_notice(line, body_span=body_span)
    )


def _normalise_body_line(line: str) -> str:
    """Normalize punctuation and remove the optional bullet prefix."""
    return BULLET.sub("", line.translate(PUNCTUATION))


def _normalised_lines(text: str, spdx: str, notices: frozenset[str], *, body_span: str = "") -> list[str]:
    """Remove reviewed notices and metadata, then normalise the remaining lines."""
    lines = []
    for line in _collapsed_licence_lines(text):
        if metadata := METADATA.fullmatch(line):
            _validate_metadata(metadata, spdx)
        elif is_licence_notice(line, body_span=body_span):
            if line not in notices:
                raise ValueError(f"unreviewed copyright notice {line!r}; review {LICENCE_NOTICES_FILE}")
        else:
            lines.append(_normalise_body_line(line))
    return lines


def _comparison_text(text: str) -> str:
    """Normalise case, licence spelling and URL schemes for text comparison."""
    return re.sub(r"\b((?:sub)?licen)c(e[s]?)\b", r"\1s\2", text.lower()).replace("https://", "http://")


def _body_comparison_text(text: str) -> str:
    """Normalize comparison text and clause labels for the licence body."""
    return CLAUSE_LABEL.sub(lambda match: f"({match[1] or match[2]})", _comparison_text(text))


def _title_lines(lines: list[str], spdx: str) -> list[str]:
    """Read only title-shaped lines in the leading block of the canonical text."""
    titles = []
    for raw in lines[:4]:
        line = _comparison_text(raw)
        if (line == spdx.lower() or TITLE.fullmatch(line)
                or (spdx == "Apache-2.0" and titles and APACHE_TITLE_CONTINUATION.fullmatch(line))):
            titles.append(line)
        else:
            break
    return titles


def _body(lines: list[str], titles: list[str], spdx: str) -> str:
    """Remove optional leading titles and normalise clause labels."""
    identifier = spdx.lower()
    for index, raw in enumerate(islice(lines, MAX_LICENCE_BYTES)):
        line = _comparison_text(raw)
        title_id = line.removeprefix("the ").removesuffix(" license")
        if line in titles or title_id == identifier:
            continue
        return _body_comparison_text(" ".join(lines[index:]))
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


def _read_licence_template(template_dir: Path, spdx: str) -> str:
    """Read the canonical template or report how to add the missing file."""
    try:
        return read_utf8(template_dir / f"{spdx}.txt", MAX_LICENCE_BYTES, refuse_links=True)
    except FileNotFoundError as exc:
        raise ValueError(f"LICENSES/{spdx}.txt missing; run reuse download {spdx}") from exc


def _template_comparison(template_dir: Path, spdx: str) -> tuple[list[str], str, frozenset[str]]:
    """Read the template's optional titles, normalized body and exact notices."""
    template = _read_licence_template(template_dir, spdx)
    notices = _notice_lines(template)
    lines = _normalised_lines(template, spdx, notices)
    titles = _title_lines(lines, spdx)
    body = _body(lines, titles, spdx)
    if not body:
        raise ValueError(f"LICENSES/{spdx}.txt has no licence body")
    return titles, body, notices


def unreviewed_licence_notices(path: Path, template_dir: Path, spdx: str,
                              reviewed: frozenset[str]) -> frozenset[str]:
    """Read the new notice lines in a staged, bounded licence file."""
    text = read_utf8(path, MAX_LICENCE_BYTES, refuse_links=True)
    _, expected, template_notices = _template_comparison(template_dir, spdx)
    return _notice_lines(text, body_span=f" {expected} ") - template_notices - reviewed


def verify_licence(path: Path, template_dir: Path, spdx: str, *, reviewed: frozenset[str] = frozenset()) -> None:
    """Raise a diagnostic unless the complete body matches the declared SPDX template."""
    text = read_utf8(path, MAX_LICENCE_BYTES, refuse_links=True)
    titles, expected, template_notices = _template_comparison(template_dir, spdx)
    actual_lines = _normalised_lines(text, spdx, template_notices | reviewed, body_span=f" {expected} ")
    actual = _body(actual_lines, titles, spdx).split()
    if not actual:
        raise ValueError("LICENSE has no licence body")
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
    raise LicenceBodyDifference(f"LICENSE differs from {spdx} at {mismatch[1]}")
