#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Install one Peritia release at user scope for every coding client on this machine, read it back, or remove it.

The release tree is cloned at its tag, its skills/ folder (with CREDITS.md and the licence texts) is copied into a
store named after the tagged commit, and each client's user skill folder gets one symbolic link per skill into that
store. A manifest beside the store records the sha256 of every file and every link, so `verify` refuses a changed,
added or missing file and a link that points elsewhere. Nothing outside the store and those links is written. An
existing entry of the same name in a client folder is never replaced: it is reported as a collision and skipped.

Usage:
  install_user_skills.py plan      [--tag TAG] [--repo URL] [--home DIR] [--clients all]
  install_user_skills.py install   [--tag TAG] [--repo URL] [--home DIR] [--clients all] [--apply]
  install_user_skills.py verify    [--home DIR]
  install_user_skills.py drift     [--home DIR] [--json]
  install_user_skills.py uninstall [--home DIR] [--apply]
  install_user_skills.py selftest
Without --apply, install and uninstall only print what they would do.

Drift exits 2 if any folder fails, otherwise 1 for differences or 0 for agreement.
With --json it prints one object with:
  folders: objects with path and status (scanned, absent, or error)
  summary: counts named drifting, shared, and folders (scanned folders)
  drift: objects with name, first_difference (relative path), and copies
  skipped: objects with name and affected copies for names without drift
  errors: list of "<folder>: <detail>" diagnostics, empty when all scans succeed
Each copy has a full SHA-256 digest, installed locations, and skipped_links
(sorted relative paths of dangling and out-of-tree links excluded from the digest).
Only completed folders contribute to the counts and differences. The text view
prints skipped link counts with the first path, and prints each error to stderr.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from itertools import islice
from pathlib import Path
from unittest.mock import patch

REPO = "https://github.com/plurano/peritia"
TAG = "v0.1.0"
STORE_PARENT = ".local/share/praetor-tools/skills"
MANIFEST = "peritia-user-skills.json"
INSTALL_SET_FILE = "install-set.txt"
INSTALL_LIMIT = 129  # Codex skill list limit (measured 2026-10-15)
GIT_TIMEOUT = 300  # seconds for one clone or ls-remote (HISS-02)
MAX_FILES = 20000  # bounds every tree walk (HISS-02)
MAX_DRIFT_DEPTH = 16  # directory levels below a skill root
MAX_DRIFT_ENTRIES = 25000  # entries per folder listing or across an entire skill tree
# Windows refuses a symbolic link without developer mode or admin rights; a directory junction needs neither.
# PERITIA_LINK_MODE=junction forces junctions (to test that path where symlinks work); the output names the mode.
LINK_MODE_ENV = "PERITIA_LINK_MODE"
# User skill folders, read from each installed client (binary strings and its own docs, 2026-10-15):
# Claude Code ~/.claude/skills; Codex $CODEX_HOME/skills (default ~/.codex/skills); Antigravity CLI
# ~/.gemini/config/skills; Gemini CLI ~/.gemini/skills; opencode auto-loads ~/.claude/skills and ~/.agents/skills
# (`opencode debug skill` lists both), so it gets the client-neutral ~/.agents/skills.
CLIENTS = (
    ("claude-code", "claude", ".claude/skills"),
    ("codex", "codex", "$CODEX_HOME/skills"),
    ("antigravity", "agy", ".gemini/config/skills"),
    ("gemini-cli", "gemini", ".gemini/skills"),
    ("opencode", "opencode", ".agents/skills"),
)
# Additional folders from the workstation installation survey (2026-11-24).
# The second field marks a directory whose children are separate skill sets.
DRIFT_EXTRA_FOLDERS = (
    (".copilot/skills", False),
    (".pi/agent/skills", False),
    (".claude/skills/synced", True),
)


def client_dirs(home, everyone):
    """{client: skill folder} for the clients installed on PATH (all of them with everyone)."""
    out, skipped = {}, {}
    for name, binary, rel in CLIENTS:
        if not everyone and shutil.which(binary) is None:
            skipped[name] = f"{binary} is not on PATH"
            continue
        if rel.startswith("$CODEX_HOME"):
            base = Path(os.environ.get("CODEX_HOME") or home / ".codex")
            out[name] = base / rel.split("/", 1)[1]
        else:
            out[name] = home / rel
    return out, skipped


def drift_entries(folder, remaining):
    """List at most the remaining entry budget, in path order."""
    with os.scandir(folder) as entries:
        paths = [Path(entry.path) for entry in islice(entries, remaining + 1)]
    if len(paths) > remaining:
        raise ValueError(f"{folder}: exceeds MAX_DRIFT_ENTRIES={MAX_DRIFT_ENTRIES}")
    return sorted(paths)


def drift_is_dir(path):
    """Identify directories and tolerate missing entries and dangling links."""
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False


