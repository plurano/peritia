#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Decide whether classified skills are adoption candidates and under which licence.

Reads census.tsv and classified.tsv, inspects source checkouts at the census pin,
resolves licences (folder, frontmatter, upstream trace for aggregators, or repository),
classifies them into permissive, weak, strong, or other, and writes candidates.tsv.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from check_skills import FIELD, FRONT  # noqa: E402
from classify import DOMAINS, FUNCTIONS  # noqa: E402

CORE_FUNCTIONS = frozenset({
    "coding",
    "review-audit",
    "testing",
    "ci-release",
    "docs-writing",
    "research",
    "planning-fleet-mgmt",
    "security",
    "infra-ops",
})
assert CORE_FUNCTIONS.issubset(FUNCTIONS), f"unknown core functions: {CORE_FUNCTIONS - set(FUNCTIONS)}"

MAX_ROWS = 200_000
MAX_GH_CALLS = 2000
GH_TIMEOUT_SECONDS = 20.0
MAX_SKILL_CHARS = 8192

SPDX_TABLE = (
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "CC-BY-4.0", "MPL-2.0", "EUPL-1.2",
    "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0", "AGPL-3.0", "MIT-0",
)
SPDX_LOOKUP = {s.upper(): s for s in SPDX_TABLE}

PERMISSIVE_LICENCES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "MIT-0",
})
WEAK_LICENCES = frozenset({"MPL-2.0", "EUPL-1.2"})

HEADINGS: list[tuple[str, re.Pattern[str]]] = [
    ("AGPL-3.0", re.compile(r"GNU\s+Affero\s+General\s+Public\s+License.*?(?:Version\s+3|v3|\b3\b)", re.I | re.S)),
    ("LGPL-3.0", re.compile(r"GNU\s+Lesser\s+General\s+Public\s+License.*?(?:Version\s+3|v3|\b3\b)", re.I | re.S)),
    ("LGPL-2.1", re.compile(r"GNU\s+(?:Lesser|Library)\s+General\s+Public\s+License.*?(?:Version\s+2\.1|v2\.1|\b2\.1\b)", re.I | re.S)),
    ("GPL-3.0", re.compile(r"GNU\s+General\s+Public\s+License.*?(?:Version\s+3|v3|\b3\b)", re.I | re.S)),
    ("GPL-2.0", re.compile(r"GNU\s+General\s+Public\s+License.*?(?:Version\s+2|v2|\b2\b)", re.I | re.S)),
    ("Apache-2.0", re.compile(r"Apache\s+License.*?(?:Version\s+2\.0|2\.0)", re.I | re.S)),
    ("MPL-2.0", re.compile(r"Mozilla\s+Public\s+License.*?(?:Version\s+2\.0|2\.0)", re.I | re.S)),
    ("EUPL-1.2", re.compile(r"European\s+Union\s+Public\s+Licen[sc]e.*?(?:Version\s+1\.2|v\.?\s*1\.2|\b1\.2\b)", re.I | re.S)),
    ("CC-BY-4.0", re.compile(r"Creative\s+Commons\s+Attribution\s+4\.0|CC[- ]BY[- ]4\.0", re.I)),
    ("CC0-1.0", re.compile(r"Creative\s+Commons\s+Zero|CC0\s+1\.0|CC0-1\.0", re.I)),
    ("0BSD", re.compile(r"\b0BSD\b|Zero[- ]Clause\s+BSD|Free\s+Public\s+License\s+1\.0\.0", re.I)),
    ("BSD-3-Clause", re.compile(r"BSD\s+3-Clause|3-Clause\s+BSD", re.I)),
    ("BSD-2-Clause", re.compile(r"BSD\s+2-Clause|2-Clause\s+BSD", re.I)),
    ("ISC", re.compile(r"\bISC\s+License\b", re.I)),
    ("Zlib", re.compile(r"\bzlib\s+License\b|The\s+zlib/libpng\s+License", re.I)),
    ("Unlicense", re.compile(r"\bThe\s+Unlicense\b|unlicense\.org", re.I)),
    ("MIT-0", re.compile(r"\bMIT-0\b|MIT\s+No\s+Attribution", re.I)),
    ("MIT", re.compile(r"\bMIT\s+License\b|\bThe\s+MIT\s+License\b", re.I)),
]

