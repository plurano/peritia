#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline self-tests for candidate filtering and licence resolution; no test calls a live network."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import candidates

CENSUS_FIELDS = [
    "source", "pin", "path", "skill_name", "blob_id", "size", "folder_file_count",
    "scripts_present", "folder_license", "repository_license", "ineligible",
    "dup_of_blob", "dup_of_name", "failure_reason",
]
CLASSIFIED_FIELDS = [
    "blob_id", "source", "path", "skill_name", "functions", "domains", "model", "status",
]


class FakeGitHubReader:
    """In-memory boundary for gh api repos/<owner>/<name>; no test makes network calls."""

    def __init__(self, records: dict[str, str | Exception] | None = None) -> None:
        self.records = records or {}
        self.calls: list[str] = []

    def repository_license(self, repo: str) -> str:
        self.calls.append(repo)
        if repo not in self.records:
            raise RuntimeError(f"HTTP 404: Not Found: {repo}")
        res = self.records[repo]
        if isinstance(res, Exception):
            raise res
        return res


def write_census(folder: Path, rows: list[dict[str, str]]) -> Path:
    path = folder / "census.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CENSUS_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for i, row in enumerate(rows):
            if i >= candidates.MAX_ROWS:
                break
            writer.writerow({f: row.get(f, "") for f in CENSUS_FIELDS})
    return path


def write_classified(folder: Path, rows: list[dict[str, str]]) -> Path:
    path = folder / "classified.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CLASSIFIED_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for i, row in enumerate(rows):
            if i >= candidates.MAX_ROWS:
                break
            writer.writerow({f: row.get(f, "") for f in CLASSIFIED_FIELDS})
    return path


def write_skill(
    checkouts: Path,
    source: str,
    path: str,
    text: str = "---\nname: x\n---\nbody\n",
    folder_license_text: str | None = None,
) -> Path:
    target = checkouts / source.replace("/", "__") / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if folder_license_text is not None:
        (target.parent / "LICENSE").write_text(folder_license_text, encoding="utf-8")
    return target