def drift_tree_path(root, path):
    """Resolve an entry within a skill and skip dangling links and outside targets."""
    target = link_target(path)
    if target is None:
        return path
    target = path.parent / target
    if not Path(os.path.abspath(target)).is_relative_to(root):
        return None
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def drift_skill_root(location):
    """Return the resolved root of a directory with a regular SKILL.md."""
    if not drift_is_dir(location):
        return None
    root = location.resolve(strict=True)
    marker = drift_tree_path(root, root / "SKILL.md")
    if marker is not None:
        try:
            if stat.S_ISREG(marker.stat().st_mode):
                return root
        except FileNotFoundError:
            return None
    return None


def drift_add_file(files, root, relative, target):
    """Record a file digest within the cap, naming the file if reading fails."""
    if len(files) == MAX_FILES:
        raise ValueError(f"{root}: exceeds MAX_FILES={MAX_FILES}")
    try:
        files[relative.as_posix()] = digest(target)
    except OSError as exc:
        raise OSError(f"cannot hash {target}: {exc}") from exc


def drift_tree_digest(root):
    """Return the tree digest, file inventory, and sorted skipped link paths."""
    pending = [(root, Path(), (root,))]
    remaining = MAX_DRIFT_ENTRIES
    files = {}
    skipped_links = []
    for _ in range(MAX_DRIFT_ENTRIES + 1):
        if not pending:
            break
        folder, prefix, ancestors = pending.pop()
        entries = drift_entries(folder, remaining)
        remaining -= len(entries)
        for entry in entries:
            relative = prefix / entry.name
            target = drift_tree_path(root, entry)
            if target is None:
                skipped_links.append(relative.as_posix())
                continue
            mode = target.stat().st_mode
            if stat.S_ISREG(mode):
                drift_add_file(files, root, relative, target)
            elif stat.S_ISDIR(mode) and target not in ancestors:
                if len(relative.parts) > MAX_DRIFT_DEPTH:
                    raise ValueError(f"{root}: {relative} exceeds MAX_DRIFT_DEPTH={MAX_DRIFT_DEPTH}")
                pending.append((target, relative, (*ancestors, target)))
    tree = hashlib.sha256()
    for relative, file_digest in sorted(files.items()):
        tree.update(os.fsencode(relative) + b"\0" + bytes.fromhex(file_digest))
    return tree.hexdigest(), files, sorted(skipped_links)


def drift_scan_folder(folder, locations, trees):
    """Record skill locations after a full folder scan, hashing each resolved root once."""
    found = []
    for location in drift_entries(folder, MAX_DRIFT_ENTRIES):
        root = drift_skill_root(location)
        if root is None:
            continue
        if root not in trees:
            trees[root] = drift_tree_digest(root)
        found.append((location, root))
    for location, root in found:
        locations.setdefault(location.name, []).append((location, root))


def drift_scan(home):
    """Scan configured roots and per-set folders, collecting every folder error."""
    clients, _ = client_dirs(home, everyone=True)
    roots = dict.fromkeys(clients.values(), False)
    roots.update((home / relative, expand) for relative, expand in DRIFT_EXTRA_FOLDERS)
    roots[clients["codex"] / ".system"] = False
    folders, locations, trees = {}, {}, {}
    errors = []
    pending = list(reversed(roots))
    for _ in range(len(roots) + MAX_DRIFT_ENTRIES):
        if not pending:
            break
        folder = pending.pop()
        if folder in folders:
            continue
        try:
            try:
                mode = folder.stat().st_mode
            except FileNotFoundError:
                if folder in roots:
                    folders[folder] = "absent"
                continue
            if not stat.S_ISDIR(mode):
                if folder in roots:
                    raise NotADirectoryError("expected a skill directory")
                continue
            if roots.get(folder):
                pending.extend(reversed(drift_entries(folder, MAX_DRIFT_ENTRIES)))
            else:
                drift_scan_folder(folder, locations, trees)
                folders[folder] = "scanned"
        except (OSError, RuntimeError, ValueError) as exc:
            # Path.resolve raises RuntimeError for symlink loops on Python 3.11.
            folders[folder] = "error"
            errors.append(f"{folder}: {exc}")
    return folders, locations, trees, errors


def drift_copies(locations, trees):
    """Group installed locations by resolved folder and retain every alias."""
    copies = {}
    for location, root in sorted(locations):
        fingerprint, _, skipped_links = trees[root]
        copy = copies.setdefault(root, {"digest": fingerprint, "locations": [], "skipped_links": skipped_links})
        copy["locations"].append(str(location))
    return list(copies.values())


def drift_difference(copies, inventories):
    """Find the first different path between the two most common copy digests."""
    counts = Counter(copy["digest"] for copy in copies)
    if len(counts) < 2:
        return None
    first, second = sorted(counts, key=lambda fingerprint: (-counts[fingerprint], fingerprint))[:2]
    left, right = inventories[first], inventories[second]
    return next(path for path in sorted(left.keys() | right.keys()) if left.get(path) != right.get(path))


