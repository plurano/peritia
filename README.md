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
name, and it prints a plan unless you pass `--apply`:

```sh
python3 tools/install_user_skills.py plan --tag v0.1.0
python3 tools/install_user_skills.py install --tag v0.1.0 --apply
python3 tools/install_user_skills.py verify
python3 tools/install_user_skills.py uninstall --apply
```

`verify` compares every stored file with the sha256 recorded at install and
every link with its target, and fails on any change. `uninstall` removes the
links and the store and reports how many Peritia paths remain (zero).

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