AGGREGATOR_UPSTREAM_KEYS = (
    "source_repo", "license_source", "source", "upstream", "origin", "repository", "homepage",
)
GITHUB_REPO_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)",
    re.I,
)
GITHUB_REPO_PLAIN_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)$",
)
LICENSE_FILENAMES = (
    "LICENSE", "LICENCE", "COPYING",
    "LICENSE.md", "LICENSE.txt", "LICENCE.md", "LICENCE.txt", "COPYING.txt",
)
LICENSE_FILE_PATTERN = re.compile(r"\b(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?\b", re.I)

OUTPUT_FIELDS = [
    "blob_id", "source", "path", "skill_name", "functions", "domains", "match_clause",
    "licence", "licence_class", "licence_evidence", "verdict", "reason",
]


@dataclass(frozen=True)
class CensusRow:
    source: str
    pin: str
    path: str
    skill_name: str
    blob_id: str
    folder_license: str
    repository_license: str
    ineligible: str
    dup_of_blob: str
    failure_reason: str


@dataclass(frozen=True)
class ClassifiedRow:
    blob_id: str
    source: str
    path: str
    skill_name: str
    functions: str
    domains: str
    model: str
    status: str


@dataclass(frozen=True)
class CandidateRow:
    blob_id: str
    source: str
    path: str
    skill_name: str
    functions: str
    domains: str
    match_clause: str
    licence: str
    licence_class: str
    licence_evidence: str
    verdict: str
    reason: str

    def values(self) -> list[str]:
        return [
            self.blob_id, self.source, self.path, self.skill_name,
            self.functions, self.domains, self.match_clause, self.licence, self.licence_class,
            self.licence_evidence, self.verdict, self.reason,
        ]


@dataclass(frozen=True)
class LicenceResolution:
    licence: str
    evidence: str
    verdict: str | None = None
    reason: str = ""

    def __iter__(self):
        return iter((self.licence, self.evidence))

    def __getitem__(self, item: int) -> str:
        return (self.licence, self.evidence)[item]


def identify_licence_text(text: str) -> str:
    sample = text[:MAX_SKILL_CHARS]
    for spdx_id, pattern in HEADINGS:
        if pattern.search(sample):
            return spdx_id
    for line in sample.splitlines()[:15]:
        cleaned = line.strip().strip("#/*=- \t'\"").upper()
        if cleaned in SPDX_LOOKUP:
            return SPDX_LOOKUP[cleaned]
    return "unknown"


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONT.match(text)
    if not match:
        return {}
    meta: dict[str, str] = {}
    for i, m in enumerate(FIELD.finditer(match.group(1))):
        if i >= 100:
            break
        meta[m.group("key")] = m.group("value").strip().strip("'\"")
    return meta


def normalise_licence(raw: str) -> tuple[str, str, bool]:
    """Normalises raw licence string.

    Returns (output_id, base_id, is_known).
    If unknown, returns ("unknown", "unknown", False).
    """
    if not raw:
        return ("unknown", "unknown", False)
    val = raw.strip().strip("'\"").strip()
    val = re.sub(r"\s+licen[sc]e$", "", val, flags=re.I).strip()
    val = val.strip("'\"").strip()
    if not val or val.lower() == "unknown":
        return ("unknown", "unknown", False)

    m = re.search(r"^(.*?)-(only|or-later)$", val, flags=re.I)
    if m:
        base_cand = m.group(1).strip()
        suffix = m.group(2).lower()
        if base_cand.upper() in SPDX_LOOKUP:
            canonical_base = SPDX_LOOKUP[base_cand.upper()]
            full_id = f"{canonical_base}-{suffix}"
            return (full_id, canonical_base, True)

    if val.upper() in SPDX_LOOKUP:
        canonical = SPDX_LOOKUP[val.upper()]
        return (canonical, canonical, True)

    return (val, val, False)


