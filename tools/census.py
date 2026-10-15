#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Deterministically inventory public SKILL.md files without downloading blobs.

Usage: census.py run [--sources FILE] [--out FILE] [--limit-per-source N]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

NETWORK_TIMEOUT_SECONDS = 30
MAX_SOURCES = 1000
MAX_MANIFEST_BYTES = 1 << 20
SOURCE_FIELDS = frozenset({"repository", "note", "found_at"})
REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
MAX_SKILLS_PER_SOURCE = 100_000
TEXT_SUFFIXES = frozenset({"", ".txt", ".rst", ".adoc", ".log", ".csv", ".tsv"})
MARKDOWN_SUFFIXES = frozenset({".md", ".mdx", ".markdown"})
IMAGE_SUFFIXES = frozenset({".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"})
# GitHub returns SPDX identifiers but does not expose the OSI approval flag. This allow-list contains the licences
# normally recognised by GitHub's licence API; an unknown identifier is conservatively not eligible without a folder
# licence. The census records the identifier either way, so a later adoption stage can make a finer decision.
OSI_SPDX = frozenset({
    "0BSD", "AFL-1.1", "AFL-1.2", "AFL-2.0", "AFL-2.1", "AFL-3.0", "AGPL-1.0", "AGPL-1.0-only",
    "AGPL-1.0-or-later", "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "Apache-1.0", "Apache-1.1",
    "Apache-2.0", "Artistic-1.0", "Artistic-2.0", "BSD-1-Clause", "BSD-2-Clause", "BSD-2-Clause-Patent",
    "BSD-3-Clause", "BSD-3-Clause-Clear", "BSL-1.0", "CDDL-1.0", "CDDL-1.1", "CECILL-2.1", "CPL-1.0",
    "ECL-1.0", "ECL-2.0", "EFL-1.0", "EFL-2.0", "EPL-1.0", "EPL-2.0", "EUDatagrid", "EUPL-1.1",
    "EUPL-1.2", "Fair", "Frameworx-1.0", "FreeBSD-DOC", "GPL-1.0", "GPL-1.0-only", "GPL-1.0-or-later",
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later", "HPND",
    "IPL-1.0", "ISC", "LGPL-2.0", "LGPL-2.0-only", "LGPL-2.0-or-later", "LGPL-2.1", "LGPL-2.1-only",
    "LGPL-2.1-or-later", "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later", "LPL-1.0", "LPL-1.02",
    "MIT", "MIT-0", "MPL-1.0", "MPL-1.1", "MPL-2.0", "MS-PL", "MS-RL", "MulanPSL-2.0", "NASA-1.3",
    "NCSA", "NGPL", "Nokia", "NTP", "OCLC-2.0", "OFL-1.1", "OGTSL", "OSL-1.0", "OSL-1.1", "OSL-2.0",
    "OSL-2.1", "OSL-3.0", "PHP-3.0", "PHP-3.01", "PostgreSQL", "Python-2.0", "QPL-1.0", "RPL-1.1",
    "RPL-1.5", "RPSL-1.0", "RSCPL", "SimPL-2.0", "Sleepycat", "SPL-1.0", "UPL-1.0", "VSL-1.0", "W3C",
    "Watcom-1.0", "Wsuipa", "Xnet", "Zlib", "ZPL-1.1", "ZPL-2.0", "ZPL-2.1",
})
HEADER = [
    "source", "pin", "path", "skill_name", "blob_id", "size", "folder_file_count", "scripts_present",
    "folder_license", "repository_license", "ineligible", "dup_of_blob", "dup_of_name", "failure_reason",
]


class SourceReadError(RuntimeError):
    """A source was public in the manifest but could not be read."""


@dataclass(frozen=True)
class Source:
    repository: str
    note: str
    found_at: str


@dataclass
class Row:
    source: str
    pin: str
    path: str
    skill_name: str
    blob_id: str
    size: int
    folder_file_count: int
    scripts_present: list[str]
    folder_license: str
    repository_license: str
    ineligible: str = ""
    dup_of_blob: str = ""
    dup_of_name: str = ""
    failure_reason: str = ""

    @property
    def identifier(self) -> str:
        return f"{self.source}:{self.path}"

    def values(self) -> list[str]:
        return [
            self.source, self.pin, self.path, self.skill_name, self.blob_id, str(self.size),
            str(self.folder_file_count), ",".join(self.scripts_present), self.folder_license,
            self.repository_license, self.ineligible, self.dup_of_blob, self.dup_of_name,
            self.failure_reason,
        ]


@dataclass(frozen=True)
class Failure:
    source: str
    reason: str


@dataclass(frozen=True)
class Reconciliation:
    skill_count: int
    blob_duplicates: int
    unique_blobs: int
    ok: bool


