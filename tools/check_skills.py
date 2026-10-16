#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check every skill directory under skills/: a SKILL.md whose front matter names the directory and says when to
use it; an ORIGIN.md with the upstream repository, path, a full 40-hex pinned commit, the licence and the changes
made here (or "own" for a skill written here); the upstream LICENSE beside any adapted skill; and no command that
installs packages or fetches code when the skill runs.
Usage: check_skills.py [ROOT] | check_skills.py selftest
"""
import re
import sys
import tempfile
from pathlib import Path

try:
    from install_user_skills import read_install_set
except ImportError:
    from tools.install_user_skills import read_install_set

MAX_SKILLS = 5000  # bounds the directories read (HISS-02)
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FIELD = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.+)$", re.M)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# run-time installs and remote code: a skill must use the tools the project already pins
FETCH = re.compile(r"\b(?:pip install|uv pip install|uvx |npx |npm install|pnpm add|yarn add|go install|cargo install|"
                   r"curl[^\n|]*\|\s*(?:ba)?sh|wget[^\n|]*\|\s*(?:ba)?sh)")


def fields(text):
    return {m.group("key"): m.group("value").strip() for m in FIELD.finditer(text)}


def skill_problems(skill):
    """Problems of one skill directory, each naming the file and the rule."""
    out = []
    md, origin = skill / "SKILL.md", skill / "ORIGIN.md"
    if not md.is_file():
        return [f"{skill.name}: SKILL.md missing"]
    text = md.read_text(encoding="utf-8")
    front = FRONT.match(text)
    meta = fields(front.group(1)) if front else {}
    if meta.get("name") != skill.name:
        out.append(f"{skill.name}/SKILL.md: front matter name must be {skill.name!r}")
    if not meta.get("description"):
        out.append(f"{skill.name}/SKILL.md: front matter needs a description")
    for n, line in enumerate(text.splitlines(), 1):
        if FETCH.search(line):
            out.append(f"{skill.name}/SKILL.md:{n}: installs or fetches code at run time")
    if not origin.is_file():
        return out + [f"{skill.name}: ORIGIN.md missing"]
    rec = fields(origin.read_text(encoding="utf-8"))
    if rec.get("upstream") == "own":
        return out
    for key in ("upstream", "path", "commit", "licence", "changes"):
        if not rec.get(key):
            out.append(f"{skill.name}/ORIGIN.md: needs '{key}:'")
    if rec.get("commit") and not COMMIT.match(rec["commit"]):
        out.append(f"{skill.name}/ORIGIN.md: commit must be the full 40-hex SHA")
    if not (skill / "LICENSE").is_file():
        out.append(f"{skill.name}: the upstream LICENSE must sit beside an adapted skill")
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


def check(root):
    skills = sorted(p for p in (root / "skills").iterdir() if p.is_dir())[:MAX_SKILLS] if (root / "skills").is_dir() else []
    problems = [p for s in skills for p in skill_problems(s)]
    problems += check_install_set(root)
    problems.extend(index_problems(root, skills))
    for p in problems:
        print(f"[FAIL] {p}")
    print(f"skills: {len(skills)} checked, {len(problems)} problems")
    return 1 if problems else 0


def write_index(root, rows):
    d = root / "skills"
    d.mkdir(parents=True, exist_ok=True)
    lines = [INDEX_HEADER]
    for r in sorted(rows, key=lambda x: x["skill"]):
        lines.append(f"{r['skill']}\t{r.get('source', '')}\t{r.get('functions', '')}\t{r.get('domains', '')}\t{r.get('overlap_group', '')}")
    (d / "INDEX.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write(root, name, skill_md, origin=None, licence=True):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if origin is not None:
        (d / "ORIGIN.md").write_text(origin, encoding="utf-8")
    if licence:
        (d / "LICENSE").write_text("MIT License\n", encoding="utf-8")


def selftest():
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
        cases = {
            "wrong-name": (good_md.format(n="other"), good_origin, True, "front matter name"),
            "no-origin": (good_md.format(n="no-origin"), None, True, "ORIGIN.md missing"),
            "short-pin": (good_md.format(n="short-pin"), good_origin.replace("a" * 40, "abc1234"), True, "full 40-hex"),
            "no-licence": (good_md.format(n="no-licence"), good_origin, False, "upstream LICENSE"),
            "installs": (good_md.format(n="installs") + "\n    uv pip install numpy==2.5.3\n", good_origin, True, "installs or fetches"),
        }
        for name, (md, origin, licence, want) in cases.items():
            planted = Path(tmp) / name
            write(planted, name, md, origin, licence)
            problems = skill_problems(planted / "skills" / name)
            assert any(want in p for p in problems), (name, problems)
    print(f"selftest ok: 2 clean skills pass, {len(cases)} planted defects refused, install-set validated")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["selftest"]:
        sys.exit(selftest())
    sys.exit(check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