def licence_class(spdx: str) -> str:
    m = re.search(r"^(.*?)-(?:only|or-later)$", spdx, flags=re.I)
    base = m.group(1).strip() if m else spdx.strip()
    if base in PERMISSIVE_LICENCES:
        return "permissive"
    if base in WEAK_LICENCES or base.startswith("LGPL-"):
        return "weak"
    if base.startswith("GPL-") or base.startswith("AGPL-"):
        return "strong"
    return "other"


def parse_github_repo(val: str) -> str | None:
    cleaned = val.strip().strip("'\"").strip()
    if not cleaned:
        return None
    m_url = GITHUB_REPO_URL_RE.search(cleaned)
    if m_url:
        owner = m_url.group(1)
        name = m_url.group(2).removesuffix(".git").rstrip("/")
        return f"{owner}/{name}"
    m_plain = GITHUB_REPO_PLAIN_RE.match(cleaned)
    if m_plain:
        owner = m_plain.group(1)
        name = m_plain.group(2).removesuffix(".git").rstrip("/")
        return f"{owner}/{name}"
    return None


def extract_upstream_repo(meta: dict[str, str], aggregator_source: str | None = None) -> str | None:
    for key in AGGREGATOR_UPSTREAM_KEYS:
        val = meta.get(key)
        if not val:
            continue
        repo = parse_github_repo(val)
        if repo is None:
            continue
        if aggregator_source and repo.lower() == aggregator_source.lower():
            continue
        return repo
    return None


def find_folder_licence_file(folder: Path) -> Path | None:
    for name in LICENSE_FILENAMES:
        cand = folder / name
        if cand.is_file():
            return cand
    if folder.is_dir():
        for child in sorted(folder.iterdir()):
            if child.is_file() and re.match(r"^(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?$", child.name, re.I):
                return child
    return None


def find_folder_licence(folder: Path) -> str | None:
    f = find_folder_licence_file(folder)
    if f is not None:
        text = f.read_text(encoding="utf-8", errors="replace")
        return identify_licence_text(text)
    return None


