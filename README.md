<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# Peritia

Peritia is one library of expert skills for coding agents: methodology
(premise checks, verification before completion, root-cause analysis,
estimation, decision records, postmortems, peer review), multi-agent role
patterns, and domain experts. Each skill is adapted from an upstream source
and keeps that source's credit, licence and pinned commit.

## The name

*Peritia* is Latin for expertise: practical skill that comes from experience
(from *peritus*, experienced). It is pronounced PEH-ree-tee-ah.

## Layout

One directory per skill under `skills/`:

| File | Holds |
| --- | --- |
| `SKILL.md` | the skill, in the form agent clients read |
| `ORIGIN.md` | upstream repository, path, pinned commit, licence, and what this library changed |
| `LICENSE` | the upstream licence text, unchanged, when the skill derives from one |

A skill adapted from upstream lists its changes against the pinned upstream text
in `ORIGIN.md`. A skill written here says so there.

## Using it

Pin a release tag of this repository, the way you pin any upstream dependency,
and point your agent client's skill directory at the skills you want. A release
never changes after it is tagged; updates arrive as new tags.

To install one release for every coding client on a machine at once,
`tools/install_user_skills.py` clones the release at its tag, keeps its skills
in a store named after the tagged commit, and links each skill into the user
skill folder of every client it finds: Claude Code (`~/.claude/skills`), Codex
(`$CODEX_HOME/skills`), Antigravity (`~/.gemini/config/skills`), Gemini CLI
(`~/.gemini/skills`) and opencode (`~/.agents/skills`, which opencode reads
beside `~/.claude/skills`). It never replaces an existing entry of the same
name, and it prints a plan unless you pass `--apply`. On Windows, where a
symbolic link needs developer mode or administrator rights, a refused link
becomes a directory junction, and the output names the mode it used:

```sh
python3 tools/install_user_skills.py plan --tag v0.1.0
python3 tools/install_user_skills.py install --tag v0.1.0 --apply
python3 tools/install_user_skills.py verify
python3 tools/install_user_skills.py uninstall --apply
```

`verify` compares every stored file with the sha256 recorded at install and
every link with its target, and fails on any change. `uninstall` removes the
links and the store and reports how many Peritia paths remain (zero).

## Census

`python3 tools/census.py run --out census.tsv` creates a deterministic TSV
inventory of public `SKILL.md` files from the repositories in
`tools/census-sources.json`. It pins each source head, records only tree
metadata (never skill contents), and fails closed after writing any rows it
could read. This is census data only; it does not classify or adopt skills.

`python3 tools/classify.py run --census census.tsv --checkouts <folder> --out
classified.tsv --model <name>` labels each unique skill of a census on two axes,
what it does and which domain it serves, with a model served by a local Ollama.
`<folder>` holds one checkout per source, named `<owner>__<name>`, at the census
pin. The answer is constrained to the label lists by a JSON schema and checked
again; a skill whose answer fails twice is written with status `error`, never
guessed. Every row names its model, and an interrupted run resumes.
`python3 tools/classify.py summary --out classified.tsv` counts the labels and
names the domains no skill serves.

`python3 tools/candidates.py run --census census.tsv --classified classified.tsv
--checkouts <folder> --out candidates.tsv` decides, for every unique skill the
census classified, whether it is an adoption candidate and under which licence,
with the evidence for that licence. A skill matches when functions are not only
`none` and either domains are not only `other` or functions include a core
engineering class; `match_clause` records whether each candidate was admitted
by `domain` or `core-function` (empty for refused rows). It resolves licences
beside the skill, in front matter, through upstream GitHub repositories for
sources marked with `--aggregator`, or from the repository licence, and groups
them into permissive, weak, strong, and other classes. Permissive and weak
licences become candidates, strong copyleft or conflicting aggregator licences
are reported for review, and non-engineering functions (`none`), other domains
when no core function applies, unresolvable licences, or untraced aggregator
sources are refused with the rule recorded. It collapses extra copies of a
skill inside the same source repository (client mirrors, translations under
locale folders) to one canonical candidate, refusing every other copy with
reason `copy-of:<canonical path>`. `python3 tools/candidates.py summary
--candidates candidates.tsv` reports counts per verdict, per clause, refused
copies, per licence class, and per domain among candidates.

