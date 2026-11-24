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
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from markdown_it import MarkdownIt
import yaml

try:
    from install_user_skills import read_install_set
    from licence_policy import LICENCE_POINTERS, candidate_licence_id
    from licence_text import MAX_LICENCE_BYTES, read_utf8, verify_licence
except ImportError:
    from tools.install_user_skills import read_install_set
    from tools.licence_policy import LICENCE_POINTERS, candidate_licence_id
    from tools.licence_text import MAX_LICENCE_BYTES, read_utf8, verify_licence

COMMONMARK = MarkdownIt("commonmark")
MAX_SKILLS = 5000  # bounds the directories read (HISS-02)
MAX_SKILL_ENTRIES = 1000  # bounds the entries walked in one skill (HISS-02)
MAX_SKILL_MD_BYTES = 2 * 1024 * 1024
MAX_ORIGIN_BYTES = 256 * 1024
MAX_YAML_MAPPING_KEYS = 1000
MAX_LICENCE_EXCEPTIONS = 128
LICENCE_EXCEPTIONS_FILE = "skills/licence-exceptions.yaml"
LICENCE_EXCEPTION_KEYS = frozenset({"rule", "path", "spdx", "sha256", "reason", "category", "entered", "expires"})
LICENCE_EXCEPTION_DAYS = {"B": 3, "C": 10, "D": 120}
AGENT_CONTROL_FILES = frozenset({
    "protocol.md", "agents.md", "claude.md", "gemini.md",
    ".cursorrules", ".windsurfrules", "copilot-instructions.md",
})
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FIELD = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.+)$", re.M)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
INLINE_CODE = re.compile(r"`[^`]*`")
MD_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CHECKED_LINK_SUFFIXES = (".md", ".markdown", ".mdx", ".py", ".sh", ".mjs", ".js", ".ts")
# run-time installs and remote code: a skill must use the tools the project already pins
FETCH = re.compile(r"\b(?:pip install|uv pip install|uvx |npx |npm install|pnpm add|yarn add|go install|cargo install|"
                   r"curl[^\n|]*\|\s*(?:ba)?sh|wget[^\n|]*\|\s*(?:ba)?sh)")


def fields(text):
    """Read the scalar provenance fields in ORIGIN.md."""
    return {m.group("key"): m.group("value").strip() for m in FIELD.finditer(text)}


class _UniqueKeyLoader(yaml.SafeLoader):
    """Load validation YAML with unique keys at every mapping level."""

    def _require_unique_keys(self, node):
        """Check keys before merge expansion and again on the effective mapping."""
        if len(node.value) > MAX_YAML_MAPPING_KEYS:
            raise ValueError(f"mapping exceeds {MAX_YAML_MAPPING_KEYS} keys")
        seen = set()
        for key_node, _ in node.value:
            tag = key_node.tag
            key = key_node.value if tag.endswith((":merge", ":value")) else self.construct_object(key_node)
            try:
                if key in seen:
                    raise ValueError(f"duplicate key {key!r}")
                seen.add(key)
            except TypeError as exc:
                raise ValueError(f"unhashable YAML key {key!r}") from exc

    def construct_mapping(self, node, deep=False):
        """Refuse duplicate literal and merged keys before constructing values."""
        self._require_unique_keys(node)
        self.flatten_mapping(node)
        self._require_unique_keys(node)
        return super().construct_mapping(node, deep=deep)


def _load_yaml(text: str, loader: type[yaml.SafeLoader], *, error_prefix: str = "") -> object:
    """Construct the typed SafeLoader subclass directly to expose the safe call to scanners."""
    try:
        parser = loader(text)
        try:
            return parser.get_single_data()
        finally:
            parser.dispose()
    except (yaml.YAMLError, RecursionError) as exc:
        message = str(exc)
        if error_prefix:
            message = error_prefix + " ".join(message.split())
        raise ValueError(message) from exc


@dataclass(frozen=True)
class _LicenceException:
    """Identify the reviewed bytes and expiry for one skill licence."""

    path: str
    spdx: str
    sha256: str
    expires: date


