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

DRIFT_SCAN_SECONDS (30 seconds) bounds scanning across the whole drift run.
drift_fs checks before os.scandir, iterator next, DirEntry.is_dir, Path.stat,
os.path.realpath, Path.open, and file.read. Context exits and resolver
internals are unchecked. One blocking call can overrun the deadline.
Drift exits 2 on expiry or any folder error, otherwise 1 for differences or 0 for agreement.
With --json it prints one object with:
  folders: objects with path and status (scanned, absent, error, or not scanned)
  summary: counts named drifting, shared, and folders (scanned folders)
  drift: objects with name, first_difference (relative path), and copies
  skipped: objects with name and affected copies for names without drift
  errors: list of "<folder>: <detail>" diagnostics for failed and unscanned folders
Each copy has digest (full SHA-256), locations (installed paths), and skipped_links
(sorted relative paths of unresolvable and out-of-tree links excluded from the digest).
Synced entries with failed type queries get their own errors even if discovery
stops. If discovery stops on expiry, confirmed children are reported as not
scanned.
Only completed folders contribute to the counts and differences. The text view
prints skipped link counts with the first path, and prints each error to stderr.
"""
import ast
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from functools import partial
from io import StringIO
from itertools import islice
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
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
MAX_DRIFT_FILE_BYTES = 16 * 1024 * 1024  # per-file drift read limit (HISS-02)
DRIFT_SCAN_SECONDS = 30  # elapsed time across all drift folders (HISS-02)
HASH_CHUNK_BYTES = 64 * 1024
MAX_FIXTURE_AST_NODES = 50_000
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


class DriftScanTimeout(Exception):
    """The shared drift deadline expired."""

    def __init__(self, reason, *, detail=None):
        """Keep the deadline reason separately from the failing folder's detail."""
        super().__init__(detail or reason)
        self.reason = reason


def drift_deadline_reason(started):
    """Return the diagnostic when the whole-run scan budget has expired."""
    elapsed = monotonic() - started
    if elapsed >= DRIFT_SCAN_SECONDS:
        return f"DRIFT_SCAN_SECONDS={DRIFT_SCAN_SECONDS} reached after {elapsed:.3f}s"
    return None


def drift_check_deadline(path, started):
    """Stop scanning before the next filesystem operation if time has expired."""
    reason = drift_deadline_reason(started)
    if reason is not None:
        raise DriftScanTimeout(f"{reason} while scanning {path}")


def drift_fs(path, started, operation, /, *args, **kwargs):
    """Check the drift deadline immediately before a filesystem operation."""
    if started is not None:
        drift_check_deadline(path, started)
    return operation(*args, **kwargs)


def drift_synced_candidate(entry, path, started, errors):
    """Queue directories and retain failed type queries for the candidate's folder row."""
    try:
        return drift_fs(path, started, entry.is_dir)
    except OSError as exc:
        errors[path] = exc
        return True


def drift_entries(folder, remaining, started, *, synced_errors=None):
    """Yield bounded entries in listing order, filtering synced candidates when requested."""
    try:
        with drift_fs(folder, started, os.scandir, folder) as entries:
            for index in range(remaining + 1):
                entry = drift_fs(folder, started, next, entries, None)
                if entry is None:
                    return
                if index == remaining:
                    raise ValueError(f"{folder}: exceeds MAX_DRIFT_ENTRIES={MAX_DRIFT_ENTRIES}")
                path = Path(entry.path)
                if synced_errors is None or drift_synced_candidate(entry, path, started, synced_errors):
                    yield path
    except OSError as exc:
        raise OSError(f"cannot list {folder}: {exc}") from exc


def drift_unresolvable(exc):
    """Identify a missing path, non-directory component, or link loop."""
    # WinError.h (Microsoft Learn, System Error Codes): ERROR_FILE_NOT_FOUND (2),
    # ERROR_PATH_NOT_FOUND (3), and ERROR_CANT_RESOLVE_FILENAME (1921).
    return (isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno == errno.ELOOP
            or getattr(exc, "winerror", None) in (2, 3, 1921))