def drift_report(home):
    """Build the report shared by the text and JSON views."""
    folders, locations, trees, errors = drift_scan(home)
    inventories = {fingerprint: files for fingerprint, files, _ in trees.values()}
    shared = 0
    differences = []
    skipped = []
    for name, paths in sorted(locations.items()):
        copies = drift_copies(paths, trees)
        difference = None
        if len(copies) >= 2:
            shared += 1
            difference = drift_difference(copies, inventories)
        if difference is not None:
            differences.append({"name": name, "copies": copies, "first_difference": difference})
            continue
        affected = [copy for copy in copies if copy["skipped_links"]]
        if affected:
            skipped.append({"name": name, "copies": affected})
    return {"folders": [{"path": str(path), "status": status} for path, status in folders.items()],
            "summary": {"drifting": len(differences), "shared": shared,
                        "folders": sum(status == "scanned" for status in folders.values())},
            "drift": differences, "skipped": skipped, "errors": errors}


def drift_print_copies(row):
    """Print locations, skipped link details, and any first difference for one name."""
    print(f"{row['name']}:")
    for copy in row["copies"]:
        for location in copy["locations"]:
            print(f"  {copy['digest'][:12]}  {location}")
        skipped_links = copy["skipped_links"]
        if skipped_links:
            print(f"  skipped links: {len(skipped_links)} ({skipped_links[0]})")
    if "first_difference" in row:
        print(f"  first difference: {row['first_difference']}")


def drift(home, as_json):
    """Print installed skill drift and return the scan status."""
    report = drift_report(home)
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for folder in report["folders"]:
            if folder["status"] != "error":
                print(f"{folder['status']}: {folder['path']}")
        for difference in report["drift"]:
            drift_print_copies(difference)
        for skipped in report["skipped"]:
            drift_print_copies(skipped)
        summary = report["summary"]
        print(f"drift: {summary['drifting']} of {summary['shared']} shared skill names differ across "
              f"{summary['folders']} folders")
        for error in report["errors"]:
            print(f"drift error: {error}", file=sys.stderr)
    if report["errors"]:
        return 2
    return int(bool(report["drift"]))


def git(args, cwd=None):
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if run.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {run.stderr.strip()[:300]}")
    return run.stdout.strip()


def fetch(repo, tag, into):
    """Clone repo at tag into `into`; refuse when the clone's head is not the commit the tag names remotely."""
    git(["-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", tag, repo, str(into)])
    head = git(["rev-parse", "HEAD"], cwd=into)
    remote = git(["ls-remote", repo, f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"])
    peeled = [line.split()[0] for line in remote.splitlines() if line.endswith("^{}")]
    named = peeled[0] if peeled else (remote.split()[0] if remote else "")
    if named != head:
        raise SystemExit(f"refused: tag {tag} names {named or 'nothing'} remotely, the clone is at {head}")
    return head


def release_files(tree):
    """Relative paths the store keeps: the skills folder, the credits and the licence texts."""
    picked = []
    for top in ("skills", "LICENSES"):
        root = tree / top
        walked = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
        if len(walked) > MAX_FILES:
            raise SystemExit(f"refused: {top} holds more than {MAX_FILES} files")
        picked += [p.relative_to(tree).as_posix() for p in walked]
    return picked + [name for name in ("CREDITS.md", "LICENSE", INSTALL_SET_FILE) if (tree / name).is_file()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_install_set(tree):
    """Return ordered skill names from install-set.txt.

    Refuse duplicate names, path separators, names missing skills/<name>/SKILL.md,
    and lists longer than INSTALL_LIMIT.
    """
    root = tree if tree.is_dir() else tree.parent
    path = root / INSTALL_SET_FILE if tree.is_dir() else tree
    skills_dir = root / "skills"
    if not path.is_file():
        raise ValueError(f"install set missing: {path}")
    names = []
    seen = set()
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        clean = raw.split("#", 1)[0].strip()
        if not clean:
            continue
        if "/" in clean or "\\" in clean or (os.sep in clean and os.sep != "/"):
            raise ValueError(f"refused: path separator in skill name {clean!r} at line {n}")
        if clean in seen:
            raise ValueError(f"refused: duplicate skill {clean!r} at line {n}")
        skill_md = skills_dir / clean / "SKILL.md"
        if not skill_md.is_file():
            raise ValueError(f"refused: unknown skill {clean!r} (missing skills/{clean}/SKILL.md)")
        names.append(clean)
        seen.add(clean)
    if len(names) > INSTALL_LIMIT:
        raise ValueError(f"refused: install set holds {len(names)} skills, exceeding limit of {INSTALL_LIMIT}")
    return names


def skill_names(tree):
    """Return ordered skill names: install-set.txt if present in release tree, else all skills."""
    if (tree / INSTALL_SET_FILE).is_file():
        try:
            return read_install_set(tree)
        except ValueError as exc:
            raise SystemExit(str(exc))
    skills_dir = tree / "skills"
    return sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()) if skills_dir.is_dir() else []


def skill_source_desc(tree, names):
    """Return source description string for plan/install/verify."""
    skills_dir = tree / "skills"
    all_count = len([p for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()]) if skills_dir.is_dir() else 0
    if (tree / INSTALL_SET_FILE).is_file():
        return f"install set: {len(names)} of {all_count} skills"
    return f"no install set: all {all_count} skills"


def is_junction(path):
    return bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def link_target(link):
    """The folder a symlink or junction at link points to, or None when link is neither."""
    if not (link.is_symlink() or is_junction(link)):
        return None
    raw = os.readlink(link)
    return Path(raw[4:] if raw.startswith("\\\\?\\") else raw)


def make_link(link, target):
    """Link link to target; returns the mode used. A refused symlink on Windows becomes a junction, named."""
    forced = os.environ.get(LINK_MODE_ENV, "")
    if forced == "junction" and os.name != "nt":
        raise SystemExit("refused: junctions exist only on Windows; unset " + LINK_MODE_ENV)
    if forced != "junction":
        try:
            link.symlink_to(target, target_is_directory=True)
            return "symlink"
        except OSError as exc:
            if os.name != "nt":
                raise
            why = f"symlink refused: {exc.strerror or exc}"
    else:
        why = f"forced by {LINK_MODE_ENV}"
    run = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True,
                         timeout=GIT_TIMEOUT)
    if run.returncode != 0:
        raise SystemExit(f"refused: junction {link} failed: {run.stdout.strip()} {run.stderr.strip()}"[:400])
    return f"junction ({why})"


def remove_link(link):
    if is_junction(link):
        os.rmdir(link)
    else:
        link.unlink()


def plan_links(names, dirs, store):
    """[(client, link path, target)] for every skill and client, plus collisions that are never replaced."""
    links, collisions = [], []
    for client, folder in sorted(dirs.items()):
        for name in names:
            link, target = folder / name, store / "skills" / name
            if link_target(link) == target:
                links.append((client, link, target))
            elif link.exists() or link.is_symlink() or is_junction(link):
                collisions.append(f"{client}: {link} exists and is not this release's link")
            else:
                links.append((client, link, target))
    return links, collisions


def write_store(tree, store, files):
    """Copy the release files into the store and make them read-only; returns {relative path: sha256}."""
    sums = {}
    for rel in files:
        dest = store / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree / rel, dest)
        dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        sums[rel] = digest(dest)
    return sums


