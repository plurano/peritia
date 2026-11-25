#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check every skill directory under skills/: a SKILL.md whose front matter names the directory and says when to
use it; an ORIGIN.md with the upstream repository, path, a full 40-hex pinned commit, the licence and the changes
made here (or "own" for a skill written here); the upstream LICENSE beside any adapted skill; and no command that
installs packages or fetches code when the skill runs.
Usage: check_skills.py [ROOT] | check_skills.py selftest
"""
import io
import os
import re
import stat
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import closing, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import islice
from pathlib import Path
from unittest.mock import patch

import yaml
from markdown_it import MarkdownIt

try:
    from install_user_skills import read_install_set
    from licence_policy import (
        candidate_licence_id,
        find_folder_licence_file,
        licence_pointer_name,
    )
    from licence_text import (
        LICENCE_NOTICES_FILE,
        MAX_LICENCE_BYTES,
        LicenceBodyDifference,
        is_licence_notice,
        read_utf8,
        verify_licence,
    )
except ImportError:
    from tools.install_user_skills import read_install_set
    from tools.licence_policy import (
        candidate_licence_id,
        find_folder_licence_file,
        licence_pointer_name,
    )
    from tools.licence_text import (
        LICENCE_NOTICES_FILE,
        MAX_LICENCE_BYTES,
        LicenceBodyDifference,
        is_licence_notice,
        read_utf8,
        verify_licence,
    )

COMMONMARK = MarkdownIt("commonmark")
MAX_SKILLS = 5000  # bounds the directories read (HISS-02)
MAX_SKILL_ENTRIES = 1000  # bounds the entries walked in one skill (HISS-02)
MAX_SKILL_MD_BYTES = 2 * 1024 * 1024
MAX_ORIGIN_BYTES = 256 * 1024
MAX_YAML_MAPPING_KEYS = 1000
MAX_YAML_EVENTS = 32_000
MAX_YAML_DEPTH = 32
MAX_LICENCE_NOTICES = 512
MAX_LICENCE_NOTICE_CHARS = 200
MAX_LICENCE_EXCEPTIONS = 128
LICENCE_EXCEPTIONS_FILE = "skills/licence-exceptions.yaml"
LICENCE_EXCEPTION_KEYS = frozenset({"rule", "path", "spdx", "sha256", "reason", "category", "entered", "expires"})
LICENCE_EXCEPTION_DAYS = {"B": 3, "C": 10, "D": 120}
AGENT_CONTROL_FILES = frozenset({
    "protocol.md", "agents.md", "claude.md", "gemini.md",
    ".cursorrules", ".windsurfrules", "copilot-instructions.md",
})
FRONT = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
FIELD = re.compile(r"^(?P<key>[a-z_]+):[^\S\r\n]*(?P<value>[^\r\n]*)$", re.MULTILINE)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
INLINE_CODE = re.compile(r"`[^`]*`")
MD_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CHECKED_LINK_SUFFIXES = (".md", ".markdown", ".mdx", ".py", ".sh", ".mjs", ".js", ".ts")
# run-time installs and remote code: a skill must use the tools the project already pins
FETCH = re.compile(r"\b(?:pip install|uv pip install|uvx |npx |npm install|pnpm add|yarn add|go install|cargo install|"
                   r"curl[^\n|]*\|\s*(?:ba)?sh|wget[^\n|]*\|\s*(?:ba)?sh)")


def _normalize_line_endings(text: str) -> str:
    """Translate CRLF and lone CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fields(text):
    """Read the scalar provenance fields in ORIGIN.md."""
    text = _normalize_line_endings(text)
    values = {}
    for match in islice(FIELD.finditer(text), MAX_ORIGIN_BYTES):
        key = match["key"]
        if key in values:
            raise ValueError(f"ORIGIN.md: duplicate provenance key {key!r}")
        values[key] = match["value"].strip()
    return values


class _YamlReadError(ValueError):
    """A YAML document could not be parsed or safely constructed."""


class _YamlMappingError(ValueError):
    """A strict YAML mapping failed key validation."""


class _YamlStructureError(ValueError):
    """YAML events violated the supported document structure or limits."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Load validation YAML with unique keys at every mapping level."""

    def _require_unique_keys(self, node):
        """Check mapping keys before constructing values."""
        if len(node.value) > MAX_YAML_MAPPING_KEYS:
            raise _YamlMappingError(f"mapping exceeds {MAX_YAML_MAPPING_KEYS} keys")
        seen = set()
        for key_node, _ in node.value:
            tag = key_node.tag
            key = key_node.value if tag == "tag:yaml.org,2002:value" else self.construct_object(key_node)
            try:
                if key in seen:
                    raise _YamlMappingError(f"duplicate key {key!r}")
                seen.add(key)
            except TypeError as exc:
                raise _YamlMappingError(f"unhashable YAML key {key!r}") from exc

    def construct_mapping(self, node, deep=False):
        """Refuse duplicate literal keys before constructing values."""
        self._require_unique_keys(node)
        return super().construct_mapping(node, deep=deep)


def _check_yaml_events(text: str, loader: type[yaml.SafeLoader]) -> None:
    """Reject aliases and bound parser events and depth before composition."""
    depth = 0
    with closing(yaml.parse(text, Loader=loader)) as events:
        for count, event in enumerate(islice(events, MAX_YAML_EVENTS + 1), 1):
            if count > MAX_YAML_EVENTS:
                raise _YamlStructureError(
                    f"document exceeds {MAX_YAML_EVENTS} YAML events"
                )
            if isinstance(event, yaml.AliasEvent):
                raise _YamlStructureError("YAML aliases are not allowed")
            if isinstance(event, yaml.CollectionStartEvent):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise _YamlStructureError(
                        f"YAML nesting depth exceeds {MAX_YAML_DEPTH}"
                    )
            elif isinstance(event, yaml.CollectionEndEvent):
                depth -= 1


def _check_yaml_nodes(node: yaml.Node) -> None:
    """Reject merge keys before PyYAML can expand a mapping."""
    pending = [node]
    for _ in range(MAX_YAML_EVENTS):
        if not pending:
            return
        current = pending.pop()
        if isinstance(current, yaml.MappingNode):
            for key, value in current.value:
                if key.tag == "tag:yaml.org,2002:merge" or (isinstance(key, yaml.ScalarNode) and key.value == "<<"):
                    raise ValueError("YAML merge keys are not allowed")
                pending.extend((key, value))
        elif isinstance(current, yaml.SequenceNode):
            pending.extend(current.value)
    raise ValueError(f"document exceeds {MAX_YAML_EVENTS} YAML nodes")


def _load_yaml(text: str, loader: type[yaml.SafeLoader], *, error_prefix: str = "") -> object:
    """Validate the YAML structure before constructing values with a safe loader."""
    try:
        _check_yaml_events(text, loader)
        parser = loader(text)
        try:
            node = parser.get_single_node()
            if node is None:
                return None
            _check_yaml_nodes(node)
            return parser.construct_document(node)
        finally:
            parser.dispose()
    except _YamlMappingError:
        raise
    except (yaml.YAMLError, AttributeError, IndexError, KeyError, OverflowError, RecursionError, TypeError, ValueError) as exc:
        message = str(exc)
        if error_prefix:
            message = error_prefix + " ".join(message.split())
        raise _YamlReadError(message) from exc


@dataclass(frozen=True)
class _LicenceException:
    """Identify the reviewed bytes and expiry for one skill licence."""

    path: str
    spdx: str
    sha256: str
    expires: date


@dataclass
class LicenceScan:
    """Track reviewed notices, applicable pins and verified licence counts."""

    exceptions: dict[str, _LicenceException]
    today: date
    notices: frozenset[str] = frozenset()
    checked: set[str] = field(default_factory=set)
    pinned: int = 0
    verified: Counter[str] = field(default_factory=Counter)

    def copy(self) -> "LicenceScan":
        """Start independent validation using the same entries and check date."""
        return LicenceScan(self.exceptions, self.today, self.notices)


def _exception_fields(entry: object) -> dict[str, str]:
    """Require exactly the allowed keys and a nonempty string for every value."""
    if not isinstance(entry, dict):
        raise TypeError("expected a mapping")
    if unknown := entry.keys() - LICENCE_EXCEPTION_KEYS:
        raise ValueError(f"unknown key(s): {', '.join(sorted(map(repr, unknown)))}")
    if missing := LICENCE_EXCEPTION_KEYS - entry.keys():
        raise ValueError(f"missing key(s): {', '.join(sorted(missing))}")
    for key in sorted(LICENCE_EXCEPTION_KEYS):
        value = entry[key]
        if not isinstance(value, str):
            raise TypeError(f"{key}: expected a string")
        if not value.strip():
            raise ValueError(f"{key}: expected a nonempty string")
    return entry


def _exception_date(entry: dict[str, str], key: str) -> date:
    """Parse a register date in the exact YYYY-MM-DD format."""
    value = entry[key]
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{key}: expected a date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key}: invalid date {value!r}") from exc