@dataclass
class _LicenceScan:
    """Track pending register entries and counts of verified licences and pins."""

    pending: dict[str, _LicenceException]
    today: date
    pinned: int = 0
    verified: Counter[str] = field(default_factory=Counter)


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


def _exception_expiry(entry: dict[str, str]) -> date:
    """Apply the declared category's expiry interval."""
    category = entry["category"]
    entered = _exception_date(entry, "entered")
    expires = _exception_date(entry, "expires")
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


def _parse_licence_exception(raw: object) -> _LicenceException:
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
    return _LicenceException(path, spdx, digest, _exception_expiry(entry))


def _read_licence_exceptions(root: Path) -> dict[str, _LicenceException]:
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
    by_path = {}
    for index, raw in enumerate(entries, 1):
        try:
            entry = _parse_licence_exception(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"entry {index}: {exc}") from exc
        if entry.path in by_path:
            raise ValueError(f"entry {index}: duplicate path {entry.path}")
        by_path[entry.path] = entry
    return by_path


def _load_front_matter(text: str, loader: type[yaml.SafeLoader]) -> dict:
    """Parse the YAML mapping without converting value types."""
    match = FRONT.match(text.replace("\r\n", "\n"))
    if not match:
        return {}
    data = _load_yaml(match.group(1), loader)
    if not isinstance(data, dict):
        raise ValueError("expected a mapping")
    return data


def front_matter(text: str, *, exact_license: bool = False) -> dict[str, str]:
    """Project up to 100 YAML fields to strings for upstream discovery."""
    loader = _UniqueKeyLoader if exact_license else yaml.SafeLoader
    data = _load_front_matter(text, loader)
    meta: dict[str, str] = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= 100:
            break
        if isinstance(v, str):
            meta[str(k)] = v.strip()
        elif not isinstance(v, (list, dict, type(None))):
            meta[str(k)] = str(v)
    if exact_license and "license" in data:
        licence = data["license"]
        meta["license"] = licence if isinstance(licence, str) else f"<{type(licence).__name__}>"
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


def _front_licence_problem(meta: dict, spdx: str) -> str | None:
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
        if " ".join(raw.casefold().split()) in LICENCE_POINTERS:
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


def _licence_text_problem(skill: Path, template_dir: Path, spdx: str,
                          scan: _LicenceScan | None, entry: _LicenceException | None) -> str | None:
    """Check the canonical text first and consult a pin only for a body difference."""
    path = skill / "LICENSE"
    try:
        verify_licence(path, template_dir, spdx)
    except (ValueError, FileNotFoundError) as exc:
        difference = str(exc)
        if scan is None or entry is None or not difference.startswith(f"LICENSE differs from {spdx} at "):
            return f"{skill.name}: {difference}"
        if problem := _pinned_licence_problem(path, spdx, entry, scan.today, difference):
            return problem
        scan.pinned += 1
    else:
        if entry is not None:
            return f"{entry.path}: LICENSE matches {spdx}; remove the exception"
    return None


def _licence_problem(skill: Path, declared: str, meta: dict, template_dir: Path,
                     scan: _LicenceScan | None) -> str | None:
    """Report the first licence problem or record the verified SPDX ID."""
    relative_path = f"skills/{skill.name}/LICENSE"
    entry = scan.pending.pop(relative_path, None) if scan is not None else None
    spdx = candidate_licence_id(declared)
    if spdx is None:
        return f"{skill.name}/ORIGIN.md: licence {declared} not recognised"
    front_problem = _front_licence_problem(meta, spdx)
    if front_problem:
        return f"{skill.name}/SKILL.md: {front_problem}"
    if problem := _licence_text_problem(skill, template_dir, spdx, scan, entry):
        return problem
    if scan is not None:
        scan.verified[spdx] += 1
    return None