@dataclass
class Result:
    rows: list[Row] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def reconcile(self) -> Reconciliation:
        skill_count = len(self.rows)
        duplicates = sum(bool(row.dup_of_blob) for row in self.rows)
        unique = len({row.blob_id for row in self.rows})
        return Reconciliation(skill_count, duplicates, unique, skill_count - duplicates == unique)


def load_sources(path: Path) -> list[Source]:
    """Read the source manifest: a JSON object whose only key, sources, lists the repositories."""
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"{path}: manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"sources"} or not isinstance(document["sources"], list):
        raise ValueError(f"{path}: expected an object with one key, sources, holding a list")
    entries = document["sources"]
    if not entries or len(entries) > MAX_SOURCES:
        raise ValueError(f"{path}: source count must be between 1 and {MAX_SOURCES}")
    sources: list[Source] = []
    seen: set[str] = set()
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) != SOURCE_FIELDS:
            raise ValueError(f"{path}: source {number} needs exactly repository, note and found_at")
        if not all(isinstance(entry[key], str) and entry[key].strip() for key in SOURCE_FIELDS):
            raise ValueError(f"{path}: source {number} has an empty or non-text field")
        repository = entry["repository"]
        if not REPOSITORY_RE.fullmatch(repository):
            raise ValueError(f"{path}: source {number} repository {repository!r} is not owner/name")
        if repository.lower() in seen:
            raise ValueError(f"{path}: source {number} repeats {repository}")
        seen.add(repository.lower())
        sources.append(Source(**entry))
    return sources


class LiveReader:
    """The only network boundary: GitHub CLI, git ls-remote, and truncated-tree clone fallback."""

    def run(self, args: list[str], *, network: bool = True, cwd: Path | None = None) -> str:
        try:
            done = subprocess.run(
                args, cwd=cwd, text=True, capture_output=True, check=False,
                timeout=NETWORK_TIMEOUT_SECONDS if network else None,
            )
        except subprocess.TimeoutExpired as error:
            raise SourceReadError(f"timeout after {NETWORK_TIMEOUT_SECONDS}s: {args[0]}") from error
        if done.returncode:
            detail = (done.stderr or done.stdout).strip().replace("\n", " ")
            raise SourceReadError(detail or f"{args[0]} exited {done.returncode}")
        return done.stdout

    def gh_json(self, endpoint: str) -> Any:
        args = ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint]
        for attempt in range(2):
            try:
                return json.loads(self.run(args))
            except SourceReadError as error:
                if attempt == 0 and "rate limit" in str(error).lower():
                    time.sleep(1)
                    continue
                raise
        raise AssertionError("bounded retry exhausted")

    def resolve_pin(self, source: Source) -> str:
        repo = self.gh_json(f"repos/{source.repository}")
        branch = repo.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise SourceReadError("repository has no default branch")
        output = self.run(["git", "ls-remote", f"https://github.com/{source.repository}.git", f"refs/heads/{branch}"])
        fields = output.split()
        if not fields or len(fields[0]) != 40:
            raise SourceReadError(f"default branch {branch} has no resolvable head")
        return fields[0]

    def repository_license(self, source: Source) -> str:
        try:
            data = self.gh_json(f"repos/{source.repository}/license")
        except SourceReadError as error:
            if "404" in str(error):
                return "none"
            raise
        spdx = data.get("license", {}).get("spdx_id")
        return spdx if isinstance(spdx, str) and spdx != "NOASSERTION" else "none"

    def recursive_tree(self, source: Source, pin: str) -> dict[str, Any]:
        data = self.gh_json(f"repos/{source.repository}/git/trees/{pin}?recursive=1")
        if not isinstance(data.get("tree"), list):
            raise SourceReadError("GitHub tree response has no tree")
        return data

    def fallback_tree(self, source: Source, pin: str) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="peritia-census-") as temp:
            folder = Path(temp) / "repository"
            self.run([
                "git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
                f"https://github.com/{source.repository}.git", str(folder),
            ])
            output = self.run(["git", "ls-tree", "-r", "-l", "HEAD"], network=False, cwd=folder)
        entries = []
        for line in output.splitlines():
            try:
                metadata, path = line.split("\t", 1)
                mode, object_type, sha, size = metadata.split()
            except ValueError as error:
                raise SourceReadError("could not parse git ls-tree output") from error
            if object_type == "blob":
                entries.append({"path": path, "type": object_type, "sha": sha, "size": int(size)})
        return entries


def tree_entries(tree: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (entry for entry in tree if entry.get("type") == "blob" and isinstance(entry.get("path"), str)),
        key=lambda entry: entry["path"],
    )


def is_folder_license(path: str) -> bool:
    return Path(path).name.upper() == "LICENSE"