def _exception_expiry(entry: dict[str, str], today: date) -> date:
    """Apply the declared category's expiry interval."""
    category = entry["category"]
    entered = _exception_date(entry, "entered")
    expires = _exception_date(entry, "expires")
    if entered > today:
        raise ValueError(f"entered: {entered} is after check date {today}")
    if expires < entered:
        raise ValueError(f"expires: {expires} is before entered date {entered}")
    if category == "A":
        return expires
    days = LICENCE_EXCEPTION_DAYS.get(category)
    if days is None:
        raise ValueError(f"category: expected A, B, C or D, found {category!r}")
    try:
        expected = entered + timedelta(days=days)
    except OverflowError as exc:
        raise ValueError(f"entered: {entered} leaves no expiry date for category {category}") from exc
    if expires != expected:
        raise ValueError(f"expires: category {category} requires {expected}, found {expires}")
    return expires


def _parse_licence_exception(raw: object, today: date) -> _LicenceException:
    """Validate the rule, skill path, SPDX ID, byte hash and expiry."""
    entry = _exception_fields(raw)
    if entry["rule"] != "skill-licence-text":
        raise ValueError("rule: expected skill-licence-text")
    path = entry["path"]
    match = re.fullmatch(r"skills/([^/\\]+)/LICENSE", path)
    if not match or match[1] in {".", ".."} or not path.isprintable():
        raise ValueError("path: expected skills/<name>/LICENSE")
    spdx = entry["spdx"]
    if candidate_licence_id(spdx) != spdx:
        raise ValueError(f"spdx: expected a supported SPDX identifier, found {spdx!r}")
    digest = entry["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("sha256: expected 64 lowercase hexadecimal characters")
    return _LicenceException(path, spdx, digest, _exception_expiry(entry, today))


def _read_licence_exceptions(root: Path, today: date | None = None) -> dict[str, _LicenceException]:
    """Read the optional register and index validated entries by skill licence path."""
    try:
        text = read_utf8(root / LICENCE_EXCEPTIONS_FILE, MAX_LICENCE_BYTES, refuse_links=True)
    except FileNotFoundError:
        return {}
    data = _load_yaml(text, _UniqueKeyLoader, error_prefix="invalid YAML: ")
    if not isinstance(data, dict) or data.keys() != {"exceptions"}:
        raise ValueError("expected a mapping with exactly the key exceptions")
    entries = data["exceptions"]
    if not isinstance(entries, list):
        raise TypeError("exceptions: expected a list")
    if len(entries) > MAX_LICENCE_EXCEPTIONS:
        raise ValueError(f"exceptions: expected at most {MAX_LICENCE_EXCEPTIONS} entries, found {len(entries)}")
    check_date = datetime.now(UTC).date() if today is None else today
    by_path = {}
    for index, raw in enumerate(entries, 1):
        try:
            entry = _parse_licence_exception(raw, check_date)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"entry {index}: {exc}") from exc
        if entry.path in by_path:
            raise ValueError(f"entry {index}: duplicate path {entry.path}")
        by_path[entry.path] = entry
    return by_path


def _licence_notice_entry(raw: object, index: int) -> str:
    """Require one canonical, bounded notice string."""
    if not isinstance(raw, str):
        raise TypeError(f"notice {index}: expected a string")
    if len(raw) > MAX_LICENCE_NOTICE_CHARS:
        raise ValueError(f"notice {index}: expected at most {MAX_LICENCE_NOTICE_CHARS} characters")
    if raw != " ".join(raw.split()) or not is_licence_notice(raw):
        raise ValueError(f"notice {index}: expected a copyright notice with collapsed whitespace")
    return raw


def validate_licence_notices(entries: object) -> frozenset[str]:
    """Validate loaded or staged notice lines before comparison or publication."""
    if not isinstance(entries, list):
        raise TypeError("notices: expected a list")
    if len(entries) > MAX_LICENCE_NOTICES:
        raise ValueError(f"notices: expected at most {MAX_LICENCE_NOTICES} entries, found {len(entries)}")
    notices = set()
    previous = ""
    for index, raw in enumerate(entries, 1):
        line = _licence_notice_entry(raw, index)
        if line in notices:
            raise ValueError(f"notice {index}: duplicate notice {line!r}")
        if line < previous:
            raise ValueError(f"notice {index}: notices must be sorted")
        notices.add(line)
        previous = line
    return frozenset(notices)


def read_licence_notices(root: Path) -> frozenset[str]:
    """Read the optional reviewed notice register with the strict YAML loader."""
    try:
        text = read_utf8(root / LICENCE_NOTICES_FILE, MAX_LICENCE_BYTES, refuse_links=True)
        data = _load_yaml(text, _UniqueKeyLoader, error_prefix="invalid YAML: ")
        if not isinstance(data, dict) or data.keys() != {"notices"}:
            raise ValueError("expected a mapping with exactly the key notices")
        return validate_licence_notices(data["notices"])
    except FileNotFoundError:
        return frozenset()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{LICENCE_NOTICES_FILE}: {exc}") from exc


def licence_notices_yaml(notices: frozenset[str]) -> str:
    """Render the sorted register, keeping every notice on a single line."""
    return yaml.safe_dump({"notices": sorted(notices)}, allow_unicode=True, width=MAX_LICENCE_BYTES)


def read_licence_scan(root: Path, *, today: date | None = None) -> LicenceScan:
    """Read reviewed licences using a single check date for entry and expiry validation."""
    check_date = datetime.now(UTC).date() if today is None else today
    try:
        entries = _read_licence_exceptions(root, check_date)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{LICENCE_EXCEPTIONS_FILE}: {exc}") from exc
    return LicenceScan(entries, check_date, read_licence_notices(root))


def _load_front_matter(text: str, loader: type[yaml.SafeLoader]) -> dict:
    """Parse the YAML mapping without converting value types."""
    text = _normalize_line_endings(text)
    match = FRONT.match(text)
    if not match:
        if text.startswith("---\n"):
            raise ValueError("front matter is missing its closing delimiter")
        return {}
    data = _load_yaml(match.group(1), loader)
    if not isinstance(data, dict):
        raise ValueError("expected a mapping")
    return data


def front_matter(
    text: str, *, exact_license: bool = False, require_string_licence: bool = False,
) -> dict[str, str]:
    """Project 100 YAML fields and retain both licence keys in strict mode."""
    strict = exact_license or require_string_licence
    loader = _UniqueKeyLoader if strict else yaml.SafeLoader
    data = _load_front_matter(text, loader)
    if require_string_licence:
        for key in ("license", "licence"):
            if key in data and not isinstance(data[key], str):
                raise TypeError(f"{key} must be a string")
    meta: dict[str, str] = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= 100:
            break
        if isinstance(v, str):
            meta[str(k)] = v.strip()
        elif not isinstance(v, (list, dict, type(None))):
            meta[str(k)] = str(v)
    if strict:
        for key in ("license", "licence"):
            value = data.get(key)
            if isinstance(value, str):
                meta.setdefault(key, value.strip())
    if exact_license and "license" in data and not isinstance(data["license"], str):
        meta["license"] = f"<{type(data['license']).__name__}>"
    return meta


def front_matter_or_empty(text: str) -> dict[str, str]:
    """front_matter for readers that skip an unparsable skill: invalid YAML reads as no fields."""
    try:
        return front_matter(text)
    except ValueError:
        return {}


def _workbench_problems(skill: Path) -> list[str]:
    entries: list[Path] = []
    for i, entry in enumerate(skill.rglob("*")):
        if i >= MAX_SKILL_ENTRIES:
            return [f"{skill.name}: more than {MAX_SKILL_ENTRIES} entries"]
        entries.append(entry)
    out: list[str] = []
    for entry in sorted(entries):
        if entry.name.startswith(".") or (entry.is_file() and entry.name.lower() in AGENT_CONTROL_FILES):
            rel = entry.relative_to(skill).as_posix()
            out.append(f"{skill.name}/{rel}: agent workbench or agent-control content is not allowed")
    return out


def _skill_md_link_problems(skill: Path, text: str) -> list[str]:
    out: list[str] = []
    skill_root = skill.resolve()
    tokens = COMMONMARK.parse(text)
    skipped: set[int] = set()
    for token in tokens:
        if token.type in ("fence", "code_block") and token.map:
            skipped.update(range(token.map[0], token.map[1]))

    for lineno, line in enumerate(text.splitlines()[:MAX_SKILL_ENTRIES * 10], 1):
        if (lineno - 1) in skipped:
            continue
        stripped = INLINE_CODE.sub("", line)
        for match in MD_LINK.finditer(stripped):
            target = match.group(1).strip("<>")
            path_part = target.split("#", 1)[0].split("?", 1)[0]
            if not path_part or ":" in path_part or path_part.startswith(("/", "../")):
                continue
            if not path_part.lower().endswith(CHECKED_LINK_SUFFIXES):
                continue
            resolved = (skill / path_part).resolve()
            if not resolved.is_file() or not resolved.is_relative_to(skill_root):
                out.append(f"{skill.name}/SKILL.md:{lineno}: links to missing file {path_part}")
    return out


def _front_licence_problem(meta: dict, spdx: str, path: Path | None) -> str | None:
    """Require each front matter licence to be an agreeing ID or a complete pointer."""
    for key in ("license", "licence"):
        if key not in meta:
            continue
        raw = meta[key]
        label = f"front matter {key}"
        if not isinstance(raw, str):
            return f"{label} has type {type(raw).__name__}; expected a string"
        if not raw.strip():
            return f"{label} is an empty string"
        if name := licence_pointer_name(raw):
            if path is None:
                return f"{label} pointer names {name}, but no licence file was found"
            if name != path.name:
                return f"{label} pointer names {name}, discovered {path.name}"
            continue
        front_id = candidate_licence_id(raw)
        if front_id is None:
            return f"{label} {raw} not recognised"
        if front_id != spdx:
            return f"{label} {raw} disagrees with ORIGIN.md licence {spdx}"
    return None