def skill_problems(
    skill: Path, *, template_dir: Path | None = None, licence_scan: _LicenceScan | None = None,
) -> list[str]:
    """Problems of one skill directory, each naming the file and the rule."""
    out = []
    md, origin = skill / "SKILL.md", skill / "ORIGIN.md"
    try:
        text = read_utf8(md, MAX_SKILL_MD_BYTES)
    except (ValueError, FileNotFoundError) as exc:
        return [f"{skill.name}: {exc}"]
    try:
        meta = _load_front_matter(text, _UniqueKeyLoader)
    except ValueError as exc:
        return [f"{skill.name}/SKILL.md: front matter is not valid YAML: {' '.join(str(exc).split())}"]
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
    try:
        rec = fields(read_utf8(origin, MAX_ORIGIN_BYTES))
    except (ValueError, FileNotFoundError) as exc:
        return out + [f"{skill.name}: {exc}"]
    if rec.get("upstream") == "own":
        return out
    for key in ("upstream", "path", "commit", "licence", "changes"):
        if not rec.get(key):
            out.append(f"{skill.name}/ORIGIN.md: needs '{key}:'")
    if rec.get("commit") and not COMMIT.match(rec["commit"]):
        out.append(f"{skill.name}/ORIGIN.md: commit must be the full 40-hex SHA")
    if declared := rec.get("licence"):
        if template_dir is None:
            template_dir = skill.parent.parent / "LICENSES"
        if problem := _licence_problem(skill, declared, meta, template_dir, licence_scan):
            out.append(problem)
    return out


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