def is_script(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in MARKDOWN_SUFFIXES or suffix in TEXT_SUFFIXES or suffix in IMAGE_SUFFIXES:
        return None
    return suffix or "[no-extension]"


def rows_for_source(source: Source, pin: str, repository_license: str, entries: list[dict[str, Any]], limit: int | None) -> list[Row]:
    selected = [entry for entry in entries if Path(entry["path"]).name == "SKILL.md"]
    if limit is not None:
        selected = selected[:limit]
    rows = []
    repository_is_osi = repository_license in OSI_SPDX
    for skill in selected:
        path = skill["path"]
        folder = str(Path(path).parent)
        prefix = "" if folder == "." else folder + "/"
        folder_entries = [entry for entry in entries if entry["path"].startswith(prefix)]
        extensions = sorted({extension for entry in folder_entries if (extension := is_script(entry["path"]))})
        has_license = any(is_folder_license(entry["path"]) for entry in folder_entries)
        ineligible = "" if repository_is_osi or has_license else "licence:none"
        rows.append(Row(
            source=source.repository, pin=pin, path=path, skill_name=Path(folder).name if folder != "." else "SKILL",
            blob_id=str(skill.get("sha", "")), size=int(skill.get("size", 0)), folder_file_count=len(folder_entries),
            scripts_present=extensions, folder_license="y" if has_license else "n", repository_license=repository_license,
            ineligible=ineligible,
        ))
    return rows


def mark_duplicates(rows: list[Row]) -> None:
    blobs: dict[str, str] = {}
    names: dict[str, tuple[str, str]] = {}
    for row in rows:
        first_blob = blobs.setdefault(row.blob_id, row.identifier)
        if first_blob != row.identifier:
            row.dup_of_blob = first_blob
        first_name = names.setdefault(row.skill_name, (row.blob_id, row.identifier))
        if first_name[0] != row.blob_id:
            row.dup_of_name = first_name[1]


def collect(sources: list[Source], reader: Any, limit_per_source: int | None = None) -> Result:
    if limit_per_source is not None and not 1 <= limit_per_source <= MAX_SKILLS_PER_SOURCE:
        raise ValueError(f"limit must be between 1 and {MAX_SKILLS_PER_SOURCE}")
    result = Result()
    for source in sources:
        try:
            pin = reader.resolve_pin(source)
            licence = reader.repository_license(source)
            tree = reader.recursive_tree(source, pin)
            entries = reader.fallback_tree(source, pin) if tree.get("truncated") else tree_entries(tree["tree"])
            result.rows.extend(rows_for_source(source, pin, licence, tree_entries(entries), limit_per_source))
        except (SourceReadError, KeyError, TypeError, ValueError) as error:
            result.failures.append(Failure(source.repository, str(error)))
    result.rows.sort(key=lambda row: (row.source, row.path))
    mark_duplicates(result.rows)
    return result


def render_tsv(result: Result) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(HEADER)
    for row in result.rows:
        writer.writerow(row.values())
    for failure in sorted(result.failures, key=lambda item: item.source):
        writer.writerow([failure.source] + [""] * (len(HEADER) - 2) + [failure.reason])
    return output.getvalue()


def write_tsv(path: Path, result: Result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_tsv(result), encoding="utf-8", newline="")


def render_summary(result: Result, sources: list[Source]) -> str:
    lines = []
    for source in sources:
        rows = [row for row in result.rows if row.source == source.repository]
        failed = next((failure for failure in result.failures if failure.source == source.repository), None)
        if failed:
            lines.append(f"source={source.repository} skill_md=0 rows=0 blob_duplicates=0 name_duplicates=0 failed={failed.reason}")
            continue
        lines.append(
            f"source={source.repository} skill_md={len(rows)} rows={len(rows)} "
            f"blob_duplicates={sum(bool(row.dup_of_blob) for row in rows)} "
            f"name_duplicates={sum(bool(row.dup_of_name) for row in rows)}"
        )
    reconciliation = result.reconcile()
    lines.append(
        f"reconcile: skill_md={reconciliation.skill_count} - blob_duplicates={reconciliation.blob_duplicates} "
        f"= unique_blobs={reconciliation.unique_blobs} status={'ok' if reconciliation.ok else 'FAIL'}"
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="write one TSV row for every public SKILL.md")
    run.add_argument("--sources", type=Path, default=Path("tools/census-sources.json"))
    run.add_argument("--out", type=Path, default=Path("census.tsv"))
    run.add_argument("--limit-per-source", type=int)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    sources = load_sources(args.sources)
    result = collect(sources, LiveReader(), args.limit_per_source)
    write_tsv(args.out, result)
    print(render_summary(result, sources))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