class CandidatesTests(unittest.TestCase):
    def test_licence_path_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "a/repo", "skills/s1/SKILL.md", "---\nname: s1\n---\n", "MIT License\n")
            census_p = write_census(folder, [{"source": "a/repo", "path": "skills/s1/SKILL.md", "skill_name": "s1", "blob_id": "b1"}])
            class_p = write_classified(folder, [{"blob_id": "b1", "source": "a/repo", "path": "skills/s1/SKILL.md", "skill_name": "s1", "functions": "coding", "domains": "go-service-framework", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            self.assertEqual(len(res), 1)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict, row.reason),
                             ("MIT", "permissive", "folder", "candidate", ""))

    def test_licence_path_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s2\nlicense: Apache-2.0\n---\nbody\n"
            write_skill(checkouts, "a/repo", "skills/s2/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "a/repo", "path": "skills/s2/SKILL.md", "skill_name": "s2", "blob_id": "b2"}])
            class_p = write_classified(folder, [{"blob_id": "b2", "source": "a/repo", "path": "skills/s2/SKILL.md", "skill_name": "s2", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("Apache-2.0", "permissive", "frontmatter", "candidate"))

    def test_licence_path_upstream_aggregator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s3\nupstream: https://github.com/orig/lib\n---\nbody\n"
            write_skill(checkouts, "agg/hub", "skills/s3/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s3/SKILL.md", "skill_name": "s3", "blob_id": "b3", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b3", "source": "agg/hub", "path": "skills/s3/SKILL.md", "skill_name": "s3", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader({"orig/lib": "BSD-3-Clause"})
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("BSD-3-Clause", "permissive", "upstream:orig/lib", "candidate"))
            self.assertEqual(reader.calls, ["orig/lib"])

    def test_licence_path_repository_non_aggregator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s4/SKILL.md", "---\nname: s4\n---\nbody\n")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s4/SKILL.md", "skill_name": "s4", "blob_id": "b4", "repository_license": "0BSD"}])
            class_p = write_classified(folder, [{"blob_id": "b4", "source": "solo/repo", "path": "skills/s4/SKILL.md", "skill_name": "s4", "functions": "testing", "domains": "reverse-engineering", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("0BSD", "permissive", "repository", "candidate"))

    def test_licence_path_weak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "a/repo", "skills/s5/SKILL.md", "---\nname: s5\n---\n", "Mozilla Public License Version 2.0\n")
            census_p = write_census(folder, [{"source": "a/repo", "path": "skills/s5/SKILL.md", "skill_name": "s5", "blob_id": "b5"}])
            class_p = write_classified(folder, [{"blob_id": "b5", "source": "a/repo", "path": "skills/s5/SKILL.md", "skill_name": "s5", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict), ("MPL-2.0", "weak", "candidate"))

    def test_aggregator_row_without_trace_field_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "agg/hub", "skills/s6/SKILL.md", "---\nname: s6\n---\nbody\n")
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s6/SKILL.md", "skill_name": "s6", "blob_id": "b6", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b6", "source": "agg/hub", "path": "skills/s6/SKILL.md", "skill_name": "s6", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader()
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict), ("untraced", "other", "refused"))
            self.assertIn("untraced", row.reason)
            self.assertEqual(len(reader.calls), 0)

    def test_planted_fake_upstream_repository_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s7\nupstream: https://github.com/missing/ghost-repo\n---\n"
            write_skill(checkouts, "agg/hub", "skills/s7/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s7/SKILL.md", "skill_name": "s7", "blob_id": "b7"}])
            class_p = write_classified(folder, [{"blob_id": "b7", "source": "agg/hub", "path": "skills/s7/SKILL.md", "skill_name": "s7", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader()
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("untraced", "other", "upstream:missing/ghost-repo", "refused"))
            self.assertIn("untraced", row.reason)
            self.assertIn("missing/ghost-repo", row.reason)

    def test_planted_unlicensed_entry_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "plain/repo", "skills/s8/SKILL.md", "---\nname: s8\n---\nbody\n")
            census_p = write_census(folder, [{"source": "plain/repo", "path": "skills/s8/SKILL.md", "skill_name": "s8", "blob_id": "b8", "repository_license": "none"}])
            class_p = write_classified(folder, [{"blob_id": "b8", "source": "plain/repo", "path": "skills/s8/SKILL.md", "skill_name": "s8", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("none", "other", "repository", "refused"))
            self.assertIn("none", row.reason)
            self.assertIn("repository", row.reason)

    def test_strong_copyleft_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "gpl/repo", "skills/s9/SKILL.md", "---\nname: s9\n---\n", "GNU General Public License Version 3\n")
            census_p = write_census(folder, [{"source": "gpl/repo", "path": "skills/s9/SKILL.md", "skill_name": "s9", "blob_id": "b9"}])
            class_p = write_classified(folder, [{"blob_id": "b9", "source": "gpl/repo", "path": "skills/s9/SKILL.md", "skill_name": "s9", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict, row.reason),
                             ("GPL-3.0", "strong", "report", "licence:GPL-3.0 strong copyleft: a person decides"))

    def test_path_leaving_checkout_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "a/repo", "skills/s10/SKILL.md")
            (folder / "secret.md").write_text("secret", encoding="utf-8")
            census_p = write_census(folder, [{"source": "a/repo", "path": "../secret.md", "skill_name": "esc", "blob_id": "b10"}])
            class_p = write_classified(folder, [{"blob_id": "b10", "source": "a/repo", "path": "../secret.md", "skill_name": "esc", "functions": "coding", "domains": "other", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual(row.verdict, "refused")
            self.assertIn("leaves checkout", row.reason)

    def test_cache_hit_makes_no_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm1 = "---\nname: s11\nupstream: https://github.com/shared/repo\n---\n"
            fm2 = "---\nname: s12\nupstream: https://github.com/shared/repo\n---\n"
            write_skill(checkouts, "agg/hub", "skills/s11/SKILL.md", fm1)
            write_skill(checkouts, "agg/hub", "skills/s12/SKILL.md", fm2)
            census_p = write_census(folder, [
                {"source": "agg/hub", "path": "skills/s11/SKILL.md", "skill_name": "s11", "blob_id": "b11"},
                {"source": "agg/hub", "path": "skills/s12/SKILL.md", "skill_name": "s12", "blob_id": "b12"},
            ])
            class_p = write_classified(folder, [
                {"blob_id": "b11", "source": "agg/hub", "path": "skills/s11/SKILL.md", "skill_name": "s11", "functions": "coding", "domains": "governance-devtooling", "status": "ok"},
                {"blob_id": "b12", "source": "agg/hub", "path": "skills/s12/SKILL.md", "skill_name": "s12", "functions": "coding", "domains": "governance-devtooling", "status": "ok"},
            ])
            cache_file = folder / "cache.json"
            reader = FakeGitHubReader({"shared/repo": "MIT"})
            res = candidates.run(census_p, class_p, checkouts, folder / "out.tsv", ["agg/hub"], reader, cache_path=cache_file)
            self.assertEqual(len(res), 2)
            self.assertEqual(reader.calls, ["shared/repo"])
            self.assertTrue(cache_file.is_file())
            cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(cache_data, {"shared/repo": "MIT"})

    def test_call_bound_enforced(self) -> None:
        reader = FakeGitHubReader({"r1": "MIT", "r2": "Apache-2.0", "r3": "ISC"})
        tracer = candidates.UpstreamTracer(reader, max_calls=2)
        self.assertEqual(tracer.trace("r1"), "MIT")
        self.assertEqual(tracer.trace("r2"), "Apache-2.0")
        self.assertEqual(tracer.trace("r3"), "untraced")
        self.assertEqual(reader.calls, ["r1", "r2"])

    def test_candidate_filter_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "a/repo", "skills/s/SKILL.md", folder_license_text="MIT License\n")
            cases = [
                ("err", {"status": "error", "functions": "coding", "domains": "windows-platform"}, {}, "status:error"),
                ("none_f", {"status": "ok", "functions": "none", "domains": "windows-platform"}, {}, "functions:none"),
                ("oth_d", {"status": "ok", "functions": "design-frontend", "domains": "other"}, {}, "domains:other"),
                ("inel", {"status": "ok", "functions": "coding", "domains": "windows-platform"}, {"ineligible": "licence:none"}, "ineligible:licence:none"),
            ]
            for i, (name, class_overrides, census_overrides, want_reason) in enumerate(cases):
                census_d = {"source": "a/repo", "path": "skills/s/SKILL.md", "skill_name": name, "blob_id": f"b_{name}"}
                census_d.update(census_overrides)
                class_d = {"blob_id": f"b_{name}", "source": "a/repo", "path": "skills/s/SKILL.md", "skill_name": name}
                class_d.update(class_overrides)
                census_p = write_census(folder, [census_d])
                class_p = write_classified(folder, [class_d])
                out = folder / f"out_{i}.tsv"
                res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
                self.assertEqual(res[0].verdict, "refused")
                self.assertEqual(res[0].reason, want_reason)

    def test_summary_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "candidates.tsv"
            rows = [
                candidates.CandidateRow("1", "a/b", "p1", "s1", "coding", "governance-devtooling", "domain", "MIT", "permissive", "folder", "candidate", ""),
                candidates.CandidateRow("2", "a/b", "p2", "s2", "coding", "go-service-framework", "domain", "GPL-3.0", "strong", "folder", "report", ""),
                candidates.CandidateRow("3", "a/b", "p3", "s3", "none", "other", "", "none", "other", "repository", "refused", "licence:none evidence:repository"),
            ]
            candidates.write_tsv(rows, out)
            rep = candidates.summary(out)
            self.assertIn("rows=3 candidate=1 report=1 refused=1", rep)
            self.assertIn("verdict candidate 1", rep)
            self.assertIn("verdict report 1", rep)
            self.assertIn("verdict refused 1", rep)
            self.assertIn("clause domain 1", rep)
            self.assertIn("clause core-function 0", rep)
            self.assertIn("clause functions:none 0", rep)
            self.assertIn("clause domains:other 0", rep)
            self.assertIn("licence_class permissive 1", rep)
            self.assertIn("licence_class strong 1", rep)
            self.assertIn("candidate_domain governance-devtooling 1", rep)
            self.assertIn("candidate_domain go-service-framework 0", rep)

    def test_identical_inputs_give_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "a/repo", "skills/s1/SKILL.md", folder_license_text="MIT License\n")
            census_p = write_census(folder, [{"source": "a/repo", "path": "skills/s1/SKILL.md", "skill_name": "s1", "blob_id": "b1"}])
            class_p = write_classified(folder, [{"blob_id": "b1", "source": "a/repo", "path": "skills/s1/SKILL.md", "skill_name": "s1", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out1 = folder / "out1.tsv"
            out2 = folder / "out2.tsv"
            candidates.run(census_p, class_p, checkouts, out1, [], FakeGitHubReader())
            candidates.run(census_p, class_p, checkouts, out2, [], FakeGitHubReader())
            self.assertEqual(out1.read_bytes(), out2.read_bytes())

    # --- Round 2 tests ---

    def test_frontmatter_named_file_absent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_terms\nlicense: Complete terms in LICENSE.txt\n---\nbody\n"
            write_skill(checkouts, "solo/repo", "skills/s_terms/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_terms/SKILL.md", "skill_name": "s_terms", "blob_id": "b_terms", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_terms", "source": "solo/repo", "path": "skills/s_terms/SKILL.md", "skill_name": "s_terms", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual(row.verdict, "refused")
            self.assertNotEqual(row.licence, "MIT")
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence), ("unknown", "other", "frontmatter"))

    def test_frontmatter_named_file_present_decides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_terms_ok\nlicense: Complete terms in LICENSE.txt\n---\nbody\n"
            skill_p = write_skill(checkouts, "solo/repo", "skills/s_terms_ok/SKILL.md", fm_text)
            (skill_p.parent / "LICENSE.txt").write_text("MIT License\n", encoding="utf-8")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_terms_ok/SKILL.md", "skill_name": "s_terms_ok", "blob_id": "b_terms_ok", "repository_license": "GPL-3.0"}])
            class_p = write_classified(folder, [{"blob_id": "b_terms_ok", "source": "solo/repo", "path": "skills/s_terms_ok/SKILL.md", "skill_name": "s_terms_ok", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("MIT", "permissive", "folder", "candidate"))

    def test_census_folder_license_absent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s_absent/SKILL.md", "---\nname: s_absent\n---\nbody\n")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_absent/SKILL.md", "skill_name": "s_absent", "blob_id": "b_absent", "folder_license": "y", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_absent", "source": "solo/repo", "path": "skills/s_absent/SKILL.md", "skill_name": "s_absent", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("unreadable", "other", "folder-missing", "refused"))
            self.assertIn("checkout lacks the file", row.reason)

    def test_aggregator_source_repo_plain_traced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_plain\nsource_repo: upstream/cool-tool\n---\nbody\n"
            write_skill(checkouts, "agg/hub", "skills/s_plain/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s_plain/SKILL.md", "skill_name": "s_plain", "blob_id": "b_plain"}])
            class_p = write_classified(folder, [{"blob_id": "b_plain", "source": "agg/hub", "path": "skills/s_plain/SKILL.md", "skill_name": "s_plain", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader({"upstream/cool-tool": "Apache-2.0"})
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("Apache-2.0", "permissive", "upstream:upstream/cool-tool", "candidate"))
            self.assertEqual(reader.calls, ["upstream/cool-tool"])

    def test_aggregator_license_source_url_traced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_url\nlicense_source: https://github.com/upstream/other-tool/blob/main/LICENSE\n---\nbody\n"
            write_skill(checkouts, "agg/hub", "skills/s_url/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s_url/SKILL.md", "skill_name": "s_url", "blob_id": "b_url"}])
            class_p = write_classified(folder, [{"blob_id": "b_url", "source": "agg/hub", "path": "skills/s_url/SKILL.md", "skill_name": "s_url", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader({"upstream/other-tool": "BSD-2-Clause"})
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("BSD-2-Clause", "permissive", "upstream:upstream/other-tool", "candidate"))
            self.assertEqual(reader.calls, ["upstream/other-tool"])

    def test_aggregator_source_type_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_self\nsource_type: self\nsource_repo: ignored/repo\n---\nbody\n"
            write_skill(checkouts, "agg/hub", "skills/s_self/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s_self/SKILL.md", "skill_name": "s_self", "blob_id": "b_self", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_self", "source": "agg/hub", "path": "skills/s_self/SKILL.md", "skill_name": "s_self", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader()
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("MIT", "permissive", "repository:self", "candidate"))
            self.assertEqual(len(reader.calls), 0)

    def test_aggregator_self_reference_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_self_ref\nsource_repo: agg/hub\n---\nbody\n"
            write_skill(checkouts, "agg/hub", "skills/s_self_ref/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "agg/hub", "path": "skills/s_self_ref/SKILL.md", "skill_name": "s_self_ref", "blob_id": "b_self_ref"}])
            class_p = write_classified(folder, [{"blob_id": "b_self_ref", "source": "agg/hub", "path": "skills/s_self_ref/SKILL.md", "skill_name": "s_self_ref", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            reader = FakeGitHubReader()
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.licence_evidence, row.verdict),
                             ("untraced", "other", "none", "refused"))
            self.assertEqual(len(reader.calls), 0)

    def test_aggregator_frontmatter_traced_conflict_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            # Case 1: front matter MIT vs traced GPL-3.0
            fm1 = "---\nname: s_c1\nlicense: MIT\nsource_repo: upstream/gpl-repo\n---\n"
            write_skill(checkouts, "agg/hub", "skills/s_c1/SKILL.md", fm1)
            # Case 2: front matter GPL-3.0 vs traced MIT
            fm2 = "---\nname: s_c2\nlicense: GPL-3.0\nsource_repo: upstream/mit-repo\n---\n"
            write_skill(checkouts, "agg/hub", "skills/s_c2/SKILL.md", fm2)
            census_p = write_census(folder, [
                {"source": "agg/hub", "path": "skills/s_c1/SKILL.md", "skill_name": "s_c1", "blob_id": "b_c1"},
                {"source": "agg/hub", "path": "skills/s_c2/SKILL.md", "skill_name": "s_c2", "blob_id": "b_c2"},
            ])
            class_p = write_classified(folder, [
                {"blob_id": "b_c1", "source": "agg/hub", "path": "skills/s_c1/SKILL.md", "skill_name": "s_c1", "functions": "coding", "domains": "governance-devtooling", "status": "ok"},
                {"blob_id": "b_c2", "source": "agg/hub", "path": "skills/s_c2/SKILL.md", "skill_name": "s_c2", "functions": "coding", "domains": "governance-devtooling", "status": "ok"},
            ])
            out = folder / "out.tsv"
            reader = FakeGitHubReader({
                "upstream/gpl-repo": "GPL-3.0",
                "upstream/mit-repo": "MIT",
            })
            res = candidates.run(census_p, class_p, checkouts, out, ["agg/hub"], reader)
            self.assertEqual(len(res), 2)
            row1, row2 = res[0], res[1]

            self.assertEqual(row1.verdict, "report")
            self.assertEqual((row1.licence, row1.licence_class, row1.licence_evidence),
                             ("GPL-3.0", "strong", "upstream:upstream/gpl-repo"))
            self.assertIn("MIT", row1.reason)
            self.assertIn("GPL-3.0", row1.reason)

            self.assertEqual(row2.verdict, "report")
            self.assertEqual((row2.licence, row2.licence_class, row2.licence_evidence),
                             ("MIT", "permissive", "upstream:upstream/mit-repo"))
            self.assertIn("GPL-3.0", row2.reason)
            self.assertIn("MIT", row2.reason)

    def test_licence_mit_0_permissive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_mit0\nlicense: MIT-0\n---\nbody\n"
            write_skill(checkouts, "solo/repo", "skills/s_mit0/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_mit0/SKILL.md", "skill_name": "s_mit0", "blob_id": "b_mit0"}])
            class_p = write_classified(folder, [{"blob_id": "b_mit0", "source": "solo/repo", "path": "skills/s_mit0/SKILL.md", "skill_name": "s_mit0", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict),
                             ("MIT-0", "permissive", "candidate"))
            self.assertEqual(candidates.licence_class("MIT-0"), "permissive")

    def test_licence_bsd_3_clause_license_normalisation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = '---\nname: s_bsd\nlicense: "BSD-3-Clause license"\n---\nbody\n'
            write_skill(checkouts, "solo/repo", "skills/s_bsd/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_bsd/SKILL.md", "skill_name": "s_bsd", "blob_id": "b_bsd"}])
            class_p = write_classified(folder, [{"blob_id": "b_bsd", "source": "solo/repo", "path": "skills/s_bsd/SKILL.md", "skill_name": "s_bsd", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict),
                             ("BSD-3-Clause", "permissive", "candidate"))

    def test_licence_agpl_3_0_only_strong_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            fm_text = "---\nname: s_agpl\nlicense: AGPL-3.0-only\n---\nbody\n"
            write_skill(checkouts, "solo/repo", "skills/s_agpl/SKILL.md", fm_text)
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_agpl/SKILL.md", "skill_name": "s_agpl", "blob_id": "b_agpl"}])
            class_p = write_classified(folder, [{"blob_id": "b_agpl", "source": "solo/repo", "path": "skills/s_agpl/SKILL.md", "skill_name": "s_agpl", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            row = res[0]
            self.assertEqual((row.licence, row.licence_class, row.verdict, row.reason),
                             ("AGPL-3.0-only", "strong", "report", "licence:AGPL-3.0-only strong copyleft: a person decides"))
            self.assertEqual(candidates.licence_class("AGPL-3.0-only"), "strong")

    def test_core_functions_is_subset_of_classify_functions(self) -> None:
        from classify import FUNCTIONS
        self.assertTrue(candidates.CORE_FUNCTIONS.issubset(FUNCTIONS))
        expected_core = {
            "coding", "review-audit", "testing", "ci-release", "docs-writing",
            "research", "planning-fleet-mgmt", "security", "infra-ops",
        }
        self.assertEqual(candidates.CORE_FUNCTIONS, expected_core)

    def test_domain_other_with_coding_is_candidate_core_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s_core/SKILL.md", "---\nname: s_core\n---\nbody\n")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_core/SKILL.md", "skill_name": "s_core", "blob_id": "b_core", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_core", "source": "solo/repo", "path": "skills/s_core/SKILL.md", "skill_name": "s_core", "functions": "coding", "domains": "other", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            self.assertEqual(len(res), 1)
            row = res[0]
            self.assertEqual(row.verdict, "candidate")
            self.assertEqual(row.match_clause, "core-function")
            self.assertEqual(row.reason, "")

    def test_planted_marketing_skill_refused_functions_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s_mkt/SKILL.md", "---\nname: s_mkt\n---\nbody\n")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_mkt/SKILL.md", "skill_name": "s_mkt", "blob_id": "b_mkt", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_mkt", "source": "solo/repo", "path": "skills/s_mkt/SKILL.md", "skill_name": "s_mkt", "functions": "none", "domains": "other", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            self.assertEqual(len(res), 1)
            row = res[0]
            self.assertEqual(row.verdict, "refused")
            self.assertEqual(row.reason, "functions:none")
            self.assertEqual(row.match_clause, "")

    def test_domain_other_non_core_refused_domains_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s_nc/SKILL.md", "---\nname: s_nc\n---\nbody\n")
            for non_core in ("design-frontend", "media"):
                census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_nc/SKILL.md", "skill_name": f"s_{non_core}", "blob_id": f"b_{non_core}", "repository_license": "MIT"}])
                class_p = write_classified(folder, [{"blob_id": f"b_{non_core}", "source": "solo/repo", "path": "skills/s_nc/SKILL.md", "skill_name": f"s_{non_core}", "functions": non_core, "domains": "other", "status": "ok"}])
                out = folder / f"out_{non_core}.tsv"
                res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
                self.assertEqual(res[0].verdict, "refused")
                self.assertEqual(res[0].reason, "domains:other")
                self.assertEqual(res[0].match_clause, "")

    def test_fleet_domain_skill_gets_match_clause_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            checkouts = folder / "checkouts"
            write_skill(checkouts, "solo/repo", "skills/s_fl/SKILL.md", "---\nname: s_fl\n---\nbody\n")
            census_p = write_census(folder, [{"source": "solo/repo", "path": "skills/s_fl/SKILL.md", "skill_name": "s_fl", "blob_id": "b_fl", "repository_license": "MIT"}])
            class_p = write_classified(folder, [{"blob_id": "b_fl", "source": "solo/repo", "path": "skills/s_fl/SKILL.md", "skill_name": "s_fl", "functions": "coding", "domains": "governance-devtooling", "status": "ok"}])
            out = folder / "out.tsv"
            res = candidates.run(census_p, class_p, checkouts, out, [], FakeGitHubReader())
            self.assertEqual(len(res), 1)
            row = res[0]
            self.assertEqual(row.verdict, "candidate")
            self.assertEqual(row.match_clause, "domain")
            self.assertEqual(row.reason, "")

    def test_summary_clauses_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "candidates.tsv"
            rows = [
                candidates.CandidateRow("1", "a/b", "p1", "s1", "coding", "governance-devtooling", "domain", "MIT", "permissive", "folder", "candidate", ""),
                candidates.CandidateRow("2", "a/b", "p2", "s2", "coding", "other", "core-function", "MIT", "permissive", "folder", "candidate", ""),
                candidates.CandidateRow("3", "a/b", "p3", "s3", "none", "other", "", "MIT", "permissive", "folder", "refused", "functions:none"),
                candidates.CandidateRow("4", "a/b", "p4", "s4", "media", "other", "", "MIT", "permissive", "folder", "refused", "domains:other"),
            ]
            candidates.write_tsv(rows, out)
            rep = candidates.summary(out)
            self.assertIn("clause domain 1", rep)
            self.assertIn("clause core-function 1", rep)
            self.assertIn("clause functions:none 1", rep)
            self.assertIn("clause domains:other 1", rep)


if __name__ == "__main__":
    unittest.main()