def check(root: Path, *, today: date | None = None) -> int:
    """Check the skill tree and report verified licence counts."""
    skills = []
    scan = _LicenceScan({}, datetime.now(UTC).date() if today is None else today)
    try:
        scan.pending = _read_licence_exceptions(root)
    except (TypeError, ValueError) as exc:
        problems = [f"{LICENCE_EXCEPTIONS_FILE}: {' '.join(str(exc).split())}"]
    else:
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            skills = sorted(p for p in skills_dir.iterdir() if p.is_dir())[:MAX_SKILLS]
        problems = [p for s in skills for p in skill_problems(s, licence_scan=scan)]
        problems.extend(f"{path}: no applicable skill licence; remove the exception" for path in sorted(scan.pending))
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
    cases = (
        ("carrier-licence", mit, "Apache-2.0", "Apache-2.0",
         "LICENSE differs from Apache-2.0 at word offset"),
        ("apache-as-mit", apache, "MIT", None, "LICENSE differs from MIT at word offset"),
        ("mit-zero-as-mit", mit_zero, "MIT", None, "LICENSE differs from MIT at word offset"),
        ("mit-as-mit-zero", mit, "MIT-0", None, "LICENSE differs from MIT-0 at word offset"),
        ("unknown-text", "All rights reserved.\n", "MIT", None, "LICENSE differs from MIT at word offset"),
        ("unknown-origin", mit, "GPL-3.0", None, "ORIGIN.md: licence GPL-3.0 not recognised"),
        ("front-mismatch", mit, "MIT", "Apache-2.0",
         "front matter license Apache-2.0 disagrees with ORIGIN.md licence MIT"),
        ("unknown-front", mit, "MIT", "BSD-ish", "front matter license BSD-ish not recognised"),
        ("null-front", mit, "MIT", "null", "front matter license has type NoneType"),
        ("padded-front", mit, "MIT", '" MIT "', None),
        ("mit-alias", mit, "MIT", '"MIT license"', None),
        ("license-pointer", apache, "Apache-2.0", "Complete terms in LICENSE.txt", None),
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
        cases = (
            ("mit-quotes-apache", self.mit + '\n"Apache License, Version 2.0"\n', "Apache-2.0", "word offset"),
            ("late-mit-notice", late_notice, "MIT-0", "word offset"),
            ("apache-heading", "Apache License, Version 2.0\n", "Apache-2.0", "word offset"),
            ("bare-mit-grant", "Permission is hereby granted, free of charge\n", "MIT-0", "word offset"),
            ("invalid-utf8", b"\xff\n" + self.mit.encode(), "MIT", "invalid UTF-8"),
            ("apache-plus-notice", self.apache + "\n" + self.mit[notice_start:], "Apache-2.0", "word offset"),
            ("mit-extra-word", self.mit + "\nExtra\n", "MIT", "word offset"),
            ("mit-as-mit-zero", self.mit, "MIT-0", "word offset"),
            ("mit-zero-as-mit", self.mit_zero, "MIT", "word offset"),
        )
        for name, body, declared, message in cases:
            with self.subTest(name=name):
                self._refused(self._plant(body, declared, name=name), message)

    def test_candidate_families(self):
        """Admit additional candidate families only when their template exists."""
        ids = ("ISC", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Zlib", "Unlicense",
               "MPL-2.0", "EUPL-1.2", "LGPL-2.1", "LGPL-3.0-only", "LGPL-2.0-or-later")
        for spdx in ids:
            body = f"Fixture terms for {spdx}. The complete grant appears here.\n"
            skill = self._plant(body, spdx, name=spdx.lower())
            with self.subTest(spdx=spdx, template="missing"):
                self._refused(skill, f"reuse download {spdx}")
            with self.subTest(spdx=spdx, template="present"):
                (self.templates / f"{spdx}.txt").write_text(body, encoding="utf-8")
                self.assertEqual(skill_problems(skill), [])

    def test_front_matter(self):
        """Accept shared ID forms and exact file pointers, reject other values."""
        cases = (
            ("mit-name", "MIT License", "MIT", self.mit, None),
            ("mit-lower", "mit", "MIT", self.mit, None),
            ("mit-origin", "MIT", "mit licence", self.mit, None),
            ("apache-name", "Apache 2.0", "Apache-2.0", self.apache, "not recognised"),
            ("see-licence", "SEE LICENSE IN LICENSE", "MIT", self.mit, None),
            ("see-txt", "SEE LICENSE IN LICENSE.txt", "MIT", self.mit, None),
            ("complete-txt", "Complete terms in LICENSE.txt", "MIT", self.mit, None),
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

    def test_unsafe_yaml_tags(self):
        """Refuse Python-specific YAML tags in discovery and validation."""
        document = "---\nname: fixture\nmetadata: !!python/object:types.SimpleNamespace {}\n---\n"
        for exact_license in (False, True):
            with self.subTest(exact_license=exact_license):
                with self.assertRaisesRegex(ValueError, "could not determine a constructor"):
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
            "non-exclusive", "non–exclusive").replace("1. Definitions.", "• Definitions.")
        cases = (
            ("apache-no-appendix", self.apache[:apache_end], "Apache-2.0"),
            ("apache-formatting", formatted_apache, "Apache-2.0"),
            ("bom-crlf", b"\xef\xbb\xbf" + self.mit.replace("\n", "\r\n").encode(), "MIT"),
            ("long-mit", self.mit.replace("The above copyright notice", " " * 4096 + "The above copyright notice"), "MIT"),
            ("cap-minus-one", self.mit.ljust(256 * 1024 - 1), "MIT"),
            ("normalisation", self.mit.lower().replace('"', "“").replace("license", "licence"), "MIT"),
            ("title-absent", self.mit.removeprefix("MIT License\n"), "MIT"),
            ("title-alias", self.mit.replace("MIT License", "The MIT License", 1), "MIT"),
            ("metadata-lines", "# SPDX-License-Identifier: MIT\n© 2026 Owner\n" + self.mit, "MIT"),
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
        now = datetime(today.year, today.month, today.day, tzinfo=UTC)
        with (patch.object(sys.modules[__name__], "datetime", create=True) as clock,
              redirect_stdout(output)):
            clock.now.return_value = now
            result = check(self.root)
        report = output.getvalue()
        self.assertEqual(result, int(bool(messages)), report)
        if messages:
            failures = [line for line in report.splitlines() if line.startswith("[FAIL]")]
            self.assertEqual(len(failures), 1, report)
            for message in messages:
                self.assertIn(message, failures[0])
        pinned = 0 if messages else 1
        self.assertIn(f"pinned licence texts: {pinned}", report)

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
            ("category-overflow", entry | {"entered": "9999-12-31"}, "entered", "leaves no expiry date"),
            ("hash-shape", entry | {"sha256": "A" * 64}, "sha256", "64 lowercase hexadecimal"),
        )
        for name, invalid, key, detail in cases:
            with self.subTest(case=name):
                self._write_licence_exceptions([invalid])
                self._check_licence_exceptions("licence-exceptions.yaml", "entry 1", key, detail)

    def test_licence_exception_invalid_structure(self):
        """Reject a malformed register, document or entry with one diagnostic."""
        self._plant_licence_exception()
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