def install(args, home, apply):
    dirs, skipped = client_dirs(home, args.get("--clients") == "all")
    tag, repo = args.get("--tag", TAG), args.get("--repo", REPO)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "release"
        commit = fetch(repo, tag, tree)
        store = home / STORE_PARENT / f"peritia@{commit}"
        names = skill_names(tree)
        source = skill_source_desc(tree, names)
        files = release_files(tree)
        links, collisions = plan_links(names, dirs, store)
        report(tag, commit, store, dirs, skipped, links, collisions, apply, source)
        if not apply:
            return 0
        if (home / STORE_PARENT / MANIFEST).exists():
            raise SystemExit("refused: a Peritia user install exists; run uninstall --apply first")
        sums = write_store(tree, store, files)
    modes = set()
    for _client, link, target in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link_target(link) is None:
            modes.add(make_link(link, target))
    print("link mode: " + (", ".join(sorted(modes)) or "links already present"))
    manifest = {"repo": repo, "tag": tag, "commit": commit, "store": str(store),
                "source": source, "files": sums,
                "links": [{"client": c, "link": str(l), "target": str(t)} for c, l, t in links],
                "skipped_clients": skipped, "collisions": collisions}
    (home / STORE_PARENT / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return verify(home)


def report(tag, commit, store, dirs, skipped, links, collisions, apply, source):
    print(f"{'install' if apply else 'plan (dry run)'}: Peritia {tag} at {commit} ({source}) -> {store}")
    for client, folder in sorted(dirs.items()):
        print(f"  {client}: {sum(1 for c, _l, _t in links if c == client)} links in {folder}")
    for client, why in sorted(skipped.items()):
        print(f"  {client}: skipped ({why})")
    for line in collisions:
        print(f"  collision, left as it is: {line}")


def load_manifest(home):
    path = home / STORE_PARENT / MANIFEST
    if not path.is_file():
        raise SystemExit(f"no Peritia user install: {path} is absent")
    return json.loads(path.read_text(encoding="utf-8"))


def store_problems(manifest):
    store, sums = Path(manifest["store"]), manifest["files"]
    found = sorted(p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()) if store.is_dir() else []
    if len(found) > MAX_FILES:
        return [f"store holds more than {MAX_FILES} files"]
    bad = [f"missing: {rel}" for rel in sorted(set(sums) - set(found))]
    bad += [f"not in the release: {rel}" for rel in found if rel not in sums]
    bad += [f"changed: {rel}" for rel in found if rel in sums and digest(store / rel) != sums[rel]]
    return bad


def link_problems(manifest):
    bad = []
    for row in manifest["links"]:
        link = Path(row["link"])
        target = link_target(link)
        if target is None:
            bad.append(f"link missing or replaced: {link}")
        elif target != Path(row["target"]):
            bad.append(f"link points elsewhere: {link} -> {target}")
    return bad


def verify(home):
    manifest = load_manifest(home)
    bad = store_problems(manifest) + link_problems(manifest)
    for line in bad:
        print(f"verify: {line}")
    source = manifest.get("source")
    if not source:
        store = Path(manifest["store"])
        all_s = [p.name for p in (store / "skills").iterdir() if (p / "SKILL.md").is_file()] if (store / "skills").is_dir() else []
        if (store / INSTALL_SET_FILE).is_file():
            try:
                iset = read_install_set(store)
                source = f"install set: {len(iset)} of {len(all_s)} skills"
            except Exception:
                source = f"install set: unknown of {len(all_s)} skills"
        else:
            source = f"no install set: all {len(all_s)} skills"
    clients = sorted({row["client"] for row in manifest["links"]})
    print(f"verify: Peritia {manifest['tag']} {manifest['commit'][:12]} ({source}): {len(manifest['files'])} files, "
          f"{len(manifest['links'])} links for {', '.join(clients) or 'no client'}: {'FAIL' if bad else 'ok'}")
    return 1 if bad else 0


def uninstall(home, apply):
    manifest = load_manifest(home)
    store = Path(manifest["store"])
    ours = [Path(r["link"]) for r in manifest["links"] if link_target(Path(r["link"])) == Path(r["target"])]
    print(f"{'uninstall' if apply else 'uninstall plan (dry run)'}: {len(ours)} links, store {store}")
    if not apply:
        return 0
    for link in ours:
        remove_link(link)
    for path in sorted(store.rglob("*"), reverse=True)[:MAX_FILES]:
        path.chmod(stat.S_IRWXU) if path.is_dir() else path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(store, ignore_errors=False)
    (home / STORE_PARENT / MANIFEST).unlink()
    left = leftovers(home, manifest)
    print(f"uninstall: {len(left)} Peritia files or links left")
    return 1 if left else 0


def leftovers(home, manifest):
    """Every path of this install that still exists: store, manifest, and links into the store."""
    store = Path(manifest["store"])
    left = [str(p) for p in (store, home / STORE_PARENT / MANIFEST) if p.exists() or p.is_symlink()]
    for row in manifest["links"]:
        target = link_target(Path(row["link"]))
        if target is not None and str(target).startswith(str(store)):
            left.append(row["link"])
    return left


def fixture_repo(root, dirname="src", skills=("alpha", "beta"), install_set=None):
    """A local repository with given skills and optional install-set.txt, tagged v9.9.9."""
    src = root / dirname
    for name in skills:
        (src / "skills" / name).mkdir(parents=True)
        (src / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")
    (src / "CREDITS.md").write_text("# Credits\n", encoding="utf-8")
    if install_set is not None:
        (src / INSTALL_SET_FILE).write_text(install_set, encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "tag", "-a", "v9.9.9", "-m", "v9.9.9"]):
        subprocess.run(["git", *args], cwd=src, env=env, check=True, capture_output=True, timeout=GIT_TIMEOUT)
    return src.as_uri()


def quiet(fn, *args):
    """Run a selftest operation without printing its stdout."""
    with redirect_stdout(StringIO()):
        return fn(*args)


def selftest_install_set_cases(root):
    five = ("s1", "s2", "s3", "s4", "s5")
    repo = fixture_repo(root, dirname="src_set", skills=five, install_set="# 3 curated\ns1\ns2\ns3\n")
    home = root / "home_set"
    os.environ["CODEX_HOME"] = str(home / ".codex")
    args = {"--tag": "v9.9.9", "--repo": repo, "--clients": "all"}
    cases = [("install set 3 of 5 installs ok", quiet(install, args, home, True) == 0)]
    manifest = load_manifest(home)
    cases.append(("install set source recorded", manifest.get("source") == "install set: 3 of 5 skills"))
    cases.append(("15 links: 3 skills x 5 clients", len(manifest["links"]) == 15))
    dirs, _ = client_dirs(home, True)
    read_back_ok = True
    for _client, folder in dirs.items():
        if sorted(p.name for p in folder.iterdir()) != ["s1", "s2", "s3"]:
            read_back_ok = False
        for s in ("s1", "s2", "s3"):
            if link_target(folder / s) != Path(manifest["store"]) / "skills" / s:
                read_back_ok = False
        if (folder / "s4").exists() or (folder / "s5").exists():
            read_back_ok = False
    cases.append(("read-back lists exactly 3 skills per client", read_back_ok))
    cases.append(("install set verify ok", quiet(verify, home) == 0))
    return cases


def selftest_reader_cases(root):
    neg_tree = root / "neg_tree"
    for name in ("s1", "s2"):
        (neg_tree / "skills" / name).mkdir(parents=True, exist_ok=True)
        (neg_tree / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (neg_tree / INSTALL_SET_FILE).write_text("s1\ns1\n", encoding="utf-8")
    cases = [("negative: duplicate refused", refused(lambda: read_install_set(neg_tree)))]
    (neg_tree / INSTALL_SET_FILE).write_text("s1\nunknown_skill\n", encoding="utf-8")
    cases.append(("negative: unknown name refused", refused(lambda: read_install_set(neg_tree))))
    (neg_tree / INSTALL_SET_FILE).write_text("s1\nsub/s2\n", encoding="utf-8")
    cases.append(("negative: path separator refused", refused(lambda: read_install_set(neg_tree))))
    bound_tree = root / "bound_tree"
    for i in range(130):
        (bound_tree / "skills" / f"sk{i}").mkdir(parents=True, exist_ok=True)
        (bound_tree / "skills" / f"sk{i}" / "SKILL.md").write_text(f"---\nname: sk{i}\n---\n", encoding="utf-8")
    (bound_tree / INSTALL_SET_FILE).write_text("\n".join(f"sk{i}" for i in range(130)) + "\n", encoding="utf-8")
    cases.append(("negative: 130 names refused", refused(lambda: read_install_set(bound_tree))))
    (bound_tree / INSTALL_SET_FILE).write_text("\n".join(f"sk{i}" for i in range(129)) + "\n", encoding="utf-8")
    cases.append(("boundary: 129 names accepted", len(read_install_set(bound_tree)) == 129))
    return cases


def selftest_cases(root):
    home, repo = root / "home", fixture_repo(root)
    os.environ["CODEX_HOME"] = str(home / ".codex")
    args = {"--tag": "v9.9.9", "--repo": repo, "--clients": "all"}
    cases = [("plan writes nothing", quiet(install, args, home, False) == 0 and not (home / STORE_PARENT).exists())]
    (home / ".claude/skills/beta").mkdir(parents=True)  # a skill of the same name from somewhere else
    cases.append(("install reads back ok", quiet(install, args, home, True) == 0))
    manifest = load_manifest(home)
    cases.append(("9 links: 2 skills x 5 clients minus 1 collision", len(manifest["links"]) == 9))
    cases.append(("boundary: no install-set.txt installs all", manifest.get("source") == "no install set: all 2 skills"))
    cases.append(("collision left as it is", (home / ".claude/skills/beta").is_dir()
                  and not (home / ".claude/skills/beta").is_symlink() and len(manifest["collisions"]) == 1))
    target = Path(manifest["store"]) / "skills/alpha/SKILL.md"
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    target.write_text("tampered\n", encoding="utf-8")
    cases.append(("tampered file refused", quiet(verify, home) == 1))
    target.write_text("---\nname: alpha\n---\n# alpha\n", encoding="utf-8")
    cases.append(("restored file passes", quiet(verify, home) == 0))
    extra = Path(manifest["store"]) / "skills/alpha/extra.md"
    extra.write_text("x\n", encoding="utf-8")
    cases.append(("added file refused", quiet(verify, home) == 1))
    extra.unlink()
    remove_link(Path(manifest["links"][0]["link"]))
    cases.append(("missing link refused", quiet(verify, home) == 1))
    make_link(Path(manifest["links"][0]["link"]), Path(manifest["links"][0]["target"]))
    cases.append(("second install refused", refused(lambda: quiet(install, args, home, True))))
    cases.append(("uninstall leaves zero Peritia files", quiet(uninstall, home, True) == 0
                  and not leftovers(home, manifest) and (home / ".claude/skills/beta").is_dir()))
    return cases + selftest_install_set_cases(root) + selftest_reader_cases(root)


def fixture_skill(home, relative, content):
    """Write a skill at a relative path under a fixture home."""
    skill = home / relative
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content, encoding="utf-8")
    return skill


def fixture_drift(home, codex_home=None):
    """Run the drift command against a fixture home and capture its JSON."""
    os.environ["CODEX_HOME"] = str(codex_home or home / ".codex")
    output = StringIO()
    with redirect_stdout(output):
        status = drift(home, as_json=True)
    return status, json.loads(output.getvalue())


def fixture_first_difference(report):
    """Read the first reported difference for a fixture with one drifting name."""
    rows = report.get("drift", [])
    return rows[0]["first_difference"] if len(rows) == 1 else None


def selftest_drift_content(root):
    """Check identical trees, changed bytes, and added or renamed paths."""
    home = root / "content"
    left = fixture_skill(home, ".claude/skills/example", "left\n")
    right = fixture_skill(home, ".agents/skills/example", "right\n")
    status, report = fixture_drift(home)
    cases = [("drift: different same-name copies exit 1",
              status == 1 and fixture_first_difference(report) == "SKILL.md")]
    (right / "SKILL.md").write_text("left\n", encoding="utf-8")
    status, report = fixture_drift(home)
    cases.append(("drift: identical copies exit 0",
                  status == 0 and report.get("summary") == {"drifting": 0, "shared": 1, "folders": 2}))
    for skill, content in ((left, "one\n"), (right, "two\n")):
        (skill / "notes.md").write_text(content, encoding="utf-8")
    status, report = fixture_drift(home)
    cases.append(("drift: content-only edit exits 1",
                  status == 1 and fixture_first_difference(report) == "notes.md"))
    (right / "notes.md").write_text("one\n", encoding="utf-8")
    (right / "extra.md").write_text("extra\n", encoding="utf-8")
    status, report = fixture_drift(home)
    cases.append(("drift: extra file exits 1 and names path",
                  status == 1 and fixture_first_difference(report) == "extra.md"))
    (right / "extra.md").unlink()
    (right / "docs").mkdir()
    (right / "notes.md").rename(right / "docs/notes.md")
    status, report = fixture_drift(home)
    cases.append(("drift: path-only rename exits 1 and names a changed path",
                  status == 1 and fixture_first_difference(report) in {"notes.md", "docs/notes.md"}))
    return cases


def selftest_drift_locations(root):
    """Check aliases, separately installed sets, and extra client folders."""
    home = root / "aliases"
    target = fixture_skill(home, "store/example", "same\n")
    for relative in (".claude/skills", ".pi/agent/skills"):
        folder = home / relative
        folder.mkdir(parents=True)
        make_link(folder / "example", target)
    status, report = fixture_drift(home)
    cases = [("drift: symlink aliases count as one copy",
              status == 0 and report.get("summary") == {"drifting": 0, "shared": 0, "folders": 2})]

    home = root / "extra-folders"
    synced = [fixture_skill(home, f".claude/skills/synced/{name}/example", name) for name in ("one", "two")]
    fixture_skill(home, ".copilot/skills/other", "copilot\n")
    fixture_skill(home, ".codex/skills/.system/other", "system\n")
    status, report = fixture_drift(home)
    scanned = {row["path"] for row in report.get("folders", []) if row["status"] == "scanned"}
    cases.append(("drift: each synced set scanned separately",
                  status == 1 and {str(path.parent) for path in synced} <= scanned
                  and any(row["name"] == "example" for row in report.get("drift", []))))
    cases.append(("drift: Copilot and Codex system folders scanned",
                  status == 1 and any(row["name"] == "other" for row in report.get("drift", []))))

    home = root / "custom-codex"
    system = fixture_skill(home, "custom-codex/skills/.system/other", "custom\n")
    copilot = fixture_skill(home, ".copilot/skills/other", "copilot\n")
    decoy = fixture_skill(home, ".codex/skills/.system/other", "decoy\n")
    status, report = fixture_drift(home, codex_home=home / "custom-codex")
    folders = {row["path"]: row["status"] for row in report.get("folders", [])}
    locations = {location for row in report.get("drift", []) for copy in row["copies"]
                 for location in copy["locations"]}
    cases.append(("drift: custom Codex system catalog used instead of default",
                  status == 1 and folders.get(str(system.parent)) == "scanned"
                  and str(decoy.parent) not in folders and locations == {str(system), str(copilot)}))
    return cases


def selftest_drift_comparison(root):
    """Check copy frequency and alias locations."""
    home = root / "comparison"
    for folder, variant in ((".claude/skills", "one"), (".agents/skills", "one"),
                            (".gemini/skills", "two"), (".copilot/skills", "two")):
        skill = fixture_skill(home, f"{folder}/example", "same\n")
        (skill / "second.md").write_text(variant, encoding="utf-8")
    rare = fixture_skill(home, ".pi/agent/skills/example", "same\n")
    (rare / "first.md").write_text("rare\n", encoding="utf-8")
    aliases = (home / ".codex/skills/example", home / ".gemini/config/skills/example")
    for alias in aliases:
        alias.parent.mkdir(parents=True)
        make_link(alias, rare)
    status, report = fixture_drift(home)
    cases = [("drift: first difference uses the two most common distinct copies",
              status == 1 and fixture_first_difference(report) == "second.md")]
    copies = [copy for row in report.get("drift", []) for copy in row["copies"]]
    alias_locations = {str(path) for path in (rare, *aliases)}
    cases.append(("drift: grouped copy retains all installed aliases",
                  any(set(copy["locations"]) == alias_locations for copy in copies)))
    return cases


def selftest_drift_links(root):
    """Check that skipped links are reported without changing comparison results."""
    home = root / "outside-link"
    skill = fixture_skill(home, ".claude/skills/example", "same\n")
    peer = fixture_skill(home, ".agents/skills/example", "same\n")
    outside = fixture_skill(home, "external", "outside\n")
    make_link(skill / "outside", outside)
    target = skill / "target"
    target.mkdir()
    make_link(skill / "dangling", target)
    target.rmdir()
    status, report = fixture_drift(home)
    cases = [("drift: outside and dangling links excluded from hash",
              status == 0 and report.get("summary") == {"drifting": 0, "shared": 1, "folders": 2})]
    skipped = [copy for row in report.get("skipped", []) for copy in row["copies"]]
    cases.append(("drift: skipped paths reported even when copies agree",
                  len(skipped) == 1 and skipped[0].get("locations") == [str(skill)]
                  and skipped[0].get("skipped_links") == ["dangling", "outside"]))
    (peer / "SKILL.md").write_text("different\n", encoding="utf-8")
    status, report = fixture_drift(home)
    skipped_by_location = {location: copy.get("skipped_links") for row in report.get("drift", [])
                           for copy in row["copies"] for location in copy["locations"]}
    cases.append(("drift: every drifting copy lists its skipped paths",
                  status == 1 and report.get("skipped") == []
                  and skipped_by_location == {str(skill): ["dangling", "outside"], str(peer): []}))
    return cases


def selftest_drift_errors(root):
    """Check absent folders, scan caps, and a file read failure."""
    home = root / "empty"
    status, report = fixture_drift(home)
    folders = report.get("folders", [])
    cases = [("drift: absent folders listed and exit 0", status == 0 and len(folders) == 9
              and all(row["status"] == "absent" for row in folders) and report.get("errors") == [])]

    home = root / "errors"
    skill = fixture_skill(home, ".claude/skills/example", "example\n")
    nested = skill / "nested"
    nested.mkdir()
    unreadable = nested.resolve() / "notes.md"
    unreadable.write_text("notes\n", encoding="utf-8")
    module = sys.modules[__name__]
    for cap, limit in (("MAX_DRIFT_ENTRIES", 2), ("MAX_DRIFT_DEPTH", 0), ("MAX_FILES", 1)):
        with patch.object(module, cap, limit, create=True):
            status, report = fixture_drift(home)
        errors = report.get("errors", [])
        cases.append((f"drift: {cap} exceeded exits 2 and names folder and cap",
                      status == 2 and len(errors) == 1 and errors[0].startswith(f"{skill.parent}: ")
                      and str(skill.resolve()) in errors[0] and f"{cap}={limit}" in errors[0]))

    read_bytes = Path.read_bytes

    def reject_file(path):
        """Simulate a read failure for one fixture file on any platform."""
        if path == unreadable:
            raise PermissionError(f"permission denied: {path}")
        return read_bytes(path)

    with patch.object(Path, "read_bytes", reject_file):
        status, report = fixture_drift(home)
    errors = report.get("errors", [])
    cases.append(("drift: unreadable file exits 2 and names path",
                  status == 2 and len(errors) == 1 and str(unreadable) in errors[0]
                  and "permission denied" in errors[0]))
    return cases


def selftest_drift_partial_scan(root):
    """Check that failed folders contribute errors and completed folders contribute drift."""
    home = root / "partial-scan"
    fixture_skill(home, ".claude/skills/example", "would leak\n")
    failed = [fixture_skill(home, f"{folder}/z-bad", "cap\n")
              for folder in (".claude/skills", ".gemini/config/skills")]
    for skill in failed:
        (skill / "extra.md").write_text("over the cap\n", encoding="utf-8")
    completed = [fixture_skill(home, f"{folder}/example", folder)
                 for folder in (".agents/skills", ".copilot/skills")]
    with patch.object(sys.modules[__name__], "MAX_FILES", 1):
        status, report = fixture_drift(home)
    folders = {row["path"]: row["status"] for row in report.get("folders", [])}
    errors = report.get("errors", [])
    locations = {location for row in report.get("drift", []) for copy in row["copies"]
                 for location in copy["locations"]}
    return [("drift: all folder errors reported and exit 2 takes precedence",
             status == 2 and len(errors) == len(failed)
             and all(folders.get(str(skill.parent)) == "error" for skill in failed)
             and all(error.startswith(f"{skill.parent}: ") and "MAX_FILES=1" in error
                     for skill, error in zip(failed, errors, strict=True))),
            ("drift: only completed folders contribute to drift after errors",
             fixture_first_difference(report) == "SKILL.md"
             and report.get("summary") == {"drifting": 1, "shared": 1, "folders": 2}
             and locations == {str(skill) for skill in completed})]


def refused(call):
    try:
        call()
    except (SystemExit, ValueError):
        return True
    return False


def selftest():
    """Run the installer and drift fixtures for the available link modes."""
    keep = {name: os.environ.get(name) for name in ("CODEX_HOME", LINK_MODE_ENV)}
    modes = ["symlink", "junction"] if os.name == "nt" else ["symlink"]
    cases = []
    try:
        for mode in modes:
            os.environ[LINK_MODE_ENV] = mode
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checked = (selftest_cases(root) + selftest_drift_content(root)
                           + selftest_drift_locations(root) + selftest_drift_comparison(root)
                           + selftest_drift_links(root) + selftest_drift_errors(root)
                           + selftest_drift_partial_scan(root))
                cases += [(f"{mode}: {name}", ok) for name, ok in checked]
        if os.name != "nt":
            os.environ[LINK_MODE_ENV] = "junction"
            cases.append(("junction mode refused off Windows", refused(lambda: make_link(Path("x"), Path("y")))))
    finally:
        for name, value in keep.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
    bad = [name for name, ok in cases if not ok]
    if bad:
        raise SystemExit(f"selftest failed: {bad}")
    print(f"selftest ok: {len(cases)} cases, link modes {', '.join(modes)} "
          "(plan, install, collision, tamper, add, link, reinstall, uninstall, install-set, drift)")
    return 0


def main(argv):
    """Dispatch the requested installer command."""
    verb = argv[0] if argv else "plan"
    flags = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--") and argv[i] != "--apply"}
    apply, home = "--apply" in argv, Path(flags.get("--home") or Path.home())
    if verb == "selftest":
        return selftest()
    if verb in ("plan", "install"):
        return install(flags, home, verb == "install" and apply)
    if verb == "verify":
        return verify(home)
    if verb == "drift":
        return drift(home, "--json" in argv)
    if verb == "uninstall":
        return uninstall(home, apply)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