class GitHubReader:
    def __init__(self, timeout: float = GH_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def repository_license(self, repo: str) -> str:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh api exited {proc.returncode}: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        lic = (data.get("license") or {}).get("spdx_id")
        if isinstance(lic, str) and lic and lic != "NOASSERTION":
            return lic
        return "untraced"


class UpstreamTracer:
    def __init__(
        self,
        reader: Any,
        cache_path: Path | None = None,
        max_calls: int = MAX_GH_CALLS,
    ) -> None:
        self.reader = reader
        self.cache_path = cache_path
        self.max_calls = max_calls
        self.call_count = 0
        self.cache = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        if self.cache_path and self.cache_path.is_file():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    def _save_cache(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def trace(self, repo: str) -> str:
        if repo in self.cache:
            return self.cache[repo]
        if self.call_count >= self.max_calls:
            return "untraced"
        self.call_count += 1
        lic = "untraced"
        try:
            if hasattr(self.reader, "repository_license"):
                lic = self.reader.repository_license(repo)
            elif callable(self.reader):
                lic = self.reader(repo)
        except Exception:
            lic = "untraced"
        if not isinstance(lic, str) or not lic or lic == "NOASSERTION":
            lic = "untraced"
        self.cache[repo] = lic
        self._save_cache()
        return lic


def check_candidate_filter(classified_row: ClassifiedRow, census_row: CensusRow) -> str | None:
    if classified_row.status != "ok":
        return f"status:{classified_row.status}"
    func_list = [f for f in classified_row.functions.split(";") if f]
    if not func_list or set(func_list) == {"none"}:
        return "functions:none"
    dom_list = [d for d in classified_row.domains.split(";") if d]
    has_fleet_domain = bool(dom_list and set(dom_list) != {"other"})
    has_core_func = any(f in CORE_FUNCTIONS for f in func_list)
    if not has_fleet_domain and not has_core_func:
        return "domains:other"
    if census_row.ineligible:
        return f"ineligible:{census_row.ineligible}"
    return None


def _resolve_aggregator_licence(
    census_row: CensusRow,
    skill_path: Path,
    tracer: UpstreamTracer,
) -> LicenceResolution:
    text = skill_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)

    # 3a. source_type self -> census repository licence, evidence repository:self
    if meta.get("source_type", "").strip().lower() == "self":
        lic = census_row.repository_license or "none"
        return LicenceResolution(lic, "repository:self")

    # 3b. upstream repository = first of keys yielding github owner/name; skip aggregator itself
    upstream_repo = extract_upstream_repo(meta, aggregator_source=census_row.source)
    if not upstream_repo:
        # 3d: No upstream found -> untraced, refused
        return LicenceResolution(
            "untraced",
            "none",
            verdict="refused",
            reason="licence:untraced evidence:none",
        )

    raw_traced = tracer.trace(upstream_repo)
    if raw_traced == "untraced":
        # 3d: trace fails -> untraced, refused
        evidence = f"upstream:{upstream_repo}"
        return LicenceResolution(
            "untraced",
            evidence,
            verdict="refused",
            reason=f"licence:untraced evidence:{evidence}",
        )

    traced_lic, _, _ = normalise_licence(raw_traced)
    traced_cls = licence_class(traced_lic)
    evidence = f"upstream:{upstream_repo}"

    # 3c. A recognised front matter licence whose class differs from traced class: verdict report, reason names both
    fm_raw = meta.get("license") or meta.get("licence")
    if fm_raw:
        fm_lic, _, fm_known = normalise_licence(fm_raw)
        if fm_known:
            fm_cls = licence_class(fm_lic)
            if fm_cls != traced_cls:
                reason = f"licence conflict: frontmatter {fm_lic} ({fm_cls}) vs traced {traced_lic} ({traced_cls})"
                return LicenceResolution(traced_lic, evidence, verdict="report", reason=reason)

    return LicenceResolution(traced_lic, evidence)


def _resolve_non_aggregator_licence(
    census_row: CensusRow,
    skill_path: Path,
    folder: Path,
) -> LicenceResolution:
    folder_lic_file = find_folder_licence_file(folder)
    if folder_lic_file is not None:
        folder_spdx = identify_licence_text(folder_lic_file.read_text(encoding="utf-8", errors="replace"))
        return LicenceResolution(folder_spdx, "folder")

    text = skill_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    fm_raw = meta.get("license") or meta.get("licence")
    if fm_raw is not None and fm_raw.strip() != "":
        fm_lic, _, fm_known = normalise_licence(fm_raw)
        if fm_known:
            return LicenceResolution(fm_lic, "frontmatter")

        # Rule 2a: Not a table id after normalisation.
        # When it names a licence file, the folder file decides;
        # otherwise licence unknown, evidence frontmatter, refused.
        m_file = LICENSE_FILE_PATTERN.search(fm_raw)
        if m_file:
            named_cand = folder / m_file.group(0)
            if named_cand.is_file():
                folder_spdx = identify_licence_text(named_cand.read_text(encoding="utf-8", errors="replace"))
                return LicenceResolution(folder_spdx, "folder")
            cand_any = find_folder_licence_file(folder)
            if cand_any is not None:
                folder_spdx = identify_licence_text(cand_any.read_text(encoding="utf-8", errors="replace"))
                return LicenceResolution(folder_spdx, "folder")

        # Fail closed: never fall through to repository licence
        return LicenceResolution(
            "unknown",
            "frontmatter",
            verdict="refused",
            reason="licence:unknown evidence:frontmatter",
        )

    return LicenceResolution(census_row.repository_license or "none", "repository")


def resolve_licence(
    census_row: CensusRow,
    checkouts: Path,
    aggregators: set[str],
    tracer: UpstreamTracer,
) -> LicenceResolution | None:
    """Returns LicenceResolution or None if path leaves the checkout."""
    root = (checkouts / census_row.source.replace("/", "__")).resolve()
    skill_path = (root / census_row.path).resolve()
    if root not in skill_path.parents:
        return None
    folder = skill_path.parent
    if folder != root and root not in folder.parents:
        return None

    # Rule 2b: census folder_license y but no licence file beside the SKILL.md in checkout
    if census_row.folder_license == "y":
        lic_file = find_folder_licence_file(folder)
        if lic_file is None:
            return LicenceResolution(
                licence="unreadable",
                evidence="folder-missing",
                verdict="refused",
                reason="census folder_license y but checkout lacks the file",
            )

    if census_row.source in aggregators:
        return _resolve_aggregator_licence(census_row, skill_path, tracer)
    return _resolve_non_aggregator_licence(census_row, skill_path, folder)


def _escaped_row(census_row: CensusRow, classified_row: ClassifiedRow) -> CandidateRow:
    return CandidateRow(
        blob_id=census_row.blob_id,
        source=census_row.source,
        path=census_row.path,
        skill_name=census_row.skill_name,
        functions=classified_row.functions,
        domains=classified_row.domains,
        match_clause="",
        licence="unknown",
        licence_class="other",
        licence_evidence="none",
        verdict="refused",
        reason="path leaves checkout",
    )


def _admit_clause(classified_row: ClassifiedRow) -> str:
    dom_list = [d for d in classified_row.domains.split(";") if d]
    if dom_list and set(dom_list) != {"other"}:
        return "domain"
    return "core-function"


def decide_row(
    census_row: CensusRow,
    classified_row: ClassifiedRow,
    checkouts: Path,
    aggregators: set[str],
    tracer: UpstreamTracer,
) -> CandidateRow:
    lic_res = resolve_licence(census_row, checkouts, aggregators, tracer)
    if lic_res is None:
        return _escaped_row(census_row, classified_row)
    licence, evidence = lic_res.licence, lic_res.evidence
    lic_cls = licence_class(licence)
    filter_reason = check_candidate_filter(classified_row, census_row)
    if filter_reason is not None:
        verdict, reason = "refused", filter_reason
    elif lic_res.verdict is not None:
        verdict, reason = lic_res.verdict, lic_res.reason
    elif lic_cls in ("permissive", "weak"):
        verdict, reason = "candidate", ""
    elif lic_cls == "strong":
        verdict, reason = "report", f"licence:{licence} strong copyleft: a person decides"
    else:
        verdict, reason = "refused", f"licence:{licence} evidence:{evidence}"

    match_clause = "" if verdict == "refused" else _admit_clause(classified_row)
    return CandidateRow(
        blob_id=census_row.blob_id,
        source=census_row.source,
        path=census_row.path,
        skill_name=census_row.skill_name,
        functions=classified_row.functions,
        domains=classified_row.domains,
        match_clause=match_clause,
        licence=licence,
        licence_class=lic_cls,
        licence_evidence=evidence,
        verdict=verdict,
        reason=reason,
    )


def load_classified(path: Path) -> dict[str, ClassifiedRow]:
    classified = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for i, row in enumerate(csv.DictReader(handle, delimiter="\t")):
            if i >= MAX_ROWS:
                break
            classified[row["blob_id"]] = ClassifiedRow(
                blob_id=row["blob_id"],
                source=row["source"],
                path=row["path"],
                skill_name=row["skill_name"],
                functions=row.get("functions", ""),
                domains=row.get("domains", ""),
                model=row.get("model", ""),
                status=row.get("status", ""),
            )
    return classified


def read_census_items(path: Path) -> list[CensusRow]:
    items = []
    with path.open(newline="", encoding="utf-8") as handle:
        for i, row in enumerate(csv.DictReader(handle, delimiter="\t")):
            if i >= MAX_ROWS:
                break
            if row.get("dup_of_blob") or row.get("failure_reason") or not row.get("blob_id"):
                continue
            items.append(CensusRow(
                source=row["source"],
                pin=row.get("pin", ""),
                path=row["path"],
                skill_name=row["skill_name"],
                blob_id=row["blob_id"],
                folder_license=row.get("folder_license", "n"),
                repository_license=row.get("repository_license", "none"),
                ineligible=row.get("ineligible", ""),
                dup_of_blob=row.get("dup_of_blob", ""),
                failure_reason=row.get("failure_reason", ""),
            ))
    return items


def write_tsv(rows: list[CandidateRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(OUTPUT_FIELDS)
        for i, row in enumerate(rows):
            if i >= MAX_ROWS:
                break
            writer.writerow(row.values())


def run(
    census_path: Path,
    classified_path: Path,
    checkouts: Path,
    out_path: Path,
    aggregators: Iterable[str],
    reader: Any,
    cache_path: Path | None = None,
    limit: int | None = None,
) -> list[CandidateRow]:
    classified = load_classified(classified_path)
    census_items = read_census_items(census_path)
    agg_set = {a.strip() for a in aggregators if a.strip()}
    tracer = UpstreamTracer(reader, cache_path=cache_path)
    rows: list[CandidateRow] = []
    for i, census_row in enumerate(census_items):
        if i >= MAX_ROWS:
            break
        if limit is not None and len(rows) >= limit:
            break
        classified_row = classified.get(census_row.blob_id)
        if classified_row is None:
            continue
        rows.append(decide_row(census_row, classified_row, checkouts, agg_set, tracer))
    write_tsv(rows, out_path)
    return rows


def summary(candidates_path: Path) -> str:
    verdicts: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    clauses: Counter[str] = Counter()
    with candidates_path.open(newline="", encoding="utf-8") as handle:
        for i, row in enumerate(csv.DictReader(handle, delimiter="\t")):
            if i >= MAX_ROWS:
                break
            v = row.get("verdict", "")
            verdicts[v] += 1
            classes[row.get("licence_class", "")] += 1
            if v == "candidate":
                for d in filter(None, row.get("domains", "").split(";")):
                    domains[d] += 1
                mc = row.get("match_clause", "")
                if mc in ("domain", "core-function"):
                    clauses[mc] += 1
            elif v == "refused":
                r = row.get("reason", "")
                if r in ("functions:none", "domains:other"):
                    clauses[r] += 1
    total = sum(verdicts.values())
    lines = [f"candidates: rows={total} candidate={verdicts['candidate']} report={verdicts['report']} refused={verdicts['refused']}"]
    lines += [f"verdict {v} {verdicts[v]}" for v in ("candidate", "report", "refused")]
    lines += [f"clause {c} {clauses[c]}" for c in ("domain", "core-function", "functions:none", "domains:other")]
    lines += [f"licence_class {c} {classes[c]}" for c in ("permissive", "weak", "strong", "other")]
    lines += [f"candidate_domain {d} {domains[d]}" for d in DOMAINS]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="decide adoption candidates from census and classification")
    run_cmd.add_argument("--census", type=Path, default=Path("census.tsv"))
    run_cmd.add_argument("--classified", type=Path, default=Path("classified.tsv"))
    run_cmd.add_argument("--checkouts", type=Path, required=True, help="folder of <owner>__<name> checkouts")
    run_cmd.add_argument("--out", type=Path, default=Path("candidates.tsv"))
    run_cmd.add_argument("--aggregator", action="append", default=[], help="aggregator sources (repeatable)")
    run_cmd.add_argument("--cache", type=Path, default=None, help="upstream licence cache JSON file")
    run_cmd.add_argument("--limit", type=int, default=None)

    sum_cmd = commands.add_parser("summary", help="count verdicts, licence classes, and candidate domains")
    sum_cmd.add_argument("--candidates", type=Path, default=None)
    sum_cmd.add_argument("--out", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "summary":
        path = args.candidates or args.out or Path("candidates.tsv")
        print(summary(path))
        return 0
    reader = GitHubReader()
    run(
        census_path=args.census,
        classified_path=args.classified,
        checkouts=args.checkouts,
        out_path=args.out,
        aggregators=args.aggregator,
        reader=reader,
        cache_path=args.cache,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