def drift_stat(path, started, *, follow_symlinks=True, allow_missing=True):
    """Stat an entry using kernel path rules, optionally skipping unresolvable paths."""
    try:
        return drift_fs(path, started, path.stat, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if allow_missing and drift_unresolvable(exc):
            return None
        raise OSError(f"cannot stat {path}: {exc}") from exc


def drift_resolve(path, started):
    """Resolve an entry, ignoring the same unresolvable targets as drift_stat."""
    # Strict realpath preserves ELOOP: CPython Lib/posixpath.py:474 (3.11),
    # :485 (3.12), :466 (3.13), :468 (3.14). On Windows, strict mode lets OS
    # errors propagate: Lib/ntpath.py:656 (3.11), :687 (3.12), :688 (3.13), :699 (3.14).
    try:
        return Path(drift_fs(path, started, os.path.realpath, path, strict=True))
    except OSError as exc:
        if drift_unresolvable(exc):
            return None
        raise OSError(f"cannot resolve {path}: {exc}") from exc


def drift_tree_path(root, path, started):
    """Resolve an entry within a skill and skip dangling links and outside targets."""
    info = drift_stat(path, started, follow_symlinks=False)
    junction = os.name == "nt" and info is not None and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    if not junction and (info is None or not stat.S_ISLNK(info.st_mode)):
        return path
    # The kernel rejects file targets followed by a slash or /. in a link.
    if drift_stat(path, started) is None:
        return None
    resolved = drift_resolve(path, started)
    return resolved if resolved is not None and resolved.is_relative_to(root) else None


def drift_skill_root(location, started):
    """Return the resolved root of a directory with a regular SKILL.md."""
    info = drift_stat(location, started)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return None
    root = drift_resolve(location, started)
    if root is None:
        return None
    marker = drift_tree_path(root, root / "SKILL.md", started)
    info = drift_stat(marker, started) if marker is not None else None
    return root if info is not None and stat.S_ISREG(info.st_mode) else None


def drift_add_file(files, root, relative, target, started):
    """Record a file digest within the cap, naming the file if reading fails."""
    if len(files) == MAX_FILES:
        raise ValueError(f"{root}: exceeds MAX_FILES={MAX_FILES}")
    try:
        files[relative.as_posix()] = digest(target, max_bytes=MAX_DRIFT_FILE_BYTES, started=started)
    except OSError as exc:
        raise OSError(f"cannot hash {target}: {exc}") from exc


def drift_tree_digest(root, started):
    """Return the tree digest, file inventory, and sorted skipped link paths."""
    pending = [(root, Path(), (root,))]
    remaining = MAX_DRIFT_ENTRIES
    files = {}
    skipped_links = []
    for _ in range(MAX_DRIFT_ENTRIES + 1):
        if not pending:
            break
        folder, prefix, ancestors = pending.pop()
        entries = sorted(drift_entries(folder, remaining, started))
        remaining -= len(entries)
        for entry in entries:
            relative = prefix / entry.name
            target = drift_tree_path(root, entry, started)
            if target is None:
                skipped_links.append(relative.as_posix())
                continue
            mode = drift_stat(target, started, allow_missing=False).st_mode
            if stat.S_ISREG(mode):
                drift_add_file(files, root, relative, target, started)
            elif stat.S_ISDIR(mode) and target not in ancestors:
                if len(relative.parts) > MAX_DRIFT_DEPTH:
                    raise ValueError(f"{root}: {relative} exceeds MAX_DRIFT_DEPTH={MAX_DRIFT_DEPTH}")
                pending.append((target, relative, (*ancestors, target)))
    tree = hashlib.sha256()
    for relative, file_digest in sorted(files.items()):
        tree.update(os.fsencode(relative) + b"\0" + bytes.fromhex(file_digest))
    return tree.hexdigest(), files, sorted(skipped_links)


def drift_scan_folder(folder, locations, trees, started):
    """Publish locations and new inventories after a full folder scan."""
    found = []
    staged = {}
    for location in sorted(drift_entries(folder, MAX_DRIFT_ENTRIES, started)):
        root = drift_skill_root(location, started)
        if root is None:
            continue
        if root not in trees and root not in staged:
            staged[root] = drift_tree_digest(root, started)
        found.append((location, root))
    drift_check_deadline(folder, started)
    trees.update(staged)
    for location, root in found:
        locations.setdefault(location.name, []).append((location, root))


def drift_folder_ready(folder, started, *, configured):
    """Distinguish available scan folders from absent roots and unrelated children."""
    try:
        mode = drift_fs(folder, started, folder.stat).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OSError(f"cannot stat {folder}: {exc}") from exc
    is_directory = stat.S_ISDIR(mode)
    if configured and not is_directory:
        raise NotADirectoryError(f"{folder}: expected a skill directory")
    return is_directory


def drift_scan_error(exc, started):
    """Preserve folder errors and detect an overrun during a failed blocking call."""
    if isinstance(exc, DriftScanTimeout):
        return str(exc), exc.reason
    reason = drift_deadline_reason(started)
    return (f"{exc} ({reason})" if reason is not None else str(exc)), reason


def drift_discover_synced(folder, started, pending):
    """Keep recorded type errors on failure and confirmed sets on expiry."""
    children, type_errors = [], {}
    try:
        children.extend(drift_entries(folder, MAX_DRIFT_ENTRIES, started, synced_errors=type_errors))
        drift_check_deadline(folder, started)
    except (DriftScanTimeout, OSError, ValueError) as exc:
        message, expired = drift_scan_error(exc, started)
        if expired is None:
            children = list(type_errors)
            raise
        raise DriftScanTimeout(expired, detail=f"synced-set discovery stopped: {message}") from exc
    finally:
        pending.extend((child, type_errors.get(child)) for child in sorted(children, reverse=True))


def drift_scan(home):
    """Scan configured roots and synced sets with one cooperative deadline.

    drift_fs checks before os.scandir, iterator next, DirEntry.is_dir,
    Path.stat, os.path.realpath, Path.open, and file.read. Context exits and
    resolver internals are unchecked. One blocking call can overrun. Only
    regular files are opened for hashing. Drift exits 2 on expiry or any
    folder error.
    """
    started = monotonic()
    clients, _ = client_dirs(home, everyone=True)
    roots = dict.fromkeys(clients.values(), False)
    roots.update((home / relative, expand) for relative, expand in DRIFT_EXTRA_FOLDERS)
    roots[clients["codex"] / ".system"] = False
    folders, locations, trees = {}, {}, {}
    errors = []
    expired = None
    pending = [(folder, None) for folder in reversed(roots)]
    for _ in range(len(roots) + MAX_DRIFT_ENTRIES):
        if not pending:
            break
        folder, type_error = pending.pop()
        if folder in folders:
            continue
        if type_error is not None:
            folders[folder] = "error"
            detail = f"cannot check directory type for {folder}: {type_error}"
            errors.append(f"{folder}: {detail}")
            expired = expired or drift_deadline_reason(started)
            continue
        if expired is not None:
            folders[folder] = "not scanned"
            errors.append(f"{folder}: not scanned: {expired}")
            continue
        try:
            if not drift_folder_ready(folder, started, configured=folder in roots):
                drift_check_deadline(folder, started)
                if folder in roots:
                    folders[folder] = "absent"
            elif roots.get(folder):
                drift_discover_synced(folder, started, pending)
            else:
                drift_scan_folder(folder, locations, trees, started)
                folders[folder] = "scanned"
        except (DriftScanTimeout, OSError, ValueError) as exc:
            folders[folder] = "error"
            message, expired = drift_scan_error(exc, started)
            errors.append(f"{folder}: {message}")
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
            if folder["status"] in ("scanned", "absent"):
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


def digest(path, *, max_bytes=None, started=None):
    """Hash file bytes in chunks, applying the supplied drift limits."""
    fingerprint = hashlib.sha256()
    remaining = max_bytes
    # Reserve one read to distinguish an exact-limit file from a larger one.
    reads = sys.maxsize if max_bytes is None else max_bytes // HASH_CHUNK_BYTES + 1
    with drift_fs(path, started, path.open, "rb") as source:
        for _ in range(reads):
            size = HASH_CHUNK_BYTES if remaining is None else min(HASH_CHUNK_BYTES, remaining + 1)
            chunk = drift_fs(path, started, source.read, size)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError(f"{path}: exceeds MAX_DRIFT_FILE_BYTES={max_bytes}")
            fingerprint.update(chunk)
            if len(chunk) < size:
                return fingerprint.hexdigest()
    raise ValueError(f"{path}: exceeds hash read limit")


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


def fixture_text(path, text):
    """Write the fixture's exact UTF-8 bytes on every platform."""
    path.write_bytes(text.encode("utf-8"))


def fixture_text_receiver_offset(function):
    """Count the explicit path argument on bare open and known unbound calls."""
    if isinstance(function, ast.Name):
        return int(function.id == "open")
    if not isinstance(function, ast.Attribute):
        return 0
    receiver = function.value
    direct = (
        isinstance(receiver, ast.Name) and receiver.id in {"builtins", "io", "Path"}
    )
    qualified = (
        isinstance(receiver, ast.Attribute) and receiver.attr == "Path"
        and isinstance(receiver.value, ast.Name) and receiver.value.id == "pathlib"
    )
    return int(direct or qualified)


def fixture_text_argument(call, name, index):
    """Read a keyword argument or its positional equivalent."""
    positional = call.args[index] if len(call.args) > index else None
    return next((item.value for item in call.keywords if item.arg == name), positional)


def fixture_text_open(call, offset):
    """Identify a literal text write mode on open() and Path.open()."""
    mode = fixture_text_argument(call, "mode", offset)
    return (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
            and "b" not in mode.value and any(flag in mode.value for flag in "wax+"))


def fixture_text_needs_newline(call):
    """Identify a direct text write without an explicit no-translation newline."""
    name = getattr(call.func, "attr", getattr(call.func, "id", None))
    if name not in {"open", "write_text"}:
        return False
    offset = fixture_text_receiver_offset(call.func)
    if name == "open" and not fixture_text_open(call, offset):
        return False
    index = (4 if name == "open" else 3) + offset
    newline = fixture_text_argument(call, "newline", index)
    return not (isinstance(newline, ast.Constant) and newline.value in {"", "\n"})


def fixture_text_offenders(tree):
    """Check direct fixture text writes within the AST node budget.

    Aliased callables, dynamic open modes, and code outside fixture_* or
    selftest_* function bodies are out of scope.
    """
    offenders = []
    pending = [(tree, None)]
    for remaining in range(MAX_FIXTURE_AST_NODES - 1, -1, -1):
        if not pending:
            break
        node, function = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(("fixture_", "selftest_")):
            function = node.name
        children = islice(ast.iter_child_nodes(node), remaining - len(pending) + 1)
        pending.extend((child, function) for child in children)
        if len(pending) > remaining:
            return [f"exceeds MAX_FIXTURE_AST_NODES={MAX_FIXTURE_AST_NODES}"]
        if function and isinstance(node, ast.Call) and fixture_text_needs_newline(node):
            offenders.append(f"{function}:{node.lineno}")
    return sorted(offenders)


def selftest_fixture_text():
    """Check the module's fixture writes and plant portable newline guard examples."""
    offenders = fixture_text_offenders(ast.parse(Path(__file__).read_bytes()))
    label = "fixtures: text writes specify newlines or write bytes"
    cases = [(f"{label} ({', '.join(offenders)})" if offenders else label, not offenders)]
    examples = (
        ("newline=None", 'path.write_text("data", newline=None)', True),
        ("update mode", 'path.open("r+")', True),
        ("explicit text mode", 'io.open(path, "wt")', True),
        ("dynamic newline", 'path.write_text("data", newline=line_ending)', True),
        ("newline=''", 'Path.open(path, "a+", newline="")', False),
        ("keyword LF", 'path.write_text("data", newline="\\n")', False),
        ("write_text positional LF", 'path.write_text("x", None, None, "\\n")', False),
        ("write_text positional empty", 'path.write_text("x", None, None, "")', False),
        ("write_text positional None", 'path.write_text("x", None, None, None)', True),
        ("bound open positional LF", 'path.open("w", -1, None, None, "\\n")', False),
        ("unbound open positional LF",
         'pathlib.Path.open(path, "w", -1, None, None, "\\n")', False),
        ("builtins.open positional empty",
         'builtins.open(path, "w", -1, None, None, "")', False),
        ("io.open positional None", 'io.open(path, "w", -1, None, None, None)', True),
        ("keyword None in bound open", 'path.open("w", newline=None)', True),
    )
    for name, statement, fails in examples:
        tree = ast.parse(f"def fixture_example(path):\n    {statement}\n")
        expected = ["fixture_example:2"] if fails else []
        cases.append((f"fixtures: newline guard checks {name}",
                      fixture_text_offenders(tree) == expected))
    with patch.object(sys.modules[__name__], "MAX_FIXTURE_AST_NODES", 1):
        cases.append(("fixtures: AST guard enforces and names its node cap",
                      fixture_text_offenders(ast.parse("")) == []
                      and fixture_text_offenders(ast.parse("pass")) == ["exceeds MAX_FIXTURE_AST_NODES=1"]))
    return cases


def fixture_repo(root, dirname="src", skills=("alpha", "beta"), install_set=None):
    """A local repository with given skills and optional install-set.txt, tagged v9.9.9."""
    src = root / dirname
    for name in skills:
        (src / "skills" / name).mkdir(parents=True)
        fixture_text(src / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n# {name}\n")
    fixture_text(src / "CREDITS.md", "# Credits\n")
    if install_set is not None:
        fixture_text(src / INSTALL_SET_FILE, install_set)
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
    """Check invalid install-set entries and the exact skill-count limit."""
    neg_tree = root / "neg_tree"
    for name in ("s1", "s2"):
        (neg_tree / "skills" / name).mkdir(parents=True, exist_ok=True)
        fixture_text(neg_tree / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n")
    fixture_text(neg_tree / INSTALL_SET_FILE, "s1\ns1\n")
    cases = [("negative: duplicate refused", refused(lambda: read_install_set(neg_tree)))]
    fixture_text(neg_tree / INSTALL_SET_FILE, "s1\nunknown_skill\n")
    cases.append(("negative: unknown name refused", refused(lambda: read_install_set(neg_tree))))
    fixture_text(neg_tree / INSTALL_SET_FILE, "s1\nsub/s2\n")
    cases.append(("negative: path separator refused", refused(lambda: read_install_set(neg_tree))))
    bound_tree = root / "bound_tree"
    for i in range(130):
        (bound_tree / "skills" / f"sk{i}").mkdir(parents=True, exist_ok=True)
        fixture_text(bound_tree / "skills" / f"sk{i}" / "SKILL.md", f"---\nname: sk{i}\n---\n")
    fixture_text(bound_tree / INSTALL_SET_FILE, "\n".join(f"sk{i}" for i in range(130)) + "\n")
    cases.append(("negative: 130 names refused", refused(lambda: read_install_set(bound_tree))))
    fixture_text(bound_tree / INSTALL_SET_FILE, "\n".join(f"sk{i}" for i in range(129)) + "\n")
    cases.append(("boundary: 129 names accepted", len(read_install_set(bound_tree)) == 129))
    return cases


def selftest_cases(root):
    """Check the fixture's install, verification failures, and uninstall."""
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
    original = target.read_bytes().decode("utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    fixture_text(target, "tampered\n")
    cases.append(("tampered file refused", quiet(verify, home) == 1))
    fixture_text(target, original)
    cases.append(("restored file passes", quiet(verify, home) == 0))
    extra = Path(manifest["store"]) / "skills/alpha/extra.md"
    fixture_text(extra, "x\n")
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
    fixture_text(skill / "SKILL.md", content)
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
    fixture_text(right / "SKILL.md", "left\n")
    status, report = fixture_drift(home)
    cases.append(("drift: identical copies exit 0",
                  status == 0 and report.get("summary") == {"drifting": 0, "shared": 1, "folders": 2}))
    for skill, content in ((left, "one\n"), (right, "two\n")):
        fixture_text(skill / "notes.md", content)
    status, report = fixture_drift(home)
    cases.append(("drift: content-only edit exits 1",
                  status == 1 and fixture_first_difference(report) == "notes.md"))
    fixture_text(right / "notes.md", "one\n")
    fixture_text(right / "extra.md", "extra\n")
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
    cases.extend([
        ("drift: each synced set scanned separately",
         status == 1 and {str(path.parent) for path in synced} <= scanned
         and any(row["name"] == "example" for row in report.get("drift", []))),
        ("drift: Copilot and Codex system folders scanned",
         status == 1 and any(row["name"] == "other" for row in report.get("drift", []))),
    ])

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
        fixture_text(skill / "second.md", variant)
    rare = fixture_skill(home, ".pi/agent/skills/example", "same\n")
    fixture_text(rare / "first.md", "rare\n")
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
    fixture_text(peer / "SKILL.md", "different\n")
    status, report = fixture_drift(home)
    skipped_by_location = {location: copy.get("skipped_links") for row in report.get("drift", [])
                           for copy in row["copies"] for location in copy["locations"]}
    cases.append(("drift: every drifting copy lists its skipped paths",
                  status == 1 and report.get("skipped") == []
                  and skipped_by_location == {str(skill): ["dangling", "outside"], str(peer): []}))
    return cases


def selftest_drift_relative_links(root):
    """Check POSIX symlink traversal and preserve the kernel's trailing path rules."""
    home = root / "relative-links"
    skill = fixture_skill(home, ".claude/skills/example", "same\n")
    peer = fixture_skill(home, ".agents/skills/example", "same\n")
    (skill / "nested/deeper").mkdir(parents=True)
    (skill / "hop").symlink_to("nested/deeper", target_is_directory=True)
    (skill / "via").symlink_to("hop/../../SKILL.md")
    fixture_text(peer / "via", "same\n")
    for folder in (skill / "dir", peer / "dir", peer / "via-dir"):
        folder.mkdir()
        fixture_text(folder / "notes.md", "notes\n")
    (skill / "via-dir").symlink_to("dir/", target_is_directory=True)
    status, report = fixture_drift(home)
    cases = [("drift: in-tree symlink with parent traversal hashes target",
              status == 0 and report["summary"] == {"drifting": 0, "shared": 1, "folders": 2}
              and report["skipped"] == [])]

    (skill / "via").unlink()
    (peer / "via").unlink()
    dangling = (("dangling", "/missing"), ("trailing-slash", "/"), ("trailing-dot", "/."))
    for name, suffix in dangling:
        (skill / name).symlink_to(f"SKILL.md{suffix}")
        (skill.parent / name).symlink_to(f"example/SKILL.md{suffix}", target_is_directory=True)
    alias = home / ".copilot/skills/example"
    alias.parent.mkdir(parents=True)
    alias.symlink_to("../../.claude/skills/example/", target_is_directory=True)
    status, report = fixture_drift(home)
    skipped = [copy for row in report["skipped"] for copy in row["copies"]]
    cases.append(("drift: dangling symlink through a file is skipped and folder kept",
                  status == 0 and report["summary"] == {"drifting": 0, "shared": 1, "folders": 3}
                  and len(skipped) == 1 and skipped[0]["locations"] == [str(skill), str(alias)]
                  and skipped[0]["skipped_links"] == sorted(name for name, _ in dangling)))
    (skill / "loop").symlink_to("loop")
    (skill.parent / "loop").symlink_to("loop", target_is_directory=True)
    status, report = fixture_drift(home)
    cases.append(("drift: looping skill and tree links leave the folder scannable",
                  status == 0 and report["summary"] == {"drifting": 0, "shared": 1, "folders": 3}
                  and any("loop" in copy["skipped_links"] for row in report["skipped"] for copy in row["copies"])))
    return cases


def selftest_drift_linked_markers(root):
    """Accept an in-tree file marker and ignore markers that require a directory."""
    home = root / "linked-markers"
    for name, suffix in (("trailing-slash", "/"), ("trailing-dot", "/."), ("valid", "")):
        skill = fixture_skill(home, f".claude/skills/{name}", "same\n")
        (skill / "SKILL.md").rename(skill / "body.md")
        (skill / "SKILL.md").symlink_to(f"body.md{suffix}")
        peer = fixture_skill(home, f".agents/skills/{name}", "same\n")
        fixture_text(peer / "body.md", "same\n")
    status, report = fixture_drift(home)
    return [("drift: only a kernel-valid symlink is a skill marker",
             status == 0 and report["summary"] == {"drifting": 0, "shared": 1, "folders": 2})]


def fixture_drift_error_cases(label, errors, expected, path, *, context_ok=True):
    """Check a complete diagnostic and reject two plausible wrong path spellings."""
    def matches(diagnostics):
        """Compare the same diagnostic for the real and planted reports."""
        return diagnostics == [expected]

    delimiter = f"{path}: "
    embedded = expected.replace(delimiter, f"{path.parent / 'wrong'} {delimiter}", 1)
    joined = expected.replace(delimiter, f"{path}:wrong ", 1)
    return [
        (label, context_ok and matches(errors)),
        (f"{label} rejects an embedded path",
         embedded != expected and not matches([embedded])),
        (f"{label} rejects a missing delimiter",
         joined != expected and not matches([joined])),
    ]


def selftest_drift_tree_limits(root):
    """Check acceptance exactly at the entry and depth caps and refusal one over."""
    home = root / "tree-limits"
    skill = fixture_skill(home, ".claude/skills/example", "example\n")
    (skill / "nested").mkdir()
    fixture_text(skill / "nested/notes.md", "notes\n")
    cases = []
    error_status = 2
    for cap, exact in (("MAX_DRIFT_ENTRIES", 3), ("MAX_DRIFT_DEPTH", 1)):
        with patch.object(sys.modules[__name__], cap, exact):
            status, report = fixture_drift(home)
        cases.append((f"drift: exactly {cap} accepted",
                      status == 0 and report["summary"]["folders"] == 1))
        with patch.object(sys.modules[__name__], cap, exact - 1):
            status, report = fixture_drift(home)
        failed = skill.resolve()
        detail = "nested "
        if cap == "MAX_DRIFT_ENTRIES":
            failed /= "nested"
            detail = ""
        expected = f"{skill.parent}: {failed}: {detail}exceeds {cap}={exact - 1}"
        cases += fixture_drift_error_cases(
            f"drift: one over {cap} exits 2 and names folder and cap",
            report["errors"], expected, failed, context_ok=status == error_status,
        )
    return cases


def selftest_drift_file_limit(root):
    """Check the byte boundary, peer scanning, and the uncapped digest."""
    home = root / "file-limit"
    skill = fixture_skill(home, ".claude/skills/example", "example\n")
    peer = fixture_skill(home, ".agents/skills/example", "example\n")
    path = skill / "data.bin"
    path.write_bytes(b"12345678")
    (peer / path.name).write_bytes(b"12345678")
    with (
        patch.object(sys.modules[__name__], "MAX_DRIFT_FILE_BYTES", 8, create=True),
        patch.object(sys.modules[__name__], "HASH_CHUNK_BYTES", 4, create=True),
    ):
        status, report = fixture_drift(home)
        cases = [("drift: exactly MAX_DRIFT_FILE_BYTES accepted",
                  status == 0 and report["summary"] == {"drifting": 0, "shared": 1, "folders": 2})]
        content = b"123456789"
        path.write_bytes(content)
        status, report = fixture_drift(home)
        cases.append(("digest: uncapped SHA-256 matches whole-file hash over drift cap",
                      digest(path) == hashlib.sha256(content).hexdigest()))
    folders = {row["path"]: row["status"] for row in report["folders"]}
    failed = path.resolve()
    expected = f"{skill.parent}: {failed}: exceeds MAX_DRIFT_FILE_BYTES=8"
    error_status = 2
    context_ok = (status == error_status and folders.get(str(skill.parent)) == "error"
                  and folders.get(str(peer.parent)) == "scanned")
    return cases + fixture_drift_error_cases(
        "drift: MAX_DRIFT_FILE_BYTES exceeded names file and cap and keeps peer",
        report["errors"], expected, failed, context_ok=context_ok,
    )


def fixture_timed_drift(home, schedule):
    """Advance a fake clock when chosen listings finish and record listing and digest paths."""
    scandir = os.scandir
    module = sys.modules[__name__]
    with patch.object(module, "monotonic", return_value=0.0, create=True) as clock:
        @contextmanager
        def advance_after_listing(folder):
            """Advance time when the iterator reaches the end of a directory."""
            def listed_entries(entries):
                """Yield entries within the caller's listing cap."""
                yield from entries
                if folder in schedule:
                    clock.return_value = schedule[folder]

            with scandir(folder) as entries:
                yield listed_entries(entries)

        with (
            patch.object(module, "DRIFT_SCAN_SECONDS", 1, create=True),
            patch.object(os, "scandir", side_effect=advance_after_listing) as listed,
            patch.object(module, "digest", wraps=digest) as hashed,
        ):
            status, report = fixture_drift(home)
    paths = {call.args[0] for call in listed.call_args_list + hashed.call_args_list}
    return status, report, paths


def selftest_drift_deadline(root):
    """Check the shared deadline before both directory listings and file digests."""
    home = root / "deadline"
    skill = fixture_skill(home, ".claude/skills/example", "same\n")
    peer = fixture_skill(home, ".agents/skills/example", "same\n")
    tree = skill.resolve()
    cases = []
    for trigger, observed, blocked in ((skill.parent, tree, skill), (tree, tree / "SKILL.md", tree / "SKILL.md")):
        status, report, paths = fixture_timed_drift(home, {trigger: 0.999})
        cases.append((f"drift: {blocked.name} allowed just under deadline",
                      status == 0 and observed in paths and report["errors"] == []
                      and report["summary"] == {"drifting": 0, "shared": 1, "folders": 2}))
        for elapsed in (1.0, 1.001):
            status, report, paths = fixture_timed_drift(home, {trigger: elapsed})
            errors = report["errors"]
            folders = {row["path"]: row["status"] for row in report["folders"]}
            cases.append((f"drift: {blocked.name} blocked at {elapsed:.3f}s and peer not scanned",
                          status == 2 and paths.isdisjoint({observed, peer.parent, peer.resolve()})
                          and len(errors) == 9
                          and errors[0].startswith(f"{skill.parent}: ")
                          and errors[0].endswith(f" while scanning {blocked}")
                          and all("DRIFT_SCAN_SECONDS=1" in error and f"{elapsed:.3f}s" in error for error in errors)
                          and folders.get(str(skill.parent)) == "error"
                          and folders.get(str(peer.parent)) == "not scanned"
                          and list(folders.values()).count("not scanned") == 8
                          and report["summary"] == {"drifting": 0, "shared": 0, "folders": 0}))
    status, report, paths = fixture_timed_drift(home, {skill.parent: 0.6, peer.parent: 1.0})
    folders = {row["path"]: row["status"] for row in report["folders"]}
    cases.append(("drift: two individually fast folders exhaust the shared deadline",
                  status == 2 and peer.resolve() not in paths
                  and folders.get(str(skill.parent)) == "scanned" and folders.get(str(peer.parent)) == "error"
                  and report["summary"] == {"drifting": 0, "shared": 0, "folders": 1}))
    return cases


def selftest_drift_synced_deadline(root):
    """Check that synced-set discovery and child scans share the same budget."""
    home = root / "synced-deadline"
    first = fixture_skill(home, ".claude/skills/synced/one/example", "first\n").parent
    second = fixture_skill(home, ".claude/skills/synced/two/example", "second\n").parent
    synced = first.parent
    cases = []
    for trigger in (synced, first):
        status, report, paths = fixture_timed_drift(home, {trigger: 1.0})
        folders = {row["path"]: row["status"] for row in report["folders"]}
        waiting = (first, second) if trigger == synced else (second,)
        cases.append((f"drift: time spent listing {trigger.name} counts against later synced sets",
                      status == 2 and folders.get(str(trigger)) == "error"
                      and paths.isdisjoint(waiting)
                      and all(folders.get(str(folder)) == "not scanned" for folder in waiting)
                      and all(any(error.startswith(f"{folder}: not scanned: DRIFT_SCAN_SECONDS=1")
                                  for error in report["errors"]) for folder in waiting)))
    status, report, paths = fixture_timed_drift(home, {first: 0.6, second: 1.0})
    folders = {row["path"]: row["status"] for row in report["folders"]}
    cases.append(("drift: two individually fast synced sets exhaust the shared deadline",
                  status == 2 and second / "example" not in paths
                  and folders.get(str(first)) == "scanned" and folders.get(str(second)) == "error"))
    return cases


def selftest_drift_inventory_deadline(root):
    """Publish inventories only after the folder's final deadline check."""
    home = root / "inventory-deadline"
    saved = fixture_skill(home, ".claude/skills/example", "saved\n")
    late = fixture_skill(home, ".agents/skills/z-late", "late\n")
    alias = late.parent / "a-shared"
    make_link(alias, saved.resolve())
    make_link(saved.parent / "alias", saved.resolve())
    saved_tree, late_tree = saved.resolve(), late.resolve()
    module = sys.modules[__name__]
    real_digest = drift_tree_digest
    locations, trees = {}, {}
    failure = None

    def hash_and_expire(tree, started):
        """Finish hashing the new tree just as the folder's budget expires."""
        inventory = real_digest(tree, started)
        if tree == late_tree:
            clock.return_value = 1.0
        return inventory

    with (
        patch.object(module, "monotonic", return_value=0.0) as clock,
        patch.object(module, "DRIFT_SCAN_SECONDS", 1),
        patch.object(
            module, "drift_tree_digest", side_effect=hash_and_expire,
        ) as hashed,
    ):
        drift_scan_folder(saved.parent, locations, trees, 0.0)
        try:
            drift_scan_folder(late.parent, locations, trees, 0.0)
        except DriftScanTimeout as exc:
            failure = str(exc)
    deadline = f"DRIFT_SCAN_SECONDS=1 reached after 1.000s while scanning {late.parent}"
    expected = {"example": [(saved, saved_tree)],
                "alias": [(saved.parent / "alias", saved_tree)]}
    return [
        ("drift: expiry after hashing keeps only the earlier folder's data",
         failure == deadline and list(trees) == [saved_tree] and locations == expected),
        ("drift: shared trees are hashed once within and across folders",
         [call.args[0] for call in hashed.call_args_list] == [saved_tree, late_tree]),
    ]


def selftest_drift_synced_regular_file(root):
    """Exclude regular entries in both a complete and an expired synced scan."""
    home = root / "synced-regular-file"
    child = fixture_skill(home, ".claude/skills/synced/one/example", "example\n").parent
    synced = child.parent
    alias = synced / "alias"
    make_link(alias, child)
    ignored = synced / "README.txt"
    fixture_text(ignored, "These are synced sets.\n")
    cases = []
    for expected, schedule, child_status in ((0, {}, "scanned"), (2, {synced: 1.0}, "not scanned")):
        status, report, _ = fixture_timed_drift(home, schedule)
        folders = {row["path"]: row["status"] for row in report["folders"]}
        cases.append((f"drift: synced regular file omitted on exit {expected}",
                      status == expected and folders.get(str(child)) == folders.get(str(alias)) == child_status
                      and str(ignored) not in folders
                      and all(not error.startswith(f"{ignored}: ") for error in report["errors"])))
    return cases


def selftest_drift_resolve_errors(root):
    """Exercise the resolver race with a loop and a non-skippable error."""
    home = root / "resolve-errors"
    skill = fixture_skill(home, ".claude/skills/example", "same\n")
    fixture_skill(home, ".agents/skills/example", "same\n")
    realpath = os.path.realpath
    cases = []
    error_status = 2
    for code in (errno.ELOOP, errno.EACCES):
        def reject_resolution(path, *, strict=False, error=code):
            """Return the requested errno only for the strict resolution of this skill."""
            if Path(path) == skill and strict:
                raise OSError(error, os.strerror(error), path)
            return realpath(path, strict=strict)

        with (
            patch.object(os.path, "realpath", reject_resolution),
            patch.object(Path, "resolve", side_effect=RuntimeError("legacy pathlib loop conversion")),
        ):
            try:
                status, report = fixture_drift(home)
            except RuntimeError as exc:
                cases.append((f"drift: strict realpath handles errno {code} ({exc})", False))
                continue
        errors = report["errors"]
        if code == errno.ELOOP:
            cases.append(("drift: strict realpath ELOOP skips a skill", status == 0 and not errors
                          and report["summary"] == {"drifting": 0, "shared": 0, "folders": 2}))
        else:
            detail = OSError(code, os.strerror(code), skill)
            expected = f"{skill.parent}: cannot resolve {skill}: {detail}"
            cases += fixture_drift_error_cases(
                "drift: other realpath errno names the failed path",
                errors, expected, skill, context_ok=status == error_status,
            )
    return cases


class FixtureDriftEntries:
    """Expose directory iteration and type checks to the fake clock."""

    def __init__(self, folder, entries, record):
        """Keep the real iterator open in the caller's context."""
        self.folder = folder
        self.entries = entries
        self.record = record

    def __iter__(self):
        """Return the instrumented directory iterator."""
        return self

    def __next__(self):
        """Record an advance and instrument the resulting entry's type query."""
        entry = self.record(self.folder, next, self.entries)
        return SimpleNamespace(path=entry.path, is_dir=partial(self.record, entry.path, entry.is_dir))


@contextmanager
def fixture_synced_entries(synced, record, *, ordered=False):
    """Instrument only iteration and type checks in the specified synced root."""
    scandir = os.scandir

    @contextmanager
    def list_directory(folder):
        """Keep the real listing open until discovery finishes."""
        with scandir(folder) as entries:
            if folder == synced and ordered:
                bounded = islice(entries, MAX_DRIFT_ENTRIES + 1)
                listing = iter(sorted(bounded, key=lambda entry: entry.name))
            else:
                listing = entries
            if folder == synced:
                yield FixtureDriftEntries(folder, listing, record)
            else:
                yield listing

    with patch.object(os, "scandir", list_directory):
        yield


def fixture_reject_synced(failed, code, path, operation, /, *args):
    """Fail only this entry's type query, leaving the rest of the listing intact."""
    if Path(path) == failed:
        raise OSError(code, os.strerror(code), path)
    return operation(*args)


def selftest_drift_synced_entry_errors(root):
    """Report a failed type query on its own row and keep the healthy sibling."""
    cases = []
    for code, name in ((errno.ELOOP, "loop"), (errno.EACCES, "denied")):
        home = root / f"synced-{name}"
        healthy = fixture_skill(home, ".claude/skills/synced/aaa-healthy/example", "example\n").parent
        synced = healthy.parent
        failed = synced / name

        if code == errno.ELOOP and os.name != "nt":
            failed.symlink_to(failed.name, target_is_directory=True)
            status, report = fixture_drift(home)
        else:
            failed.mkdir()
            with fixture_synced_entries(synced, partial(fixture_reject_synced, failed, code)):
                status, report = fixture_drift(home)
        folders = {row["path"]: row["status"] for row in report["folders"]}
        errors = report["errors"]
        cases.append((f"drift: synced {name} error keeps healthy sibling",
                      status == 2 and folders.get(str(failed)) == "error"
                      and folders.get(str(healthy)) == "scanned" and str(synced) not in folders
                      and len(errors) == 1 and errors[0].startswith(f"{failed}: ")))
    return cases


def fixture_synced_discovery_failure(home, failed, first, later, *, expire):
    """Stop discovery after recording a type error and confirming a healthy child."""
    module = sys.modules[__name__]
    queried = []

    def record(path, operation, /, *args):
        """Fail a listing advance or expire when the later child is confirmed."""
        if operation is next:
            if not expire and first in queried:
                raise OSError("fixture listing interrupted")
            return operation(*args)
        entry = Path(path)
        queried.append(entry)
        result = fixture_reject_synced(failed, errno.EACCES, path, operation, *args)
        if expire and entry == later and result:
            clock.return_value = 1.0
        return result

    with (
        patch.object(module, "DRIFT_SCAN_SECONDS", 1),
        patch.object(module, "monotonic", return_value=0.0) as clock,
        patch.object(module, "drift_folder_ready", wraps=drift_folder_ready) as checked,
        fixture_synced_entries(failed.parent, record, ordered=True),
    ):
        status, report = fixture_drift(home)
    return status, report, queried, {call.args[0] for call in checked.call_args_list}


def selftest_drift_synced_discovery_failure(root):
    """Keep exactly the recorded type errors when discovery stops."""
    home = root / "synced-discovery-failure"
    synced = home / ".claude/skills/synced"
    failed = synced / "aaa-denied"
    failed.mkdir(parents=True)
    first = fixture_skill(synced, "bbb-first/example", "first\n").parent
    later = fixture_skill(synced, "ccc-later/example", "later\n").parent
    return [fixture_synced_discovery_case(home, failed, first, later, expire=expire)
            for expire in (True, False)]


def fixture_synced_discovery_case(home, failed, first, later, *, expire):
    """Match the complete report errors and verify children receive no folder I/O."""
    synced = failed.parent
    detail = OSError(errno.EACCES, os.strerror(errno.EACCES), str(failed))
    denied = f"{failed}: cannot check directory type for {failed}: {detail}"
    expected = {synced: "error", failed: "error"}
    if expire:
        reason = f"DRIFT_SCAN_SECONDS=1 reached after 1.000s while scanning {synced}"
        waiting = (first, later, home / ".codex/skills/.system")
        expected.update(dict.fromkeys(waiting, "not scanned"))
        errors = [f"{synced}: synced-set discovery stopped: {reason}", denied,
                  *(f"{path}: not scanned: {reason}" for path in waiting)]
    else:
        errors = [
            f"{synced}: cannot list {synced}: fixture listing interrupted", denied,
        ]
    status, report, queried, checked = fixture_synced_discovery_failure(
        home, failed, first, later, expire=expire,
    )
    folders = {Path(row["path"]): row["status"] for row in report["folders"]}
    relevant = {path: state for path, state in folders.items()
                if path.is_relative_to(synced) or path in expected}
    trigger = "expiry" if expire else "listing failure"
    error_status = 2
    return (f"drift: synced {trigger} keeps the type error and exact diagnostics",
            status == error_status and relevant == expected
            and report["errors"] == errors
            and queried == ([failed, first, later] if expire else [failed, first])
            and checked.isdisjoint({failed, first, later}))


def fixture_synced_cut(home, synced, cut):
    """Run drift with expiry immediately after the selected directory confirmation."""
    module = sys.modules[__name__]
    seen = []

    def confirm(path, operation, /, *args):
        """Advance time only when the selected is_dir call confirms a directory."""
        result = operation(*args)
        if operation is not next and result:
            seen.append(Path(path))
            if len(seen) == cut:
                clock.return_value = 1.0
        return result

    with (
        patch.object(module, "monotonic", return_value=0.0) as clock,
        patch.object(module, "DRIFT_SCAN_SECONDS", 1),
        fixture_synced_entries(synced, confirm),
    ):
        status, report = fixture_drift(home)
    return status, report, sorted(seen)


def selftest_drift_synced_discovery_deadline(root):
    """Check exactly which synced sets are reported at each type-query cut."""
    home = root / "synced-discovery-deadline"
    synced = home / ".claude/skills/synced"
    for name in ("one", "two", "three"):
        fixture_skill(synced, f"{name}/example", "example\n")
    cases = []
    for cut in (1, 2, 3):
        status, report, seen = fixture_synced_cut(home, synced, cut)
        rows = [row for row in report["folders"] if Path(row["path"]).is_relative_to(synced)]
        expected = [{"path": str(synced), "status": "error"},
                    *({"path": str(child), "status": "not scanned"} for child in seen)]
        errors = [error for error in report["errors"] if error.startswith((f"{synced}: ", f"{synced}{os.sep}"))]
        reason = f"DRIFT_SCAN_SECONDS=1 reached after 1.000s while scanning {synced}"
        expected_errors = [f"{synced}: synced-set discovery stopped: {reason}",
                           *(f"{child}: not scanned: {reason}" for child in seen)]
        cases.append((f"drift: discovery expiry keeps exactly {cut} confirmed synced sets",
                      status == 2 and len(seen) == cut and rows == expected and errors == expected_errors))
    return cases


def fixture_counted_drift(home, expire_after):
    """Expire after one outer filesystem call and record every call that follows."""
    calls, late = [], []
    active = False
    scandir, open_file = os.scandir, Path.open
    module = sys.modules[__name__]

    def record(path, operation, /, *args, **kwargs):
        """Count the filesystem boundary, including calls that end with an exception."""
        nonlocal active
        if active:
            return operation(*args, **kwargs)
        active = True
        try:
            calls.append(Path(path))
            if clock.return_value >= 1.0:
                late.append(Path(path))
            return operation(*args, **kwargs)
        finally:
            active = False
            if len(calls) == expire_after:
                clock.return_value = 1.0

    def watch(operation):
        """Instrument a primitive whose first argument is its path."""
        def watched(path, *args, **kwargs):
            """Preserve the original primitive's arguments and result."""
            return record(path, operation, path, *args, **kwargs)
        return watched

    @contextmanager
    def list_directory(folder):
        """Watch scandir, iteration and directory entry type checks."""
        with record(folder, scandir, folder) as entries:
            yield FixtureDriftEntries(folder, entries, record)

    def open_for_hash(path, *args, **kwargs):
        """Watch both opening and reading a hash input."""
        source = record(path, open_file, path, *args, **kwargs)
        source.read = partial(record, path, source.read)
        return source

    with (
        patch.object(module, "monotonic", return_value=0.0) as clock,
        patch.object(module, "DRIFT_SCAN_SECONDS", 1),
        patch.object(module, "HASH_CHUNK_BYTES", 4),
        patch.object(os, "stat", watch(os.stat)),
        patch.object(os, "lstat", watch(os.lstat)),
        patch.object(os, "readlink", watch(os.readlink)),
        patch.object(os.path, "realpath", watch(os.path.realpath)),
        patch.object(os, "scandir", list_directory),
        patch.object(Path, "open", open_for_hash),
    ):
        status, report = fixture_drift(home)
    return status, report["errors"], calls, late


def selftest_drift_filesystem_deadline(root):
    """Sweep expiry across folder discovery, link probes, walking and hash reads."""
    home = root / "filesystem-deadline"
    skill = fixture_skill(home, ".claude/skills/example", "same\n")
    nested = skill / "nested"
    nested.mkdir()
    fixture_text(nested / "data", "several chunks\n")
    make_link(skill / "alias", nested)
    fixture_skill(home, ".claude/skills/synced/one/synced-example", "same\n")
    status, _, trace, _ = fixture_counted_drift(home, None)
    label = "drift: zero filesystem calls start after expiry"
    if status != 0 or not trace:
        return [(f"{label} (failed to collect an uninterrupted trace)", False)]
    paths = {str(path) for path in trace}
    for index in range(1, len(trace) + 1):
        status, errors, calls, late = fixture_counted_drift(home, index)
        if status != 2 or late or calls != trace[:index] or not errors:
            return [(f"{label} (call {index}, late paths {late})", False)]
        _, _, path = errors[0].partition("DRIFT_SCAN_SECONDS=1 reached after 1.000s while scanning ")
        if path not in paths:
            return [(f"{label} (call {index}, diagnostic {errors[0]})", False)]
    return [(label, True)]


def selftest_drift_denied(root):
    """Check that denied skill roots, tree links, and markers fail their folders."""
    labels = ("drift: skill link through a chmod 000 parent",
              "drift: plain chmod 000 skill folder",
              "drift: tree link through a chmod 000 parent",
              "drift: marker link through a chmod 000 parent")
    if os.geteuid() == 0:
        print("selftest skipped as root: " + ", ".join(labels))
        return [(f"{label} (skipped as root)", True) for label in labels]
    home = root / "denied"
    peer = fixture_skill(home, ".claude/skills/demo", "peer\n")
    target = fixture_skill(home, "private/demo", "linked\n")
    plain = fixture_skill(home, ".copilot/skills/plain", "plain\n")
    link = home / ".agents/skills/demo"
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    tree_link = fixture_skill(home, ".codex/skills/tree", "tree\n") / "notes.md"
    marker_link = home / ".gemini/skills/marker/SKILL.md"
    marker_link.parent.mkdir(parents=True)
    for path in (tree_link, marker_link):
        path.symlink_to(target / "SKILL.md")
    cases = ((link, link.parent), (plain.resolve() / "SKILL.md", plain.parent),
             (tree_link.parent.resolve() / tree_link.name, tree_link.parent.parent),
             (marker_link.parent.resolve() / marker_link.name, marker_link.parent.parent))
    accessible, _ = fixture_drift(home)
    denied = (target.parent, plain)
    try:
        for path in denied:
            path.chmod(0)
        status, report = fixture_drift(home)
    finally:
        for path in denied:
            path.chmod(0o700)
    folders = {row["path"]: row["status"] for row in report["folders"]}
    return [(label, accessible == 1 and status == 2 and folders.get(str(peer.parent)) == "scanned"
             and folders.get(str(folder)) == "error"
             and any(error.startswith(f"{folder}: cannot stat {path}: ") for error in report["errors"]))
            for label, (path, folder) in zip(labels, cases, strict=True)]


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
    fixture_text(unreadable, "notes\n")
    with patch.object(sys.modules[__name__], "MAX_FILES", 1):
        status, report = fixture_drift(home)
    failed = skill.resolve()
    expected = f"{skill.parent}: {failed}: exceeds MAX_FILES=1"
    error_status = 2
    cases += fixture_drift_error_cases(
        "drift: MAX_FILES exceeded exits 2 and names folder and cap",
        report["errors"], expected, failed, context_ok=status == error_status,
    )

    open_file = Path.open

    def reject_file(path, *args, **kwargs):
        """Simulate an open failure for one fixture file on any platform."""
        if path == unreadable:
            raise PermissionError(f"permission denied: {path}")
        return open_file(path, *args, **kwargs)

    with patch.object(Path, "open", reject_file):
        status, report = fixture_drift(home)
    errors = report.get("errors", [])
    cases.append(("drift: unreadable file exits 2 and names path",
                  status == 2 and len(errors) == 1 and f"cannot hash {unreadable}: " in errors[0]
                  and "permission denied" in errors[0]))
    return cases


def selftest_drift_partial_scan(root):
    """Check that failed folders contribute errors and completed folders contribute drift."""
    home = root / "partial-scan"
    fixture_skill(home, ".claude/skills/example", "would leak\n")
    failed = [fixture_skill(home, f"{folder}/z-bad", "cap\n")
              for folder in (".claude/skills", ".gemini/config/skills")]
    for skill in failed:
        fixture_text(skill / "extra.md", "over the cap\n")
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


def selftest_drift_home_refusal(root):
    """Reject a missing home before looking up a default or dispatching any verb."""
    module = sys.modules[__name__]
    handlers = ("drift", "install", "verify", "uninstall", "selftest")
    invalid = [(verb, "--home") for verb in (*handlers, "plan")]
    invalid += [("drift", "--home", "--json"), ("drift", "--json", "--home"),
                ("plan", "--home", "--apply"), ("drift", "--home", "")]
    cases = []
    with (
        patch.object(Path, "home", return_value=root) as default_home,
        patch.multiple(module, **dict.fromkeys(handlers, fixture_unexpected_dispatch)),
    ):
        for argv in invalid:
            problem = None
            default_home.reset_mock()
            try:
                main(argv)
            except (SystemExit, RuntimeError) as exc:
                problem = str(exc)
            cases.append((f"drift: invalid home refused before dispatch {argv!r}",
                          problem == "refused: --home requires a directory value"
                          and not default_home.called))
    return cases


def fixture_unexpected_dispatch(*_args, **_kwargs):
    """Prevent invalid fixture input from starting a scan or write."""
    raise RuntimeError("invalid --home reached command dispatch")


def selftest_drift_home_order(root):
    """Accept a fixture home with the JSON switch on either side."""
    home = root / "cli-home"
    skill = fixture_skill(home, ".claude/skills/example", "fixture\n")
    scanned = {"path": str(skill.parent), "status": "scanned"}
    cases = []
    with patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}):
        for flags in (("--json", "--home", str(home)), ("--home", str(home), "--json")):
            output = StringIO()
            with redirect_stdout(output):
                status = main(("drift", *flags))
            report = json.loads(output.getvalue())
            cases.append((f"drift: accepted home with {flags[0]} first",
                          status == 0 and report["errors"] == []
                          and scanned in report["folders"]
                          and report["summary"] == {
                              "drifting": 0, "shared": 0, "folders": 1,
                          }))
    return cases


def selftest_drift_cases(root):
    """Run all drift cases through a non-canonical fixture root."""
    checked = [("drift: fixture root is non-canonical", root != root.resolve())]
    checked += (selftest_drift_content(root) + selftest_drift_locations(root) + selftest_drift_comparison(root)
                + selftest_drift_links(root) + selftest_drift_errors(root) + selftest_drift_partial_scan(root)
                + selftest_drift_tree_limits(root) + selftest_drift_file_limit(root) + selftest_drift_deadline(root)
                + selftest_drift_synced_deadline(root) + selftest_drift_synced_regular_file(root)
                + selftest_drift_synced_entry_errors(root) + selftest_drift_synced_discovery_deadline(root)
                + selftest_drift_resolve_errors(root)
                + selftest_drift_filesystem_deadline(root)
                + selftest_drift_synced_discovery_failure(root)
                + selftest_drift_inventory_deadline(root)
                + selftest_drift_home_refusal(root) + selftest_drift_home_order(root))
    if os.name != "nt":
        checked += (selftest_drift_relative_links(root) + selftest_drift_linked_markers(root)
                    + selftest_drift_denied(root))
    return checked


def selftest():
    """Run the installer and drift fixtures for the available link modes."""
    keep = {name: os.environ.get(name) for name in ("CODEX_HOME", LINK_MODE_ENV)}
    modes = ["symlink", "junction"] if os.name == "nt" else ["symlink"]
    cases = selftest_fixture_text()
    try:
        for mode in modes:
            os.environ[LINK_MODE_ENV] = mode
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                real, alias = root / "real", root / "alias"
                real.mkdir()
                make_link(alias, real.resolve())
                checked = selftest_cases(root) + selftest_drift_cases(alias)
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


def install_cli_flags(arguments):
    """Collect the value options and refuse a missing or switch-like home."""
    flags = {}
    for flag, value in zip(arguments, (*arguments[1:], None), strict=False):
        if flag == "--home" and (not value or value.startswith("--")):
            raise SystemExit("refused: --home requires a directory value")
        if flag in {"--tag", "--repo", "--home", "--clients"} and value is not None:
            flags[flag] = value
    return flags


def main(argv):
    """Dispatch the requested installer command."""
    verb = argv[0] if argv else "plan"
    arguments = argv[1:]
    flags = install_cli_flags(arguments)
    home = Path(flags["--home"]) if "--home" in flags else Path.home()
    apply = "--apply" in arguments
    if verb == "selftest":
        return selftest()
    if verb in ("plan", "install"):
        return install(flags, home, verb == "install" and apply)
    if verb == "verify":
        return verify(home)
    if verb == "drift":
        return drift(home, "--json" in arguments)
    if verb == "uninstall":
        return uninstall(home, apply)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