`python3 tools/overlap.py run --candidates candidates.tsv --checkouts <folder>
--out overlap.tsv --groups groups.tsv --judge-model <name>` finds, for every
adoption candidate, skills it may overlap among other candidates and skills
already in `skills/`. It ranks the k nearest neighbours by cosine similarity
using embeddings from a local model (defaulting to
`hf.co/unsloth/embeddinggemma-2-GGUF:Q8_0`) plus any item with the same
normalised name, and lets a local chat model judge per pair whether they serve
the same use. Embeddings are cached per blob id, and an interrupted run resumes
without asking previously judged pairs again; failed judgements are recorded as
unjudged, never guessed. Connected components over matching pairs are grouped in
`groups.tsv`, identifying overlap-free candidates and groups containing an
existing skill. `python3 tools/overlap.py summary --overlap overlap.tsv --groups
groups.tsv --candidates candidates.tsv` reports counts of candidates,
overlap-free candidates, groups, groups with an existing skill, and unjudged
pairs.

`python3 tools/judge_groups.py run --groups groups.tsv --candidates candidates.tsv
--checkouts <folder> --eval-tasks tools/eval-tasks.json --out verdicts.tsv
--trials-out trials.jsonl --model <name> --judge-model <name>` scores each
member skill in same-use overlap groups on concrete engineering tasks from
`tools/eval-tasks.json` across function classes using the eval-harness method.
Deterministic regular expression assertions (`must_match` and `must_not_match`)
decide pass/fail across k trials per task (measuring pass@3 and pass^3), while a
local chat model scores a 0-10 rubric to break rule-score ties. It decides
`keep-better` when one member's rule score exceeds every other's by at least 0.2
(decided by rule) or when the rule gap is under 0.2 and the judge score exceeds
others by at least 2.0 (decided by judge), `keep-both` with distinct use when
members win different tasks where the other fails, or `propose-merge` on ties.
Verdicts in `verdicts.tsv` record `decided_by` along with kept and dropped
skills. Existing skills under `skills/` are never dropped; when an existing skill
loses, the verdict records `replace-proposed` for human review. Completed trials
are saved in `trials.jsonl` so interrupted runs resume without re-running, and
`python3 tools/judge_groups.py summary --verdicts verdicts.tsv` reports counts
per verdict, kept and dropped skills, and models used.

`python3 -B tools/adopt_skills.py --candidates candidates.tsv --groups groups.tsv --census census.tsv --checkouts <folder> --root .` previews adoption in census order; add `--apply` to write accepted skills and upstream attribution in `REUSE.toml` and `CREDITS.md`, downloading missing licence texts into `LICENSES/` with `reuse`. `--report <path>` writes `adopt-report.tsv` there; without it no report file is written, and the repository root is refused as a report location. Grouped candidates wait for the group judge. Each skill comes from its census pin, with its licence text and an `ORIGIN.md`; collisions receive an owner suffix. Opening code fences without an info string in Markdown files get language `text` and record that in `ORIGIN.md`. Folders exceeding 200 files or 2 MiB, symlinks, unsafe paths, missing licence text, and failures of the skill checker, repository markdown gate, or HISS gate (staging the skill alone in a temporary root with `.standards.yaml` and `.standards-baseline.json` under `praetorctl baseline --verify --all-violations`) are refused individually; markdown and HISS refusals report the failing rule IDs and file. A missing `praetorctl` binary fails the run. Both modes print the same report and counts per primary function class; previews leave the repository and supplied checkout sparse patterns unchanged, fetching missing candidate blobs at their pin to evaluate every candidate.

## Credits

Every skill credits its upstream author and repository in its own `ORIGIN.md`.
The list of upstreams is in [CREDITS.md](CREDITS.md).

## Licence

Text written or adapted in this repository is licensed under the European
Union Public Licence 1.2 ([LICENSES/EUPL-1.2.txt](LICENSES/EUPL-1.2.txt)).
Upstream text keeps its own licence, recorded per file in `REUSE.toml` and
shipped beside the skill as `LICENSE`. The repository follows the
[REUSE](https://reuse.software) specification.

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]
[![Documentation Governance][praetor-docs-badge]][praetor-docs-runs]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Documentation**: `make docs-lint` enforces locked Markdown style and the
private scratch-link policy.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/plurano/peritia/blob/HEAD/AGENTS.md
[praetor-docs-badge]: https://github.com/plurano/peritia/actions/workflows/praetor-docs.yml/badge.svg
[praetor-docs-runs]: https://github.com/plurano/peritia/actions/workflows/praetor-docs.yml
<!-- praetor:readme-governance:end -->