def _licence_sha256(path: Path) -> str:
    """Hash the exact bytes of a bounded regular licence file."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("LICENSE is not a regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_LICENCE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read LICENSE ({exc.strerror or exc})") from exc
    if len(data) > MAX_LICENCE_BYTES:
        raise ValueError(f"LICENSE exceeds {MAX_LICENCE_BYTES // 1024} KiB")
    return sha256(data).hexdigest()


def _pinned_licence_problem(path: Path, spdx: str, entry: _LicenceException,
                            today: date, difference: str) -> str | None:
    """Refuse a reviewed variant when its SPDX ID, expiry or byte hash fails."""
    if entry.spdx != spdx:
        return f"{entry.path}: SPDX mismatch (recorded {entry.spdx}, declared {spdx})"
    if today > entry.expires:
        return f"{entry.path}: exception expired on {entry.expires}"
    try:
        found = _licence_sha256(path)
    except ValueError as exc:
        return f"{entry.path}: {exc}"
    if found != entry.sha256:
        return f"{entry.path}: hash mismatch (recorded {entry.sha256}, found {found}). {difference}"
    return None


def _licence_text_problem(path: Path, template_dir: Path, spdx: str,
                          scan: LicenceScan | None, notices: frozenset[str]) -> str | None:
    """Consult exception pins only for a compared body or a redundant pin."""
    difference = ""
    try:
        verify_licence(path, template_dir, spdx, reviewed=notices)
    except LicenceBodyDifference as exc:
        difference = str(exc)
    except (ValueError, FileNotFoundError) as exc:
        return f"{path.parent.name}: {exc}"
    problem = f"{path.parent.name}: {difference}" if difference else None
    if scan is None:
        return problem
    entry = scan.exceptions.get(f"skills/{path.parent.name}/LICENSE")
    if entry is None:
        return problem
    if path.name != "LICENSE":
        return f"{entry.path}: no applicable skill licence; remove the exception"
    if not difference:
        return f"{entry.path}: LICENSE matches {spdx}; remove the exception"
    if problem := _pinned_licence_problem(path, spdx, entry, scan.today, difference):
        return problem
    scan.pinned += 1
    return None


def _licence_problem(skill: Path, declared: str, meta: dict, template_dir: Path,
                     scan: LicenceScan | None) -> str | None:
    """Report the first licence problem or record the verified SPDX ID."""
    if scan is not None:
        scan.checked.add(f"skills/{skill.name}/LICENSE")
    spdx = candidate_licence_id(declared)
    if spdx is None:
        return f"{skill.name}/ORIGIN.md: licence {declared} not recognised"
    path = find_folder_licence_file(skill)
    if front_problem := _front_licence_problem(meta, spdx, path):
        return f"{skill.name}/SKILL.md: {front_problem}"
    try:
        notices = scan.notices if scan is not None else read_licence_notices(skill.parent.parent)
    except ValueError as exc:
        return str(exc)
    if problem := _licence_text_problem(path or (skill / "LICENSE"), template_dir, spdx, scan, notices):
        return problem
    if scan is not None:
        scan.verified[spdx] += 1
    return None


def _skill_content_problems(skill: Path, text: str, meta: dict) -> list[str]:
    """Check the front matter, Markdown, links and folder contents in reporting order."""
    out = []
    if str(meta.get("name")).strip() != skill.name:
        out.append(f"{skill.name}/SKILL.md: front matter name must be {skill.name!r}")
    desc = meta.get("description")
    if isinstance(desc, (list, dict, type(None))) or not str(desc).strip():
        out.append(f"{skill.name}/SKILL.md: front matter needs a description")
    for n, line in enumerate(text.splitlines(), 1):
        if FETCH.search(line):
            out.append(f"{skill.name}/SKILL.md:{n}: installs or fetches code at run time")
    out.extend(_skill_md_link_problems(skill, text))
    out.extend(_workbench_problems(skill))
    return out


def _origin_problems(skill: Path, meta: dict, template_dir: Path,
                     licence_scan: LicenceScan | None) -> list[str]:
    """Validate the provenance record and its licence for an adapted skill."""
    try:
        rec = fields(read_utf8(skill / "ORIGIN.md", MAX_ORIGIN_BYTES))
    except (ValueError, FileNotFoundError) as exc:
        return [f"{skill.name}: {exc}"]
    if rec.get("upstream") == "own":
        return []
    out = [f"{skill.name}/ORIGIN.md: needs '{key}:'"
           for key in ("upstream", "path", "commit", "licence", "changes") if not rec.get(key)]
    if rec.get("commit") and not COMMIT.match(rec["commit"]):
        out.append(f"{skill.name}/ORIGIN.md: commit must be the full 40-hex SHA")
    if (declared := rec.get("licence")) and (problem := _licence_problem(skill, declared, meta, template_dir, licence_scan)):
        out.append(problem)
    return out


def skill_problems(
    skill: Path, *, template_dir: Path | None = None, licence_scan: LicenceScan | None = None,
) -> list[str]:
    """Problems of one skill directory, each naming the file and the rule."""
    try:
        text = read_utf8(skill / "SKILL.md", MAX_SKILL_MD_BYTES)
    except (ValueError, FileNotFoundError) as exc:
        return [f"{skill.name}: {exc}"]
    try:
        meta = _load_front_matter(text, _UniqueKeyLoader)
    except ValueError as exc:
        return [f"{skill.name}/SKILL.md: front matter is not valid YAML: {' '.join(str(exc).split())}"]
    if template_dir is None:
        template_dir = skill.parent.parent / "LICENSES"
    return _skill_content_problems(skill, text, meta) + _origin_problems(skill, meta, template_dir, licence_scan)


def check_install_set(root):
    """Validate install-set.txt if present using the shared reader."""
    path = root / "install-set.txt"
    if not path.is_file():
        return []
    try:
        read_install_set(root)
        return []
    except (ValueError, SystemExit) as exc:
        return [f"install-set.txt: {exc}"]


INDEX_HEADER = "skill\tsource\tfunctions\tdomains\toverlap_group"


def index_problems(root, skills=None):
    """Problems of skills/INDEX.tsv: exact header, every skills/ directory listed exactly once, nothing else."""
    index_file = root / "skills" / "INDEX.tsv"
    if not index_file.is_file():
        return ["skills/INDEX.tsv: file missing"]
    text = index_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != INDEX_HEADER:
        return [f"skills/INDEX.tsv: header must be exact {INDEX_HEADER!r}"]
    if skills is None:
        skills = sorted(p for p in (root / "skills").iterdir() if p.is_dir())[:MAX_SKILLS] if (root / "skills").is_dir() else []
    actual_skills = {s.name for s in skills}
    seen = {}
    problems = []
    for line_idx, line in enumerate(lines[1:], 2):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            problems.append(f"skills/INDEX.tsv:{line_idx}: row must have 5 columns")
            continue
        skill_name = parts[0]
        if not skill_name:
            problems.append(f"skills/INDEX.tsv:{line_idx}: empty skill name")
            continue
        if skill_name in seen:
            problems.append(f"skills/INDEX.tsv: duplicate row for {skill_name}")
        seen[skill_name] = seen.get(skill_name, 0) + 1
    for name in sorted(actual_skills - set(seen.keys())):
        problems.append(f"skills/INDEX.tsv: missing row for {name}")
    for name in sorted(set(seen.keys()) - actual_skills):
        problems.append(f"skills/INDEX.tsv: extra row for {name}")
    return problems


def _load_scan_registers(root: Path, scan: LicenceScan, problems: list[str]) -> bool:
    """Load both registers and decide whether the skill scan can continue."""
    try:
        scan.notices = read_licence_notices(root)
    except ValueError as exc:
        problems.append(" ".join(str(exc).split()))
        return False
    try:
        scan.exceptions = _read_licence_exceptions(root, scan.today)
    except (TypeError, ValueError) as exc:
        problems.append(f"{LICENCE_EXCEPTIONS_FILE}: {' '.join(str(exc).split())}")
        return isinstance(exc, _YamlReadError)
    return True


def check(root: Path, *, today: date | None = None) -> int:
    """Check the skill tree and report verified licence counts."""
    skills = []
    scan = LicenceScan({}, datetime.now(UTC).date() if today is None else today)
    problems = []
    if _load_scan_registers(root, scan, problems):
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            skills = sorted(p for p in skills_dir.iterdir() if p.is_dir())[:MAX_SKILLS]
        problems.extend(p for s in skills for p in skill_problems(s, licence_scan=scan))
        problems.extend(f"{path}: no applicable skill licence; remove the exception" for path in sorted(scan.exceptions.keys() - scan.checked))
        problems.extend(check_install_set(root))
        problems.extend(index_problems(root, skills))
    for p in problems:
        print(f"[FAIL] {p}")
    if scan.verified:
        print("verified licences: " + ", ".join(f"{spdx}={count}" for spdx, count in sorted(scan.verified.items())))
    print(f"pinned licence texts: {scan.pinned}")
    print(f"skills: {len(skills)} checked, {len(problems)} problems")
    return 1 if problems else 0


def write_index(root, rows):
    """Write the index TSV for fixture skills."""
    d = root / "skills"
    d.mkdir(parents=True, exist_ok=True)
    lines = [INDEX_HEADER]
    for r in sorted(rows, key=lambda x: x["skill"]):
        lines.append(f"{r['skill']}\t{r.get('source', '')}\t{r.get('functions', '')}\t{r.get('domains', '')}\t{r.get('overlap_group', '')}")
    (d / "INDEX.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_licence(spdx: str) -> str:
    """Read the canonical licence text for a fixture."""
    path = Path(__file__).resolve().parent.parent / "LICENSES" / f"{spdx}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"missing selftest licence text: {path}") from exc


def write(root, name, skill_md, origin=None, licence=True, *, licence_text=None):
    """Write a fixture skill and seed its canonical template if available."""
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if origin is not None:
        (d / "ORIGIN.md").write_text(origin, encoding="utf-8")
    if licence:
        text = _fixture_licence("MIT") if licence_text is None else licence_text
        (d / "LICENSE").write_text(text, encoding="utf-8")
        spdx = candidate_licence_id(fields(origin or "").get("licence", "MIT"))
        if spdx:
            path = root / "LICENSES" / f"{spdx}.txt"
            try:
                template = _fixture_licence(spdx)
            except FileNotFoundError:
                return
            path.parent.mkdir(exist_ok=True)
            path.write_text(template, encoding="utf-8")


def selftest():
    """Exercise the skill checker against temporary fixtures."""
    good_md = "---\nname: {n}\ndescription: Use when checking a claim.\n---\n\n# x\n"
    good_origin = ("upstream: https://github.com/o/r\npath: skills/x\ncommit: " + "a" * 40 +
                   "\nlicence: MIT\nchanges: register rewritten\n")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(root, "good", good_md.format(n="good"), good_origin)
        write(root, "own", good_md.format(n="own"), "upstream: own\n", licence=False)
        write_index(root, [
            {"skill": "good", "source": "o/r"},
            {"skill": "own", "source": "peritia"},
        ])
        assert check(root) == 0, "a complete adapted skill and an own skill pass"

        write_index(root, [{"skill": "good", "source": "o/r"}])
        probs = index_problems(root)
        assert any("missing row for own" in p for p in probs), probs

        write_index(root, [
            {"skill": "good", "source": "o/r"},
            {"skill": "own", "source": "peritia"},
            {"skill": "extra", "source": "o/r"},
        ])
        probs = index_problems(root)
        assert any("extra row for extra" in p for p in probs), probs

        (root / "skills" / "INDEX.tsv").write_text("bad\theader\n", encoding="utf-8")
        probs = index_problems(root)
        assert any("header must be exact" in p for p in probs), probs

        (root / "skills" / "INDEX.tsv").unlink()
        probs = index_problems(root)
        assert any("file missing" in p for p in probs), probs

        write_index(root, [
            {"skill": "good", "source": "o/r"},
            {"skill": "own", "source": "peritia"},
        ])
        (root / "install-set.txt").write_text("good\n", encoding="utf-8")
        assert check(root) == 0, "a valid install-set.txt passes"
        (root / "install-set.txt").write_text("good\nunknown-skill\n", encoding="utf-8")
        assert check(root) == 1, "an unknown skill in install-set.txt is refused"
        (root / "install-set.txt").unlink()
        _selftest_cases(Path(tmp), good_md, good_origin)
        _selftest_licences(root, good_md, good_origin)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(_LicenceRegressionTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def _selftest_cases(tmp, good_md, good_origin):
    folded_md = "---\nname: folded\ndescription: >\n  Use when checking\n  a claim.\n---\n\n# folded\n"
    planted_folded = tmp / "folded"
    write(planted_folded, "folded", folded_md, good_origin)
    parsed = front_matter(folded_md)
    assert parsed["description"] == "Use when checking a claim.", parsed
    assert skill_problems(planted_folded / "skills" / "folded") == []

    cases = {
        "wrong-name": (good_md.format(n="other"), good_origin, True, "front matter name"),
        "no-origin": (good_md.format(n="no-origin"), None, True, "ORIGIN.md missing"),
        "short-pin": (good_md.format(n="short-pin"), good_origin.replace("a" * 40, "abc1234"), True, "full 40-hex"),
        "no-licence": (good_md.format(n="no-licence"), good_origin, False, "LICENSE missing"),
        "installs": (good_md.format(n="installs") + "\n    uv pip install numpy==2.5.3\n", good_origin, True, "installs or fetches"),
        "empty-desc": ("---\nname: empty-desc\ndescription: >\n---\n\n# x\n", good_origin, True, "needs a description"),
        "bad-yaml": ("---\nname: [\n---\n\n# x\n", good_origin, True, "not valid YAML"),
        "dangling-link": (good_md.format(n="dangling-link") + "\nSee [Guide](reference/missing.md).\n", good_origin, True, "links to missing file reference/missing.md"),
        "tilde-fence-link": (
            good_md.format(n="tilde-fence-link")
            + "\n~~~\n```\n[Inside](reference/inside-tilde.md)\n~~~\nSee [After](reference/after-tilde.md).\n",
            good_origin, True, "links to missing file reference/after-tilde.md",
        ),
        "quad-fence-link": (
            good_md.format(n="quad-fence-link")
            + "\n````markdown\n```python\n[Inside](reference/inside-quad.md)\n````\nSee [After](reference/after-quad.md).\n",
            good_origin, True, "links to missing file reference/after-quad.md",
        ),
        "indented-backtick-link": (
            good_md.format(n="indented-backtick-link")
            + "\n    ```\n[After](reference/after-indented.md)\n",
            good_origin, True, "links to missing file reference/after-indented.md",
        ),
    }
    for name, (md, origin, licence, want) in cases.items():
        planted = tmp / name
        write(planted, name, md, origin, licence)
        problems = skill_problems(planted / "skills" / name)
        if not (any(want in p for p in problems) and not any("inside-" in p for p in problems)):
            raise AssertionError((name, problems))
    _selftest_workbench(tmp, good_md, good_origin)
    print(f"selftest ok: 2 clean skills pass, {len(cases) + 1} planted defects refused, install-set validated")


def _selftest_workbench(tmp, good_md, good_origin):
    hive_root = tmp / "hive-protocol"
    write(hive_root, "hive-protocol", good_md.format(n="hive-protocol"), good_origin)
    hive_skill = hive_root / "skills" / "hive-protocol"
    (hive_skill / ".hive").mkdir()
    (hive_skill / ".hive" / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    problems = skill_problems(hive_skill)
    if not any(".hive/PROTOCOL.md" in p for p in problems):
        raise AssertionError(f"planted .hive/PROTOCOL.md not refused: {problems}")
    (hive_skill / ".hive" / "PROTOCOL.md").unlink()
    (hive_skill / ".hive").rmdir()
    left = skill_problems(hive_skill)
    if left:
        raise AssertionError(f"skill without .hive still refused: {left}")

    prose_root = tmp / "hive-prose"
    prose_md = good_md.format(n="hive-prose") + "\nNever copy a .hive workbench folder.\n"
    write(prose_root, "hive-prose", prose_md, good_origin)
    (prose_root / "skills" / "hive-prose" / "notes.md").write_text("Mentions .hive in prose.\n", encoding="utf-8")
    prose = skill_problems(prose_root / "skills" / "hive-prose")
    if prose:
        raise AssertionError(f"prose naming .hive refused: {prose}")


def _selftest_licences(tmp, good_md, good_origin):
    """Exercise licence text recognition and agreement between all declarations."""
    mit = _fixture_licence("MIT")
    apache = _fixture_licence("Apache-2.0")
    mit_zero = _fixture_licence("MIT-0")
    header, sep, body = mit.partition("Permission")
    wrapped_mit = header + "\n".join((sep + body).split()) + "\n"
    mit_body = mit.replace("Copyright (c) <year> <copyright holders>", "")
    mit_zero_body = mit_zero.replace("Copyright <YEAR> <COPYRIGHT HOLDER>", "")
    apache_body = apache.replace("Copyright [yyyy] [name of copyright owner]", "").replace("     (c) ", "     c) ", 1)
    cases = (
        ("carrier-licence", mit_body, "Apache-2.0", "Apache-2.0",
         "LICENSE differs from Apache-2.0 at word offset"),
        ("apache-as-mit", apache_body, "MIT", None, "LICENSE differs from MIT at word offset"),
        ("mit-zero-as-mit", mit_zero_body, "MIT", None, "LICENSE differs from MIT at word offset"),
        ("mit-as-mit-zero", mit_body, "MIT-0", None, "LICENSE differs from MIT-0 at word offset"),
        ("unknown-text", "All rights reserved.\n", "MIT", None, "LICENSE differs from MIT at word offset"),
        ("unknown-origin", mit, "GPL-3.0", None, "ORIGIN.md: licence GPL-3.0 not recognised"),
        ("front-mismatch", mit, "MIT", "Apache-2.0",
         "front matter license Apache-2.0 disagrees with ORIGIN.md licence MIT"),
        ("unknown-front", mit, "MIT", "BSD-ish", "front matter license BSD-ish not recognised"),
        ("null-front", mit, "MIT", "null", "front matter license has type NoneType"),
        ("padded-front", mit, "MIT", '" MIT "', None),
        ("mit-alias", mit, "MIT", '"MIT license"', None),
        ("license-pointer", apache, "Apache-2.0", "Complete terms in LICENSE", None),
        ("rewrapped-mit", wrapped_mit, "MIT", "MIT", None),
        ("absent-front", mit_zero, "MIT-0", None, None),
        ("cc-zero", _fixture_licence("CC0-1.0"), "CC0-1.0", '"CC0-1.0"', None),
    )
    refused = 0
    for name, text, declared, front, expected in cases:
        md = good_md.format(n=name)
        if front is not None:
            md = md.replace("\n---\n", f"\nlicense: {front}\n---\n", 1)
        origin = good_origin.replace("licence: MIT\n", f"licence: {declared}\n")
        planted = tmp / name
        write(planted, name, md, origin, licence_text=text)
        problems = skill_problems(planted / "skills" / name)
        if expected is None:
            if problems:
                raise AssertionError((name, problems))
        else:
            if len(problems) != 1 or expected not in problems[0]:
                raise AssertionError((name, expected, problems))
            refused += 1
    print(f"licence selftest ok: {refused} planted defects refused, {len(cases) - refused} valid cases passed")


class _LicenceRegressionTests(unittest.TestCase):
    """Plant licence files and declarations for the production skill checker."""

    def setUp(self):
        """Create an isolated skill tree and canonical template directory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.templates = self.root / "LICENSES"
        self.templates.mkdir()
        self.mit = _fixture_licence("MIT")
        self.apache = _fixture_licence("Apache-2.0")
        self.mit_zero = _fixture_licence("MIT-0")
        for spdx, body in (("MIT", self.mit), ("Apache-2.0", self.apache), ("MIT-0", self.mit_zero)):
            (self.templates / f"{spdx}.txt").write_text(body, encoding="utf-8")

    def _plant(self, body, declared="MIT", front="", *, name="planted"):
        """Write a skill and optionally write exact bytes into its LICENSE."""
        md = f'---\nname: "{name}"\ndescription: Use when testing licences.\n{front}---\n'
        origin = f"upstream: https://github.com/o/r\npath: skills/x\ncommit: {'a' * 40}\n"
        origin += f"licence: {declared}\nchanges: register rewritten\n"
        write(self.root, name, md, origin, licence=False)
        skill = self.root / "skills" / name
        if body is not None:
            (skill / "LICENSE").write_bytes(body if isinstance(body, bytes) else body.encode())
        return skill

    def _refused(self, skill, message):
        """Require one diagnostic naming the skill and the reason."""
        problems = skill_problems(skill)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(skill.name, problems[0])
        self.assertIn(message, problems[0])

    def test_false_accepts(self):
        """Reject partial matches, mixed bodies and bytes outside UTF-8."""
        notice_start = self.mit.index("The above copyright notice")
        late_notice = self.mit[:notice_start] + " " * 4096 + self.mit[notice_start:]
        mit_body = self.mit.replace("Copyright (c) <year> <copyright holders>", "")
        cases = (
            ("mit-quotes-apache", mit_body + '\n"Apache License, Version 2.0"\n', "Apache-2.0", "word offset"),
            ("late-mit-notice", late_notice, "MIT-0", "unreviewed copyright notice"),
            ("apache-heading", "Apache License, Version 2.0\n", "Apache-2.0", "word offset"),
            ("bare-mit-grant", "Permission is hereby granted, free of charge\n", "MIT-0", "word offset"),
            ("invalid-utf8", b"\xff\n" + self.mit.encode(), "MIT", "invalid UTF-8"),
            ("apache-plus-notice", self.apache + "\n" + self.mit[notice_start:], "Apache-2.0", "word offset"),
            ("mit-extra-word", self.mit + "\nExtra\n", "MIT", "word offset"),
            ("mit-as-mit-zero", self.mit, "MIT-0", "unreviewed copyright notice"),
            ("mit-zero-as-mit", self.mit_zero, "MIT", "unreviewed copyright notice"),
        )
        for name, body, declared, message in cases:
            with self.subTest(name=name):
                self._refused(self._plant(body, declared, name=name), message)

    def test_spdx_metadata_must_agree(self):
        """Reject conflicting metadata in each supported comment syntax."""
        # REUSE-IgnoreStart
        for prefix in ("SPDX-License-Identifier:", "# REUSE-License:", "// SPDX-License-Identifier:",
                       "/* SPDX-License-Identifier:", "<!-- REUSE-License:"):
            # REUSE-IgnoreEnd
            suffix = {"/*": " */", "<!--": " -->"}.get(prefix.split()[0], "")
            with self.subTest(prefix=prefix):
                body = f"{prefix} Apache-2.0{suffix}\n{self.mit}"
                self._refused(self._plant(body), "Apache-2.0; declared MIT")
                body = f"{prefix} miT{suffix}\n{self.mit}"
                self.assertEqual(skill_problems(self._plant(body)), [])

    def test_spdx_metadata_normalises_lgpl_only_alias(self):
        """Accept the bare LGPL ID and reject other or unknown identifiers."""
        spdx = "LGPL-3.0-only"
        (self.templates / f"{spdx}.txt").write_text(self.mit, encoding="utf-8")
        identifier = "SPDX-License-Identifier" + ":"
        self.assertEqual(skill_problems(self._plant(f"{identifier} LGPL-3.0\n{self.mit}", spdx)), [])
        for found in ("LGPL-3.0-or-later", "Unknown-Licence"):
            with self.subTest(found=found):
                self._refused(self._plant(f"{identifier} {found}\n{self.mit}", spdx), f"{found}; declared {spdx}")

    def _write_licence_notices(self, notices):
        """Write a fixture notice register."""
        register = self.root / "skills/licence-notices.yaml"
        register.parent.mkdir(exist_ok=True)
        register.write_text(yaml.safe_dump({"notices": notices}, allow_unicode=True), encoding="utf-8")

    def test_copyright_notice_cannot_hide_terms(self):
        """Refuse unreviewed notice-shaped text in any position."""
        for restriction in ("NO COMMERCIAL USE", "nocommercial-use", "PROPRIETARY", "noncommercial"):
            line = f"Copyright 2026 {restriction}"
            for body in (f"{line}\n{self.apache}", f"{self.apache}\n{line}\n"):
                with self.subTest(line=line, prepended=body.startswith(line)):
                    self._refused(self._plant(body, "Apache-2.0"), f"unreviewed copyright notice {line!r}")
        line = "Copyright 2024 Acme. No Commercial Use."
        self._refused(self._plant(f"{line}\n{self.mit}"), f"unreviewed copyright notice {line!r}")
        self._write_licence_exceptions([self._plant_licence_exception(f"{line}\n{self.mit}")])
        self._check_licence_exceptions("skills/licence-notices.yaml", line)

    def test_non_notice_prefixes_remain_in_body(self):
        """Compare text whose prefix differs in case or spacing."""
        for line in (
            "copyright 2024 Acme", "Copyright(c) 2024 Acme", "# Copyright 2024 Acme",
            "(c) Acme", "(C) Acme",
        ):
            with self.subTest(line=line):
                self._refused(self._plant(f"{line}\n{self.mit}"), "LICENSE differs from MIT at word offset")

    def test_lettered_clause_lines_are_required(self):
        """Compare each clause's first physical line in non-Apache templates."""
        for label in ("(c)", "(C)"):
            with self.subTest(label=label):
                spdx = "MPL-2.0"
                clause = f"{label} under Patents to make, use, sell, offer for sale,"
                rest = "import and otherwise transfer the Contribution.\n"
                body = f"Mozilla Public License\nPatent grant.\n{clause}\n{rest}"
                (self.templates / f"{spdx}.txt").write_text(body, encoding="utf-8")
                self.assertEqual(skill_problems(self._plant(body, spdx)), [])
                self._refused(self._plant(body.replace(f"{clause}\n", ""), spdx),
                              "LICENSE differs from MPL-2.0 at word offset")

    def test_copyright_symbol_without_space_requires_review(self):
        """Require exact review of a compact copyright notice."""
        line = "©2026 Owner"
        skill = self._plant(f"{line}\n{self.mit}")
        self._refused(skill, f"unreviewed copyright notice {line!r}")
        self._write_licence_notices([line])
        self.assertEqual(read_licence_notices(self.root), {line})
        self.assertEqual(skill_problems(skill), [])

    def test_reviewed_notices_verify_exactly(self):
        """Accept every seeded line and each supported prefix after whitespace collapse."""
        repo = Path(__file__).resolve().parent.parent
        seeded = yaml.safe_load((repo / "skills/licence-notices.yaml").read_text(encoding="utf-8"))["notices"]
        notices = sorted({*seeded, "© 2026 Owner"})
        self._write_licence_notices(notices)
        for line in notices:
            with self.subTest(line=line):
                spaced = "  " + line.replace(" ", "  \t")
                self.assertEqual(skill_problems(self._plant(f"{spaced}\n{self.mit}")), [])
        line = seeded[0] + "x"
        self._refused(self._plant(f"{line}\n{self.mit}"), f"unreviewed copyright notice {line!r}")

    def test_template_notices_need_no_register(self):
        """Accept canonical placeholders from the selected SPDX template."""
        for spdx, body in (("MIT", self.mit), ("MIT-0", self.mit_zero), ("Apache-2.0", self.apache)):
            with self.subTest(spdx=spdx):
                self.assertEqual(skill_problems(self._plant(body, spdx)), [])

    def test_wrapped_cc0_copyright_terms_stay_in_licence_body(self):
        """Accept wrapped CC0 terms and still require review of real notices."""
        spdx = "CC0-1.0"
        template = _fixture_licence(spdx)
        (self.templates / f"{spdx}.txt").write_text(template, encoding="utf-8")
        body = template.replace("exclusive Copyright and Related Rights", "exclusive\nCopyright and Related Rights")
        self.assertIn("\nCopyright and Related Rights", body)
        self.assertEqual(skill_problems(self._plant(body, spdx)), [])
        for notice in ("Copyright (c) 2025 Example Org", "Copyright The Example Authors."):
            with self.subTest(notice=notice):
                skill = self._plant(f"{notice}\n{body}", spdx)
                self._refused(skill, f"unreviewed copyright notice {notice!r}")
                self._write_licence_notices([notice])
                self.assertEqual(skill_problems(skill), [])

    def test_licence_notice_register_limits(self):
        """Enforce count, length, sorted order and uniqueness."""
        notices = [f"Copyright {i:03}" for i in range(512)]
        self._write_licence_notices(notices)
        self.assertEqual(read_licence_notices(self.root), frozenset(notices))
        longest = "Copyright " + "X" * 190
        self._write_licence_notices([longest])
        self.assertEqual(read_licence_notices(self.root), {longest})
        for entries, message in (
            ([*notices, "Copyright 512"], "at most 512 entries"),
            (["Copyright Acme", "Copyright Acme"], "duplicate notice 'Copyright Acme'"),
            (["Copyright Z", "Copyright A"], "must be sorted"),
            ([longest + "X"], "at most 200 characters"),
            (["copyright Acme"], "expected a copyright notice"),
            (["(c) Acme"], "notice 1: expected a copyright notice"),
            (["(C) Acme"], "notice 1: expected a copyright notice"),
        ):
            with self.subTest(message=message):
                self._write_licence_notices(entries)
                with self.assertRaisesRegex(ValueError, f"skills/licence-notices.yaml: .*{message}"):
                    read_licence_notices(self.root)

    def test_licence_notice_type_errors_are_reported(self):
        """Keep type errors distinct while retaining the register diagnostic."""
        for entries, message in (
            ([42], "notice 1: expected a string"), (42, "notices: expected a list"),
        ):
            with self.subTest(message=message):
                self._write_licence_notices(entries)
                with self.assertRaisesRegex(
                    ValueError, f"skills/licence-notices.yaml: {message}",
                ):
                    read_licence_notices(self.root)
                with self.assertRaisesRegex(TypeError, message):
                    validate_licence_notices(entries)

    def test_wrapped_apache_clause_c_remains_in_body(self):
        """Accept reflow of section 4(c) and require its substantive terms."""
        phrase = "any Derivative Works that You distribute"
        wrapped = self.apache.replace(phrase, "any Derivative Works\n          that You distribute", 1)
        self.assertNotEqual(wrapped, self.apache)
        self.assertEqual(skill_problems(self._plant(wrapped, "Apache-2.0")), [])
        three_lines = wrapped.replace(
            "that You distribute, all copyright",
            "that You\n          distribute, all copyright", 1,
        )
        self.assertNotEqual(three_lines, wrapped)
        self.assertEqual(skill_problems(self._plant(three_lines, "Apache-2.0")), [])
        changed = wrapped.replace("that You distribute, all copyright", "that You distribute, some copyright", 1)
        self._refused(self._plant(changed, "Apache-2.0"), "LICENSE differs from Apache-2.0")
        omitted = "\n".join(line for line in self.apache.splitlines() if not line.lstrip().startswith("(c) "))
        self._refused(self._plant(omitted, "Apache-2.0"), "LICENSE differs from Apache-2.0")

    def test_numbered_clauses_must_agree(self):
        """Refuse changes to section and clause identifiers."""
        for original, changed in (("3. Grant of Patent License.", "7. Grant of Patent License."),
                                  ("(a) You must give", "(z) You must give")):
            with self.subTest(label=original):
                modified = self.apache.replace(original, changed, 1)
                self.assertNotEqual(modified, self.apache)
                self._refused(self._plant(modified, "Apache-2.0"), "differs from Apache-2.0")

    def test_unknown_spdx_ids(self):
        """Refuse unsupported identifiers even when a local template exists."""
        for spdx in ("LGPL-NONSENSE", "MIT-only", "Apache-2.0-or-later"):
            with self.subTest(spdx=spdx):
                (self.templates / f"{spdx}.txt").write_text(self.mit, encoding="utf-8")
                self.assertIsNone(candidate_licence_id(spdx))
                self._refused(self._plant(self.mit, spdx), f"licence {spdx} not recognised")

    def test_pointer_requires_discovered_file(self):
        """Require the declared pointer and discovered licence file to agree."""
        skill = self._plant(self.mit, front="license: See LICENSE.txt\n")
        self._refused(skill, "pointer names LICENSE.txt, discovered LICENSE")
        (skill / "LICENSE.txt").write_text(self.mit, encoding="utf-8")
        self._refused(skill, "pointer names LICENSE.txt, discovered LICENSE")
        (skill / "LICENSE").unlink()
        (skill / "LICENSE.txt").unlink()
        self._refused(skill, "pointer names LICENSE.txt, but no licence file was found")
        skill = self._plant(None, front="license: See LICENSE.rst\n")
        (skill / "LICENSE.rst").write_text(self.mit, encoding="utf-8")
        self.assertEqual(skill_problems(skill), [])
        (skill / "LICENSE.rst").write_text("Altered text.\n", encoding="utf-8")
        self._refused(skill, "differs from MIT")

    def test_candidate_families(self):
        """Admit additional candidate families only when their template exists."""
        ids = ("ISC", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Zlib", "Unlicense",
               "MPL-2.0", "EUPL-1.2", "LGPL-2.1-only", "LGPL-3.0-only", "LGPL-2.0-or-later")
        for spdx in ids:
            body = f"Fixture terms for {spdx}. The complete grant appears here.\n"
            skill = self._plant(body, spdx, name=spdx.lower())
            with self.subTest(spdx=spdx, template="missing"):
                self._refused(skill, f"reuse download {spdx}")
            with self.subTest(spdx=spdx, template="present"):
                (self.templates / f"{spdx}.txt").write_text(body, encoding="utf-8")
                self.assertEqual(skill_problems(skill), [])

    def test_lgpl_spelling_agreement(self):
        """Compare deprecated and current -only IDs under the same template name."""
        for version in ("2.0", "2.1", "3.0"):
            base = f"LGPL-{version}"
            canonical = f"{base}-only"
            other = f"{base}-or-later"
            (self.templates / f"{canonical}.txt").write_text(self.mit, encoding="utf-8")
            for declared, front in ((base, canonical), (canonical, base)):
                with self.subTest(declared=declared):
                    self.assertEqual(skill_problems(self._plant(self.mit, declared, f"license: {front}\n")), [])
            skill = self._plant(self.mit, canonical, f"license: {other}\n")
            self._refused(skill, f"{other} disagrees with ORIGIN.md licence {canonical}")

    def test_duplicate_origin_key(self):
        """Refuse duplicated provenance, including duplicate identical values."""
        skill = self._plant(self.mit)
        self.assertEqual(skill_problems(skill), [])
        origin = skill / "ORIGIN.md"
        single = origin.read_text(encoding="utf-8")
        for key, value in (("licence", "Apache-2.0"), ("licence", ""), ("licence", "MIT"), ("upstream", "own")):
            with self.subTest(key=key, value=value):
                origin.write_text(f"{key}: {value}\n{single}", encoding="utf-8")
                self._refused(skill, f"ORIGIN.md: duplicate provenance key {key!r}")

    def test_origin_line_endings(self):
        """Read every provenance field in LF, CRLF and lone-CR files."""
        skill = self._plant(self.mit)
        origin = skill / "ORIGIN.md"
        single = origin.read_bytes()
        expected = {
            "upstream": "https://github.com/o/r", "path": "skills/x",
            "commit": "a" * 40,
            "licence": "MIT", "changes": "register rewritten",
        }
        for ending in (b"\n", b"\r\n", b"\r"):
            with self.subTest(ending=ending):
                origin.write_bytes(single.replace(b"\n", ending))
                self.assertEqual(fields(read_utf8(origin, MAX_ORIGIN_BYTES)), expected)
                self.assertEqual(skill_problems(skill), [])
        for value in (b"MIT", b"Apache-2.0"):
            with self.subTest(value=value):
                origin.write_bytes(b"licence: " + value + b"\r\n" + single)
                self._refused(skill, "ORIGIN.md: duplicate provenance key 'licence'")

    def test_front_matter(self):
        """Accept shared ID forms and exact file pointers, reject other values."""
        cases = (
            ("mit-name", "MIT License", "MIT", self.mit, None),
            ("mit-lower", "mit", "MIT", self.mit, None),
            ("mit-origin", "MIT", "mit licence", self.mit, None),
            ("apache-name", "Apache 2.0", "Apache-2.0", self.apache, "not recognised"),
            ("see-licence", "SEE LICENSE IN LICENSE", "MIT", self.mit, None),
            ("see-txt", "SEE LICENSE IN LICENSE.txt", "MIT", self.mit, "discovered LICENSE"),
            ("complete-txt", "Complete terms in LICENSE.txt", "MIT", self.mit, "discovered LICENSE"),
            ("see-arbitrary", "SEE LICENSE ON EXAMPLE.INVALID", "MIT", self.mit, "not recognised"),
            ("list", "[MIT]", "MIT", self.mit, "type list"),
            ("integer", "4", "MIT", self.mit, "type int"),
            ("boolean", "true", "MIT", self.mit, "type bool"),
            ("null-value", "", "MIT", self.mit, "type NoneType"),
            ("empty-string", "''", "MIT", self.mit, "empty string"),
        )
        for name, front, declared, body, message in cases:
            with self.subTest(name=name):
                skill = self._plant(body, declared, f"license: {front}\n", name=name)
                if message is None:
                    self.assertEqual(skill_problems(skill), [])
                else:
                    self._refused(skill, message)

    def test_duplicate_keys(self):
        """Refuse duplicate licence or unrelated mapping keys."""
        for key, front in (
            ("license", "license: Apache-2.0\nlicense: MIT\n"),
            ("name", "name: planted\n"),
            ("nested", "metadata:\n  nested: x\n  nested: y\n"),
        ):
            with self.subTest(key=key):
                self._refused(self._plant(self.mit, front=front), f"duplicate key {key!r}")

    def test_discovery_yaml(self):
        """Preserve discovery's duplicate-key behavior and reject malformed YAML."""
        self.assertEqual(front_matter("---\nname: first\nname: last\n---\n"), {"name": "last"})
        with self.assertRaisesRegex(ValueError, "while parsing a flow node"):
            front_matter("---\nname: [\n---\n")

    def test_yaml_references_and_merge_keys(self):
        """Refuse YAML aliases and merge keys in both checked documents."""
        alias_bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n"
        left = ", ".join(f"k{i}: value" for i in range(600))
        right = ", ".join(f"k{i}: value" for i in range(600, 1200))
        merge = f"metadata: {{<<: [{{{left}}}, {{{right}}}]}}\n"
        register = self.root / LICENCE_EXCEPTIONS_FILE
        register.parent.mkdir()
        for yaml_text, detail in ((alias_bomb, "YAML aliases are not allowed"),
                                  ("metadata: *undefined\n", "YAML aliases are not allowed"),
                                  (merge, "merge keys")):
            with self.subTest(detail=detail):
                self._refused(self._plant(self.mit, front=yaml_text), detail)
                register.write_text(f"exceptions: []\n{yaml_text}", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    _read_licence_exceptions(self.root)

    def test_yaml_depth_cap(self):
        """Compose documents at the cap and refuse deeper ones before composing."""
        cap = getattr(sys.modules[__name__], "MAX_YAML_DEPTH", 32)
        for loader in (yaml.SafeLoader, _UniqueKeyLoader):
            with self.subTest(loader=loader):
                at_cap = "a: " + "[" * (cap - 1) + "]" * (cap - 1)
                self.assertIn("a", _load_yaml(at_cap, loader))
                over_cap = "a: " + "[" * cap + "]" * cap
                with (
                    patch.object(loader, "get_single_node", side_effect=AssertionError("composition started")),
                    self.assertRaisesRegex(ValueError, rf"YAML nesting depth exceeds {cap}"),
                ):
                    _load_yaml(over_cap, loader)

    def test_anchor_only_scalar(self):
        """Accept an anchored scalar that has no alias."""
        front = "---\nlicense: &terms GPL-3.0-only\n---\n"
        self.assertEqual(front_matter(front)["license"], "GPL-3.0-only")
        self.assertEqual(front_matter(front, exact_license=True)["license"], "GPL-3.0-only")

    def test_yaml_event_cap(self):
        """Refuse front matter that exceeds the parser event limit."""
        skill = self._plant(self.mit, front="metadata: [one, two, three, four]\n")
        with patch.object(sys.modules[__name__], "MAX_YAML_EVENTS", 10, create=True):
            self._refused(skill, "document exceeds 10 YAML events")

    def test_yaml_invalid_explicit_scalars(self):
        """Report invalid tagged scalars and continue checking other skills."""
        self._plant(self.mit, name="valid")
        register = self.root / LICENCE_EXCEPTIONS_FILE
        write_index(self.root, [{"skill": "planted"}, {"skill": "valid"}])
        for scalar in ("!!timestamp 2026-13-45", "!!timestamp nonsense", "!!int x"):
            with self.subTest(scalar=scalar):
                self._plant(self.mit, front=f"metadata: {scalar}\n")
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(check(self.root), 1)
                self.assertIn("planted/SKILL.md: front matter is not valid YAML", output.getvalue())
                self.assertIn("skills: 2 checked, 1 problems", output.getvalue())
                self.assertIn("verified licences: MIT=1", output.getvalue())
        self._plant(self.mit)
        register.write_text("exceptions: [!!timestamp 2026-13-45]\n", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(check(self.root), 1)
        self.assertIn("licence-exceptions.yaml: invalid YAML", output.getvalue())
        self.assertIn("skills: 2 checked, 1 problems", output.getvalue())
        self.assertIn("verified licences: MIT=2", output.getvalue())

    def test_unsafe_yaml_tags(self):
        """Refuse Python-specific YAML tags in discovery and validation."""
        document = "---\nname: fixture\nmetadata: !!python/object:types.SimpleNamespace {}\n---\n"
        for exact_license in (False, True):
            with (self.subTest(exact_license=exact_license),
                  self.assertRaisesRegex(ValueError, "could not determine a constructor")):
                front_matter(document, exact_license=exact_license)

        register = self.root / LICENCE_EXCEPTIONS_FILE
        register.parent.mkdir()
        register.write_text("!!python/dict {exceptions: []}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "^invalid YAML: could not determine a constructor"):
            _read_licence_exceptions(self.root)

    def test_file_failures(self):
        """Report missing, directory, symlink and unreadable licence files."""
        skill = self._plant(None, name="missing")
        with self.subTest(case="missing"):
            self._refused(skill, "LICENSE missing")
        skill = self._plant(None, name="directory")
        (skill / "LICENSE").mkdir()
        with self.subTest(case="directory"):
            self._refused(skill, "LICENSE is a directory")
        skill = self._plant(None, name="symlink")
        (skill / "LICENSE").symlink_to(self.templates / "MIT.txt")
        with self.subTest(case="symlink"):
            self._refused(skill, "LICENSE is a symlink")
        skill = self._plant(None, name="dangling")
        (skill / "LICENSE").symlink_to(self.templates / "missing.txt")
        with self.subTest(case="dangling"):
            self._refused(skill, "LICENSE is a symlink")
        skill = self._plant(self.mit, name="permission-error")
        original_open = Path.open

        def refuse_licence(path, *args, **kwargs):
            """Simulate an unreadable file even when the test runs as root."""
            if path == skill / "LICENSE":
                raise PermissionError("permission denied fixture")
            return original_open(path, *args, **kwargs)

        with self.subTest(case="permission-error"), patch.object(Path, "open", refuse_licence):
            self._refused(skill, "cannot read LICENSE")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0, "chmod requires a non-root Unix user")
    def test_chmod_unreadable(self):
        """Report a real unreadable file on systems that enforce Unix mode bits."""
        skill = self._plant(self.mit)
        path = skill / "LICENSE"
        self.addCleanup(path.chmod, 0o600)
        path.chmod(0o000)
        self._refused(skill, "cannot read LICENSE")

    def test_scan_continues(self):
        """Report separate licence failures and still verify a later clean skill."""
        self._plant(b"\xff", name="bad-encoding")
        self._plant(None, name="missing")
        self._plant(self.mit, name="valid")
        names = ("bad-encoding", "missing", "valid")
        write_index(self.root, [{"skill": name} for name in names])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(check(self.root), 1)
        report = output.getvalue()
        self.assertIn("[FAIL] bad-encoding: LICENSE has invalid UTF-8", report)
        self.assertIn("[FAIL] missing: LICENSE missing", report)
        self.assertIn("verified licences: MIT=1", report)
        self.assertIn("skills: 3 checked, 2 problems", report)

    def test_accepted_text_variants(self):
        """Accept presentation changes and omission of the Apache appendix."""
        end_marker = "END OF TERMS AND CONDITIONS"
        apache_end = self.apache.index(end_marker) + len(end_marker)
        formatted_apache = self.apache.replace("http://", "https://").replace(
            "non-exclusive", "non–exclusive").replace("1. Definitions.", "• 1) Definitions.")
        formatted_apache = formatted_apache.replace("(a) You must give", "a. You must give", 1)
        mit_body = self.mit.replace("Copyright (c) <year> <copyright holders>", "")
        cases = (
            ("apache-no-appendix", self.apache[:apache_end], "Apache-2.0"),
            ("apache-formatting", formatted_apache, "Apache-2.0"),
            ("bom-crlf", b"\xef\xbb\xbf" + self.mit.replace("\n", "\r\n").encode(), "MIT"),
            ("long-mit", self.mit.replace("The above copyright notice", " " * 4096 + "The above copyright notice"), "MIT"),
            ("cap-minus-one", self.mit.ljust(256 * 1024 - 1), "MIT"),
            ("normalisation", mit_body.lower().replace('"', "“").replace("license", "licence"), "MIT"),
            ("title-absent", self.mit.removeprefix("MIT License\n"), "MIT"),
            ("title-alias", self.mit.replace("MIT License", "The MIT License", 1), "MIT"),
            # REUSE-IgnoreStart
            ("metadata-lines", "# SPDX-License-Identifier: MIT\n" + self.mit, "MIT"),
            ("attribution-metadata", "# SPDX-FileCopyrightText: 2026 Owner\n// SPDX-FileContributor: A. Name\n"
             "# REUSE-Copyright: 2026 Another Owner\n" + self.mit, "MIT"),
            # REUSE-IgnoreEnd
        )
        for name, body, declared in cases:
            with self.subTest(name=name):
                self.assertEqual(skill_problems(self._plant(body, declared, name=name)), [])

    def test_over_size_cap(self):
        """Refuse a valid body that is one byte over the cap."""
        skill = self._plant(self.mit.ljust(256 * 1024 + 1), name="oversize")
        self._refused(skill, "256 KiB")

    def _plant_licence_exception(self, body=None):
        """Plant a skill, its index row and a reviewed exception entry."""
        if body is None:
            body = self.mit + "\nUpstream attribution note.\n"
        skill = self._plant(body)
        write_index(self.root, [{"skill": skill.name}])
        return {
            "rule": "skill-licence-text",
            "path": f"skills/{skill.name}/LICENSE",
            "spdx": "MIT",
            "sha256": sha256(body.encode()).hexdigest(),
            "reason": "The upstream text includes an attribution note.",
            "category": "D",
            "entered": "2026-11-24",
            "expires": "2027-03-24",
        }

    def _write_licence_exceptions(self, entries):
        """Write the checker input, preserving string dates in YAML."""
        register = self.root / "skills" / "licence-exceptions.yaml"
        register.write_text(yaml.safe_dump({"exceptions": entries}), encoding="utf-8")
        return register

    def _check_licence_exceptions(self, *messages, today=date(2027, 3, 23)):
        """Expect either one refusal with the given details or one accepted pin."""
        output = io.StringIO()
        with redirect_stdout(output):
            result = check(self.root, today=today)
        report = output.getvalue()
        self.assertEqual(result, int(bool(messages)), report)
        if messages:
            failures = [line for line in report.splitlines() if line.startswith("[FAIL]")]
            self.assertEqual(len(failures), 1, report)
            for message in messages:
                self.assertIn(message, failures[0])
        pinned = 0 if messages else 1
        self.assertIn(f"pinned licence texts: {pinned}", report)

    def test_empty_licence_cannot_be_pinned(self):
        """Refuse an empty body before accepting a matching byte pin."""
        for body in ("", " \t\n", "MIT License\nCopyright (c) <year> <copyright holders>\n"):
            with self.subTest(body=body):
                self._write_licence_exceptions([self._plant_licence_exception(body)])
                self._check_licence_exceptions("LICENSE has no licence body")

    def test_pinned_licence_variant(self):
        """Accept a reviewed variant with unchanged bytes."""
        self._write_licence_exceptions([self._plant_licence_exception()])
        self._check_licence_exceptions()

    def test_pinned_licence_changed_byte(self):
        """Report both hashes and the original word difference for a changed byte."""
        entry = self._plant_licence_exception()
        self._write_licence_exceptions([entry])
        path = self.root / entry["path"]
        changed = path.read_bytes().replace(b"Upstream", b"upstream", 1)
        path.write_bytes(changed)
        self._check_licence_exceptions(
            entry["path"], "hash mismatch", entry["sha256"], sha256(changed).hexdigest(),
            "LICENSE differs from MIT at word offset",
        )

    def test_pinned_licence_spdx_mismatch(self):
        """Refuse a pin under a different SPDX licence."""
        entry = self._plant_licence_exception()
        self._write_licence_exceptions([entry | {"spdx": "Apache-2.0"}])
        self._check_licence_exceptions(entry["path"], "SPDX mismatch", "Apache-2.0", "MIT")

    def test_pinned_licence_expired(self):
        """Refuse an exception the day after its expiry."""
        entry = self._plant_licence_exception()
        self._write_licence_exceptions([entry])
        self._check_licence_exceptions(entry["path"], "expired", entry["expires"], today=date(2027, 3, 25))

    def test_pinned_licence_expiry_boundary(self):
        """Accept an exception through its expiry date."""
        self._write_licence_exceptions([self._plant_licence_exception()])
        self._check_licence_exceptions(today=date(2027, 3, 24))

    def test_pinned_licence_date_order(self):
        """Reject future entry dates and expiry before entry."""
        entry = self._plant_licence_exception() | {"category": "A", "expires": "2026-11-24"}
        self._write_licence_exceptions([entry])
        self._check_licence_exceptions(today=date(2026, 11, 24))
        with self.subTest(case="entered after check date"):
            self._check_licence_exceptions("entered", "after check date", today=date(2026, 11, 23))
        with self.subTest(case="expires before entered"):
            self._write_licence_exceptions([entry | {"expires": "2026-11-23"}])
            self._check_licence_exceptions("expires", "before entered", today=date(2026, 11, 24))

    def test_stale_licence_exception(self):
        """Require removal when the file now matches the canonical licence."""
        entry = self._plant_licence_exception()
        self._write_licence_exceptions([entry])
        (self.root / entry["path"]).write_text(self.mit, encoding="utf-8")
        self._check_licence_exceptions(entry["path"], "remove the exception")

    def test_licence_exception_missing_skill(self):
        """Refuse a register entry for a skill that is absent."""
        entry = self._plant_licence_exception(self.mit) | {"path": "skills/absent/LICENSE"}
        self._write_licence_exceptions([entry])
        self._check_licence_exceptions(entry["path"], "remove the exception")

    def test_licence_exception_invalid_fields(self):
        """Refuse unknown, missing, mistyped and invalid exception fields."""
        entry = self._plant_licence_exception()
        cases = (
            ("unknown", entry | {"extra": "value"}, "extra", "unknown"),
            ("missing", {k: v for k, v in entry.items() if k != "reason"}, "reason", "missing"),
            ("type", entry | {"reason": ["text"]}, "reason", "string"),
            ("blank-reason", entry | {"reason": " "}, "reason", "nonempty"),
            ("rule", entry | {"rule": "another-rule"}, "rule", "skill-licence-text"),
            ("path", entry | {"path": "skills/../LICENSE"}, "path", "skills/<name>/LICENSE"),
            ("spdx-shape", entry | {"spdx": "mit"}, "spdx", "supported SPDX identifier"),
            ("unknown-category", entry | {"category": "E"}, "category", "expected A, B, C or D"),
            ("date", entry | {"entered": "20261124"}, "entered", "YYYY-MM-DD"),
            ("impossible-date", entry | {"entered": "2026-11-31"}, "entered", "invalid date"),
            ("category-arithmetic", entry | {"category": "B"}, "expires", "2026-11-27"),
            ("hash-shape", entry | {"sha256": "A" * 64}, "sha256", "64 lowercase hexadecimal"),
        )
        for name, invalid, key, detail in cases:
            with self.subTest(case=name):
                self._write_licence_exceptions([invalid])
                self._check_licence_exceptions("licence-exceptions.yaml", "entry 1", key, detail)
        self._write_licence_exceptions([entry | {"entered": "9999-12-31", "expires": "9999-12-31"}])
        self._check_licence_exceptions("licence-exceptions.yaml", "leaves no expiry date", today=date.max)

    def test_licence_exception_invalid_structure(self):
        """Reject a malformed register, document or entry with one diagnostic."""
        self._plant_licence_exception(self.mit)
        register = self._write_licence_exceptions([])
        cases = (
            ("exceptions: [", "invalid YAML"),
            ("extra: []", "exactly the key exceptions"),
            ("exceptions: {}", "exceptions: expected a list"),
            ("exceptions: [3]", "entry 1: expected a mapping"),
        )
        for body, detail in cases:
            with self.subTest(detail=detail):
                register.write_text(body, encoding="utf-8")
                self._check_licence_exceptions("licence-exceptions.yaml", detail)

    def test_pinned_licence_categories(self):
        """Accept the category A date and the shorter category B and C intervals."""
        entry = self._plant_licence_exception()
        for category, expires in (("A", "2026-12-10"), ("B", "2026-11-27"), ("C", "2026-12-04")):
            with self.subTest(category=category):
                self._write_licence_exceptions([entry | {"category": category, "expires": expires}])
                self._check_licence_exceptions(today=date(2026, 11, 27))

    def test_licence_exception_duplicate_path(self):
        """Reject multiple entries for one skill licence."""
        entry = self._plant_licence_exception(self.mit)
        self._write_licence_exceptions([entry, entry.copy()])
        self._check_licence_exceptions("entry 2", entry["path"], "duplicate path")

    def test_licence_exception_duplicate_yaml_key(self):
        """Reject repeated YAML keys before validating an entry."""
        entry = self._plant_licence_exception(self.mit)
        register = self._write_licence_exceptions([entry])
        with register.open("a", encoding="utf-8") as handle:
            handle.write("  spdx: MIT\n")
        self._check_licence_exceptions("licence-exceptions.yaml", "duplicate key 'spdx'")

    def test_licence_exception_entry_cap(self):
        """Reject an oversized list before validating its entries."""
        entry = self._plant_licence_exception(self.mit)
        self._write_licence_exceptions([entry, entry.copy()])
        with patch.object(sys.modules[__name__], "MAX_LICENCE_EXCEPTIONS", 1, create=True):
            self._check_licence_exceptions("licence-exceptions.yaml", "at most 1 entries")

    def test_missing_licence_exception_register(self):
        """Keep the ordinary body refusal when no register exists."""
        entry = self._plant_licence_exception()
        self._check_licence_exceptions(Path(entry["path"]).parent.name, "LICENSE differs from MIT at word offset")


if __name__ == "__main__":
    if sys.argv[1:2] == ["selftest"]:
        sys.exit(selftest())
    sys.exit(check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
