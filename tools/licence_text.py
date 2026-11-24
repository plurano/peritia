# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Verify complete licence bodies against the repository's SPDX texts."""

import re
import stat
from itertools import islice, zip_longest
from pathlib import Path

MAX_LICENCE_BYTES = 256 * 1024
MAX_HOLDER_TOKENS = 12
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
COPYRIGHT = re.compile(
    rf"^{_COMMENT}(?:copyright(?: *(?:\(c\)|©)|(?= ))|\(c\)|©) *(?P<notice>.+)$",
    re.IGNORECASE,
)
_YEAR_RANGE = r"[0-9]{4}(?: *- *(?:[0-9]{4}|present))?"
_YEAR_LIST = rf"{_YEAR_RANGE}(?: *, *{_YEAR_RANGE})*"
COPYRIGHT_YEARS = re.compile(rf"^(?:{_YEAR_LIST}|\[{_YEAR_LIST}\]|<year>|\[yyyy\])(?= |$) *", re.IGNORECASE)
_HOLDER_WORD = r"\w+(?:[-.'\"&+]\w+)*[.+]?,?"
_HOLDER_NAME_TOKEN = re.compile(_HOLDER_WORD)
_HOLDER_EMAIL = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
_HOLDER_BRACKETS = re.compile(r"\[(?P<square>[^\[\]<>]+)\]|<(?P<angle>[^\[\]<>]+)>")
_HOLDER_PLACEHOLDER = re.compile(rf"\[{_HOLDER_WORD}\]|<{_HOLDER_WORD}>")
_HOLDER_PLACEHOLDERS = frozenset({"[name of copyright owner]", "<copyright holder>", "<copyright holders>"})
_HOLDER_HANDLE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_HOLDER_ALIAS = re.compile(rf"\({_HOLDER_HANDLE.pattern}\)")
_HOLDER_COLLECTIVES = frozenset({"contributors", "authors", "developers", "team"})
_HOLDER_CONNECTORS = _HOLDER_COLLECTIVES | {
    "and", "of", "the", "et", "al.", "de", "van", "von", "der", "da", "affiliates", "its",
}
_RIGHTS_RESERVED = re.compile(r"(?:^| )all rights reserved\.$", re.IGNORECASE)
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


def _copyright_notice(line: str, *, leading: bool = False) -> bool:
    """Recognise a complete attribution line with a bounded holder name."""
    match = COPYRIGHT.fullmatch(line)
    if match is None:
        return False
    notice = match["notice"].removesuffix(_COMMENT_END.get(match["comment"], "")).rstrip()
    years = COPYRIGHT_YEARS.match(notice)
    if years is None and not leading:
        return False
    holder = _RIGHTS_RESERVED.sub("", notice[years.end():] if years else notice)
    return _copyright_holder(holder)


def _copyright_holder(holder: str) -> bool:
    """Accept bounded names, handles and placeholders using original-case tokens.

    A single lowercase word that matches the handle shape is stripped as a whole holder.
    The remaining licence body must still equal the template.
    """
    handle, separator, collective = holder.partition(" ")
    if _HOLDER_HANDLE.fullmatch(handle) and (not separator or collective in _HOLDER_COLLECTIVES):
        return True
    bracketed = holder.removesuffix(".")
    if bracketed.lower() in _HOLDER_PLACEHOLDERS:
        return True
    brackets = _HOLDER_BRACKETS.fullmatch(bracketed)
    if brackets and " " in holder:
        holder = brackets["square"] or brackets["angle"]
    tokens = holder.split(" ", MAX_HOLDER_TOKENS)
    if len(tokens) > MAX_HOLDER_TOKENS:
        return False
    has_holder_token = False
    for token in tokens[:MAX_HOLDER_TOKENS]:
        if _holder_token(token):
            has_holder_token = True
        elif not _HOLDER_ALIAS.fullmatch(token):
            return False
    return has_holder_token


def _holder_token(token: str) -> bool:
    """Recognise an explicit connector, placeholder, email or name token."""
    bracketed = token.removesuffix(".")
    if token in {"&", "+", ","} or _HOLDER_EMAIL.fullmatch(bracketed) or _HOLDER_PLACEHOLDER.fullmatch(bracketed):
        return True
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1]
    if not _HOLDER_NAME_TOKEN.fullmatch(token):
        return False
    word = token.removesuffix(",")
    return (word[0].isupper() or word[0].isdecimal()
            or word in _HOLDER_CONNECTORS or word.removesuffix(".") in _HOLDER_CONNECTORS)


def _validate_metadata(match: re.Match[str], spdx: str) -> None:
    """Require standalone identifier metadata to agree with the declared SPDX ID."""
    if match["kind"].casefold() in _IDENTIFIER_METADATA:
        found = match["value"].removesuffix(_COMMENT_END.get(match["comment"], "")).strip()
        if found.casefold() != spdx.casefold():
            raise ValueError(f"LICENSE metadata names {found or '<empty>'}; declared {spdx}")


def _normalised_lines(text: str, spdx: str) -> list[str]:
    """Normalise line layout and remove standalone notices while retaining case."""
    lines = []
    for raw in islice(text.splitlines(), MAX_LICENCE_BYTES):
        if metadata := METADATA.fullmatch(raw.strip()):
            _validate_metadata(metadata, spdx)
            continue
        flat = " ".join(raw.translate(PUNCTUATION).split())
        if flat and not _copyright_notice(flat):
            lines.append(BULLET.sub("", flat))
    return lines


def _comparison_text(text: str) -> str:
    """Normalise case, licence spelling and URL schemes for text comparison."""
    return re.sub(r"\b((?:sub)?licen)c(e[s]?)\b", r"\1s\2", text.lower()).replace("https://", "http://")


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
    """Remove optional leading titles and undated copyright notices."""
    identifier = spdx.lower()
    for index, raw in enumerate(islice(lines, MAX_LICENCE_BYTES)):
        line = _comparison_text(raw)
        title_id = line.removeprefix("the ").removesuffix(" license")
        if line in titles or title_id == identifier or _copyright_notice(raw, leading=True):
            continue
        body = _comparison_text(" ".join(lines[index:]))
        return CLAUSE_LABEL.sub(lambda match: f"({match[1] or match[2]})", body)
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
    template_lines = _normalised_lines(template, spdx)
    titles = _title_lines(template_lines, spdx)
    expected = _body(template_lines, titles, spdx)
    if not expected:
        raise ValueError(f"LICENSES/{spdx}.txt has no licence body")
    actual = _body(_normalised_lines(text, spdx), titles, spdx).split()
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
