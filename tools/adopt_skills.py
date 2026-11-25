#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Adopt overlap-free census candidates; preview by default, --apply publishes.

Only trusted repository gates run. Upstream files are data, read from pinned Git
objects; a dry run never expands the supplied sparse checkouts or writes root.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# isort: split
# Codacy's Ruff sorts tomllib without the py311 target.
import tomllib

# isort: split

import check_skills
import overlap
from candidates import (
    LICENSE_FILENAMES,
    MAX_ROWS,
    find_folder_licence_file,
    normalize_name,
)
from census import LiveReader, SourceReadError
from check_skills import front_matter_or_empty
from git_blob import git_blob_id
from input_identity import (
    output_lock,
    provider_id,
    resolve_base_urls,
    validate_base_url,
)
from judge_groups import (
    collapse_copies,
    get_skill_text,
    is_existing_skill,
    load_sources,
)
from licence_policy import (
    LICENSE_BASENAME_PATTERN,
    candidate_licence_id,
)
from licence_text import (
    LICENCE_NOTICES_FILE,
    MAX_LICENCE_BYTES,
    unreviewed_licence_notices,
)

MAX_FILES = 200
MAX_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
TIMEOUT = 60
REPORT_FIELDS = ['candidate', 'verdict', 'directory', 'reason']
INDEX_FIELDS = ['skill', 'source', 'functions', 'domains', 'overlap_group']
INDEX_HEADER = '\t'.join(INDEX_FIELDS)
SKILLS_DIR = 'skills'
LICENCES_DIR = 'LICENSES'
REUSE_FILE = 'REUSE.toml'
CREDITS_FILE = 'CREDITS.md'
INDEX_FILE = f'{SKILLS_DIR}/INDEX.tsv'
DEFAULT_REPORT_FILE = 'adopt-report.tsv'
PUBLICATION_FILES = (REUSE_FILE, CREDITS_FILE, INDEX_FILE, LICENCE_NOTICES_FILE)
PUBLICATION_TREES = (SKILLS_DIR, LICENCES_DIR)
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z')
NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
MARKDOWN_EXTENSIONS = ('.md', '.markdown', '.mdx')
GATE_FINDING = re.compile(
    r'^(?P<path>\S+?):\d+(?::\d+)?(?::?\s+(?:error|warning))?\s+(?P<rule>[A-Za-z0-9_-]+)(?:/\S*)?',
    re.M
)
HISS_FINDING = re.compile(
    r'^\s*\[(?P<rule>[^\]]+)\]\s+(?P<path>\S+?):\d+',
    re.M
)
THREAD_HEADER = re.compile(r'^#?\s*threads:\s*(.+)\Z', re.IGNORECASE)
THREAD_ID = re.compile(r'^[A-Za-z0-9_-]+\Z')


class Refused(ValueError):
    """One candidate cannot be adopted; other candidates can continue."""


class _LicenceDownloadError(ValueError):
    """A required canonical SPDX template could not be downloaded."""


class _LicenceOutputSymlinkError(ValueError):
    """A licence template destination is a symlink."""


def command(args, *, cwd=None, data=None, timeout=TIMEOUT, allow_fetch=False):
    """Run a tool with a deadline and return its stdout or raise a contextual error."""
    result = subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0',
                                 'GIT_NO_LAZY_FETCH': '0' if allow_fetch else '1'})
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()[:500]
        raise Refused(f'command: {args[0]}: {detail}')
    return result.stdout


def git(checkout, *args, data=None, allow_fetch=False):
    return command(['git', '-C', str(checkout), *args], data=data, allow_fetch=allow_fetch)


def _read_rows(path, handle, required):
    """Read a bounded TSV from the supplied snapshot and validate its columns."""
    reader = csv.DictReader(handle, delimiter='\t')
    if not required.issubset(reader.fieldnames or []):
        raise ValueError(f'{path}: missing columns {sorted(required)}')
    rows = []
    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            raise ValueError(f'{path}: more than {MAX_ROWS} rows')
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'{path}: malformed row {index + 2}')
        rows.append(row)
    return rows


def read_rows(path, required):
    """Read a bounded input TSV with the required columns."""
    with path.open(encoding='utf-8', newline='') as handle:
        return _read_rows(path, handle, required)


def _read_group_rows(path):
    """Use the verified groups snapshot for adoption."""
    with overlap.open_overlap_generation(path) as (handle, _):
        if handle is None:
            raise FileNotFoundError(path)
        return _read_rows(path, handle, {'members'})


def candidate_id(row):
    return f"{row['source']}:{row['path']}"


def inputs(candidates, groups, census):
    """Load candidate, census, and verified overlap-group rows."""
    candidate_rows = read_rows(candidates, {'source', 'path', 'skill_name', 'verdict',
                                            'licence', 'licence_evidence', 'functions'})
    census = read_rows(census, {'source', 'path', 'pin'})
    groups = _read_group_rows(groups)
    order, pins = {}, {}
    for index, row in enumerate(census):
        source, pin = row['source'], row['pin']
        if not check_skills.COMMIT.fullmatch(pin) or pins.get(source, pin) != pin:
            raise ValueError(f'{source}: invalid or conflicting census pin')
        pins[source] = pin
        order.setdefault(candidate_id(row), index)
    selected = [row for row in candidate_rows if row['verdict'] == 'candidate']
    if len({candidate_id(row) for row in selected}) != len(selected):
        raise ValueError('duplicate candidate rows')
    selected.sort(key=lambda row: (order.get(candidate_id(row), MAX_ROWS), candidate_id(row)))
    grouped = {member.strip() for row in groups for member in row['members'].split(',')}
    group_map = {}
    for row in groups:
        gid = row.get('group_id', '').strip()
        for member in row['members'].split(','):
            m = member.strip()
            if m:
                group_map[m] = gid
    return selected, grouped, order, pins, groups, group_map, candidate_rows


def safe_path(raw):
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or '\\' in raw or ':' in raw
            or any(part in ('', '.', '..', '.git') for part in raw.split('/'))
            or any(ord(char) < 32 for char in raw)):
        raise Refused('path-outside-folder')
    return path


def is_licence_name(name: str) -> bool:
    """Identify upstream licence files by their shared filename policy."""
    return name in LICENSE_FILENAMES or bool(LICENSE_BASENAME_PATTERN.fullmatch(name))


def checkout_for(checkouts, row, pin):
    source = row['source']
    if not REPOSITORY.fullmatch(source):
        raise Refused('invalid-source')
    path = safe_path(row['path'])
    if path.name != 'SKILL.md':
        raise Refused('path-must-name-SKILL.md')
    checkout = checkouts / source.replace('/', '__')
    if checkout.is_symlink() or checkout.resolve().parent != checkouts.resolve():
        raise Refused('checkout-outside-folder')
    if not (checkout / '.git').exists():
        raise Refused('missing-git-checkout')
    if git(checkout, 'rev-parse', 'HEAD').decode().strip() != pin:
        raise Refused('checkout-pin-mismatch')
    if git(checkout, 'status', '--porcelain', '--untracked-files=all'):
        raise Refused('checkout-dirty')
    return checkout, path.parent


def tree_files(checkout, pin, folder, recursive=True):
    tree = pin if str(folder) == '.' else f'{pin}:{folder}'
    args = ['ls-tree', '-l', '-z'] + (['-r'] if recursive else []) + [tree]
    raw = git(checkout, *args)
    if len(raw) > MAX_TREE_BYTES:
        raise Refused('tree-too-large')
    entries = []
    for record in raw.split(b'\0')[:MAX_ROWS + 1]:
        if not record:
            continue
        header, name = record.decode('utf-8').split('\t', 1)
        mode, kind, oid, size = header.split()
        entries.append((safe_path(name), mode, kind, oid, size))
    if len(entries) > MAX_ROWS:
        raise Refused('tree-too-large')
    return entries


def filter_entries(entries, folder):
    root_skill = str(folder) == '.'
    kept, furniture, workbench = [], [], []
    for entry in entries[:MAX_ROWS]:
        path = entry[0]
        if root_skill and len(path.parts) == 1:
            if path.name == 'SKILL.md' or is_licence_name(path.name):
                kept.append(entry)
            else:
                furniture.append(str(path))
        elif any(part.startswith('.') for part in path.parts) or path.name.lower() in check_skills.AGENT_CONTROL_FILES:
            workbench.append(str(path))
        else:
            kept.append(entry)
    notes = []
    if furniture:
        notes.append(f"omitted repository furniture ({', '.join(sorted(furniture))})")
    if workbench and root_skill:
        notes.append(f"omitted agent workbench files ({', '.join(sorted(workbench))})")
    return kept, notes


def validate_files(entries, allow_missing=False):
    if len(entries) > MAX_FILES:
        raise Refused('file-limit: more than 200 files')
    total = 0
    for path, mode, kind, _, size in entries:
        if mode == '120000':
            raise Refused(f'symlink: {path}')
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise Refused(f'non-regular-file: {path}')
        if size == 'BAD':
            if not allow_missing:
                raise Refused(f'missing-local-git-object: {path}')
        else:
            total += int(size)
    if total > MAX_BYTES:
        raise Refused('size-limit: more than 2 MiB')


def expand_sparse(checkout, path, *, file=False):
    if git(checkout, 'config', '--default', 'false', '--get', 'core.sparseCheckout').strip() != b'true':
        return
    # Cone mode already includes root files; non-cone mode needs a literal pattern.
    cone = git(checkout, 'config', '--default', 'false', '--get', 'core.sparseCheckoutCone').strip() == b'true'
    if cone:
        git(checkout, 'sparse-checkout', 'add', '--', '.' if file else str(path), allow_fetch=True)
    else:
        literal = re.sub(r'([\\*?\[\] ])', r'\\\1', str(path))
        pattern = '/*' if str(path) == '.' else '/' + literal + ('' if file else '/')
        git(checkout, 'sparse-checkout', 'add', '--stdin', data=(pattern + '\n').encode(), allow_fetch=True)


def copy_blobs(checkout, entries, destination, allow_fetch=False):
    total = 0
    for path, mode, _, oid, size in entries:
        if allow_fetch:
            try:
                data = git(checkout, 'cat-file', 'blob', oid, allow_fetch=True)
            except Refused as error:
                detail = str(error).removeprefix('command: git: ')
                detail = ' '.join(detail.split())
                raise Refused(f'missing-git-object: {path} (fetch failed: {detail})') from error
        else:
            data = git(checkout, 'cat-file', 'blob', oid)
        if size != 'BAD' and len(data) != int(size):
            raise Refused(f'blob-size-mismatch: {path}')
        total += len(data)
        if total > MAX_BYTES:
            raise Refused('size-limit: more than 2 MiB')
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == '100755' else 0o644)


def rewrite_fences(content: str) -> tuple[str, bool]:
    lines = content.splitlines(keepends=True)
    tokens = check_skills.COMMONMARK.parse(content)
    changed = False
    for token in tokens:
        if token.type == 'fence' and not token.info.strip() and token.map:
            idx = token.map[0]
            line = lines[idx]
            pos = line.find(token.markup)
            if pos != -1:
                indent = line[:pos]
                newline = '\r\n' if line.endswith('\r\n') else ('\n' if line.endswith('\n') else '')
                new_line = f'{indent}{token.markup}text{newline}'
                if new_line != line:
                    lines[idx] = new_line
                    changed = True
    return ''.join(lines), changed


def _is_skill_markdown(path: Path, skill: Path) -> bool:
    """Select Markdown that can be rewritten without changing a shipped licence."""
    return (path.suffix.lower() in MARKDOWN_EXTENSIONS and path.name != 'ORIGIN.md'
            and not (path.parent == skill and is_licence_name(path.name)) and path.is_file())


def rewrite_skill_fences(skill: Path) -> set[str]:
    """Rewrite unlabelled code fences in eligible Markdown files."""
    changed_files: set[str] = set()
    for path in sorted(skill.rglob('*'))[:MAX_ROWS]:
        if _is_skill_markdown(path, skill):
            text = path.read_text(encoding='utf-8')
            new_text, changed = rewrite_fences(text)
            if changed:
                path.write_text(new_text, encoding='utf-8')
                changed_files.add(path.relative_to(skill).as_posix())
    return changed_files


default_reader = LiveReader().gh_json
gh_json = default_reader


def upstream_licence(repository, reader):
    if not REPOSITORY.fullmatch(repository):
        raise Refused('licence-text-missing')
    info = reader(f'repos/{repository}')
    branch = urllib.parse.quote(info['default_branch'], safe='')
    pin = reader(f'repos/{repository}/commits/{branch}')['sha']
    if not check_skills.COMMIT.fullmatch(pin):
        raise Refused('licence-text-missing')
    result = reader(f'repos/{repository}/license?ref={pin}')
    if result.get('encoding') != 'base64':
        raise Refused('licence-text-missing')
    data = base64.b64decode(''.join(result['content'].split()), validate=True)
    return data, f'licence text from <https://github.com/{repository}> at commit {pin}'


def _repository_licence_text(checkout, pin, apply) -> tuple[str, bytes]:
    """Read the selected licence basename and bytes from the pinned repository."""
    entries = tree_files(checkout, pin, PurePosixPath('.'), recursive=False)
    entries = [entry for entry in entries[:MAX_ROWS] if is_licence_name(entry[0].name)]
    with tempfile.TemporaryDirectory(prefix='adopt-licence-') as tmp:
        if apply:
            for entry in entries[:MAX_ROWS]:
                expand_sparse(checkout, entry[0], file=True)
            entries = [(path, mode, kind, oid, git(checkout, 'cat-file', '-s', oid).decode().strip())
                       for path, mode, kind, oid, _ in entries[:MAX_ROWS]]
        validate_files(entries, allow_missing=not apply)
        copy_blobs(checkout, entries, Path(tmp), allow_fetch=not apply)
        licence = find_folder_licence_file(Path(tmp))
        return (licence.name, licence.read_bytes()) if licence else ('LICENSE', b'')


def _selected_licence_text(evidence, checkout, pin, skill, inputs: StageInputs) -> tuple[str, bytes, str]:
    """Get the selected basename, original bytes and optional evidence note."""
    if evidence == 'folder':
        licence = find_folder_licence_file(skill)
        if licence is not None:
            return licence.name, licence.read_bytes(), ''
    elif evidence in ('repository', 'repository:self', 'frontmatter'):
        name, data = _repository_licence_text(checkout, pin, inputs.apply)
        return name, data, ''
    elif evidence.startswith('upstream:'):
        data, note = upstream_licence(evidence.removeprefix('upstream:'), inputs.reader)
        return 'LICENSE', data, note
    return 'LICENSE', b'', ''


def licence_text(row, checkout, pin, skill, inputs: StageInputs):
    """Copy the licence text from the pinned source to the staged skill."""
    try:
        name, data, note = _selected_licence_text(row['licence_evidence'], checkout, pin, skill, inputs)
        if not data.strip() or len(data) > MAX_BYTES:
            raise Refused('licence-text-missing')
        data.decode('utf-8')
    except SourceReadError as error:
        detail = ' '.join(str(error).split())
        raise Refused(f'licence-text-missing: {detail}' if detail else 'licence-text-missing') from error
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise Refused('licence-text-missing') from error
    destination = skill / name
    destination.write_bytes(data)
    discovered = find_folder_licence_file(skill)
    if discovered is not None and discovered != destination:
        raise Refused(f'licence-text-shadowed: {name} by {discovered.name}')
    return note


def choose_name(row, occupied):
    name = normalize_name(row['skill_name'])
    if not NAME.fullmatch(name):
        raise Refused('invalid-name')
    if name in occupied:
        name += '-' + row['source'].split('/')[0].lower()
    if name in occupied:
        raise Refused('name-collision')
    return name


def attribution(skill, row, folder, pin, note, fence_changed=False):
    md = skill / 'SKILL.md'
    text = md.read_text(encoding='utf-8')
    front = check_skills.FRONT.match(text)
    changes = []
    if front:
        names = [m for m in check_skills.FIELD.finditer(front.group(1)) if m.group('key') == 'name']
        if len(names) == 1 and names[0].group('value') != skill.name:
            start, end = names[0].span('value')
            start, end = start + front.start(1), end + front.start(1)
            changes.append(f'name changed from {names[0].group("value")} to {skill.name}')
            md.write_text(text[:start] + skill.name + text[end:], encoding='utf-8')
    if fence_changed:
        changes.append('code fences without a language marked text')
    if note:
        changes.append(note)
    changes.append('added provenance and licence attribution')
    origin = (f'upstream: <https://github.com/{row["source"]}>\npath: {folder}\ncommit: {pin}\n'
              f'licence: {row["licence"]}\nchanges: {"; ".join(changes)}\n')
    (skill / 'ORIGIN.md').write_text(origin, encoding='utf-8')


def markdown_gate(root, staging, skill):
    """Run the actual repository gate, including private-link checks, in isolation.

    verify.mjs has no fixer mode. Do not invent formatting rules or silently
    substitute another linter. Its pinned dependencies install outside root.
    """
    gate = root / 'tools/markdownlint/verify.mjs'
    if not gate.is_file():
        raise Refused('markdown')
    try:
        # A vendored .gitignore must not hide supporting Markdown from inventory.
        git(staging, 'add', '--force', '--', 'skills')
        proc = subprocess.run(['node', str(gate)], cwd=staging, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=600,
                              env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1'})
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused('markdown') from error
    if proc.returncode:
        raw = ((proc.stderr or b'').decode('utf-8', errors='replace') + '\n' +
               (proc.stdout or b'').decode('utf-8', errors='replace'))
        prefix = f'skills/{skill.name}/'
        by_file = {}
        for m in GATE_FINDING.finditer(raw):
            path = m.group('path')
            if path.startswith(prefix):
                file_name = path.removeprefix(prefix)
            elif path.startswith(f'{skill.name}/'):
                file_name = path.removeprefix(f'{skill.name}/')
            else:
                file_name = path
            by_file.setdefault(file_name, set()).add(m.group('rule'))
        if by_file:
            parts = [f"{', '.join(sorted(by_file[f]))} in {f}" for f in sorted(by_file)]
            raise Refused('markdown: ' + '; '.join(parts))
        raise Refused('markdown')


def hiss_gate(root, staging, skill):
    """Run praetorctl baseline in the isolated staging directory for the skill."""
    if shutil.which('praetorctl') is None:
        raise RuntimeError('praetorctl missing')
    manifest = staging / '.standards.yaml'
    if not manifest.is_file():
        src = root / '.standards.yaml'
        if src.is_file():
            shutil.copyfile(src, manifest)
    baseline = staging / '.standards-baseline.json'
    if not baseline.is_file():
        src = root / '.standards-baseline.json'
        if src.is_file():
            shutil.copyfile(src, baseline)
        else:
            raise RuntimeError('.standards-baseline.json missing')
    cmd = ['praetorctl', 'baseline', '--verify', '--all-violations', '--file', str(baseline)]
    try:
        proc = subprocess.run(cmd, cwd=staging, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=600,
                              env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1'})
    except FileNotFoundError as error:
        raise RuntimeError('praetorctl missing') from error
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused('hiss') from error

    if proc.returncode:
        raw = ((proc.stderr or b'').decode('utf-8', errors='replace') + '\n' +
               (proc.stdout or b'').decode('utf-8', errors='replace'))
        prefix = f'skills/{skill.name}/'
        by_file = {}
        for m in HISS_FINDING.finditer(raw):
            path = m.group('path')
            if path.startswith(prefix):
                file_name = path.removeprefix(prefix)
            elif path.startswith(f'{skill.name}/'):
                file_name = path.removeprefix(f'{skill.name}/')
            else:
                file_name = path
            by_file.setdefault(file_name, set()).add(m.group('rule'))
        if by_file:
            first_file = next(iter(by_file))
            rules = sorted(by_file[first_file])
            raise Refused(f"hiss: {', '.join(rules)} in {first_file}")
        raise Refused('hiss')


def drop_file(skill: Path, rel: str) -> None:
    """Remove one staged file and the folders it leaves empty, bounded."""
    target = skill / rel
    target.unlink()
    parent = target.parent
    for _ in range(16):
        if parent == skill or not parent.is_dir() or any(parent.iterdir()):
            break
        parent.rmdir()
        parent = parent.parent


def patch_threads(patch_text: str, rel: str) -> list[str]:
    """Review thread ids named in the header lines before a patch's first diff line; a patch without any
    is refused, so every content edit stays tied to the finding it answers."""
    threads: list[str] = []
    for line in patch_text.splitlines()[:MAX_ROWS]:
        if line.startswith('--- '):
            break
        m = THREAD_HEADER.match(line.strip())
        if m:
            threads.extend(t.strip() for t in m.group(1).split(',')[:64] if t.strip())
    if not threads or not all(THREAD_ID.fullmatch(t) for t in threads):
        raise Refused(f'patch: {rel}')
    return threads


def apply_patch_text(text: str, patch_text: str, rel: str) -> tuple[str, list[str]]:
    """Apply one unified diff to text with git apply (exact context, no fuzz) in a scratch folder and
    return the result and the patch's thread ids; a patch that does not apply is refused by name."""
    threads = patch_threads(patch_text, rel)
    with tempfile.TemporaryDirectory(prefix='adopt-patch-') as tmp:
        target = Path(tmp) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        try:
            command(['git', 'apply', '--whitespace=nowarn', '-'], cwd=tmp, data=patch_text.encode('utf-8'))
            return target.read_text(encoding='utf-8'), threads
        except (Refused, OSError, UnicodeDecodeError, subprocess.SubprocessError) as error:
            raise Refused(f'patch: {rel}') from error


def apply_skill_patches(skill: Path, patch_dir: Path | None) -> dict[str, list[str]]:
    if patch_dir is None or not patch_dir.exists():
        return {}
    if patch_dir.is_symlink() or not patch_dir.is_dir():
        raise Refused(f'patch: {skill.name}')
    patched: dict[str, list[str]] = {}
    for entry in sorted(patch_dir.rglob('*'))[:MAX_ROWS]:
        rel_entry = entry.relative_to(patch_dir).as_posix()
        if entry.is_symlink():
            raise Refused(f'patch: {rel_entry}')
        if entry.is_dir():
            continue
        if not rel_entry.endswith('.patch'):
            raise Refused(f'patch: {rel_entry}')
        rel = str(safe_path(rel_entry.removesuffix('.patch')))
        target = skill / rel
        if not target.is_file():
            raise Refused(f'patch: {rel}')
        try:
            src_text = target.read_text(encoding='utf-8')
            patch_text = entry.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as error:
            raise Refused(f'patch: {rel}') from error
        new_text, threads = apply_patch_text(src_text, patch_text, rel)
        target.write_text(new_text, encoding='utf-8')
        patched[rel] = threads
    return patched


def _allowed_by_patch(rel: str, pin_text: str, norm_text: str, after: str, patch_dir: Path | None) -> bool:
    if patch_dir is None or ''.join(pin_text.split()) != ''.join(norm_text.split()):
        return False
    patch_file = patch_dir / f'{rel}.patch'
    if patch_file.is_symlink() or not patch_file.is_file():
        return False
    try:
        expected, _ = apply_patch_text(norm_text, patch_file.read_text(encoding='utf-8'), rel)
    except (OSError, UnicodeDecodeError, Refused):
        return False
    return after == expected


def whitespace_arbiter(skill: Path, before: dict[str, str], patch_dir: Path | None = None,
                       normalized_pin: dict[str, str] | None = None) -> list[str]:
    """Files the normalizer changed beyond whitespace (ruling peritia-code-review-skill-markdown-refinement-0013): each is
    dropped unless a listed patch in patch_dir exists and the file equals normalized-pin plus that patch."""
    dropped = []
    norm_map = normalized_pin if normalized_pin is not None else before
    for rel, text in sorted(before.items())[:MAX_FILES]:
        target = skill / rel
        after = target.read_text(encoding='utf-8') if target.is_file() else ''
        if ''.join(text.split()) == ''.join(after.split()):
            continue
        if _allowed_by_patch(rel, text, norm_map.get(rel, text), after, patch_dir):
            continue
        drop_file(skill, rel)
        dropped.append(rel)
    return dropped


def normalize_skill_markdown(root: Path, skill: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Normalise eligible staged Markdown and apply listed review patches."""
    tool_dir = root / 'tools/markdownlint' if root is not None else Path(__file__).resolve().parent / 'markdownlint'
    if not tool_dir.exists():
        tool_dir = Path(__file__).resolve().parent / 'markdownlint'
    script = tool_dir / 'normalize.mjs'
    if not script.is_file():
        raise Refused('markdown-normalize')
    patch_dir = (root / 'tools/skill-patches' / skill.name) if root is not None else None
    files = [
        path.relative_to(skill).as_posix()
        for path in sorted(skill.rglob('*'))[:MAX_ROWS]
        if _is_skill_markdown(path, skill)
    ]
    if len(files) > MAX_FILES:
        raise Refused('file-limit: more than 200 files')
    if not files:
        patched = apply_skill_patches(skill, patch_dir)
        return {}, patched
    before = {rel: (skill / rel).read_text(encoding='utf-8') for rel in files}
    try:
        raw = command(['node', str(script), *files], cwd=skill, timeout=TIMEOUT)
        parsed = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Refused('markdown-normalize') from error
    if not isinstance(parsed, dict):
        raise Refused('markdown-normalize')
    normalized = {str(k): [str(r) for r in v] for k, v in parsed.items() if isinstance(v, list) and v}
    normalized_pin = {rel: (skill / rel).read_text(encoding='utf-8') for rel in files}
    patched = apply_skill_patches(skill, patch_dir)
    for rel in whitespace_arbiter(skill, before, patch_dir=patch_dir, normalized_pin=normalized_pin):
        normalized[rel] = ['changed beyond whitespace, omitted']
    return normalized, patched


def drop_gate_refused(skill: Path, message: str, dropped: dict[str, str]) -> bool:
    pairs = re.findall(r'([A-Za-z0-9_,\s-]+) in (\S+)', message)
    if not pairs:
        return False
    for rules, relpath in pairs[:MAX_FILES]:
        rel = relpath.rstrip(';')
        if rel == 'SKILL.md':
            return False
        if not (skill / rel).is_file():
            return False
        drop_file(skill, rel)
        dropped[rel] = rules.strip()
    return True


def build_notes(skill: Path, filter_notes: list[str], normalized: dict[str, list[str]],
                dropped_md: dict[str, str], dropped_hiss: dict[str, str], lic_note: str,
                patched: dict[str, list[str]] | None = None) -> str:
    notes = list(filter_notes)
    active_norm = {rel: rules for rel, rules in sorted(normalized.items()) if (skill / rel).is_file()}
    beyond = sorted(rel for rel, rules in normalized.items() if rules == ['changed beyond whitespace, omitted'])
    if beyond:
        notes.append(f"omitted files the whitespace normalizer would change beyond whitespace ({', '.join(beyond)})")
    if active_norm:
        items = ', '.join(f"{rel} ({', '.join(rules)})" for rel, rules in active_norm.items())
        notes.append(f'normalized Markdown whitespace ({items})')
    active_patched = {rel: threads for rel, threads in sorted((patched or {}).items()) if (skill / rel).is_file()}
    if active_patched:
        items = ', '.join(f"{rel} ({', '.join(threads)})" for rel, threads in active_patched.items())
        notes.append(f'applied review patches ({items})')
    if dropped_md:
        items = ', '.join(f'{f} ({r})' for f, r in sorted(dropped_md.items()))
        notes.append(f'omitted files refused by markdown gate ({items})')
    if dropped_hiss:
        items = ', '.join(f'{f} ({r})' for f, r in sorted(dropped_hiss.items()))
        notes.append(f'omitted files refused by HISS gate ({items})')
    if lic_note:
        notes.append(lic_note)
    return '; '.join(notes)


@dataclass(frozen=True)
class GateOptions:
    """Configured gate callbacks for staged skill validation."""
    gate: object = None
    hiss_gate: object = None


def run_gates(root, staging, skill, row, folder, pin, state, gates: GateOptions):
    filter_notes, normalized, lic_note, fenced_files, patched = state
    dropped_md, dropped_hiss = {}, {}
    for step in range(8):
        try:
            if gates.gate:
                gates.gate(root, staging, skill)
            if gates.hiss_gate:
                gates.hiss_gate(root, staging, skill)
            break
        except Refused as error:
            msg = str(error)
            target_map = dropped_md if msg.startswith('markdown:') else dropped_hiss
            if str(folder) != '.' or step == 7 or not drop_gate_refused(skill, msg, target_map):
                raise
            note = build_notes(skill, filter_notes, normalized, dropped_md, dropped_hiss, lic_note, patched=patched)
            fence_changed = any((skill / rel).is_file() for rel in fenced_files)
            attribution(skill, row, folder, pin, note, fence_changed=fence_changed)
            git(staging, 'add', '-A', '--', 'skills')


@dataclass(frozen=True)
class StageInputs:
    """Shared inputs for staging the candidates in a batch."""

    checkouts: Path
    root: Path
    occupied: set[str]
    apply: bool
    reader: Callable
    template_cache: Path
    runner: Callable
    licence_scan: check_skills.LicenceScan


def _check_staged_skill(skill: Path, template_dir: Path, licence_scan: check_skills.LicenceScan) -> None:
    """Apply the shared checker with a fresh copy of the reviewed licence entries."""
    problems = check_skills.skill_problems(skill, template_dir=template_dir, licence_scan=licence_scan.copy())
    if problems:
        raise Refused('skill-check: ' + '; '.join(problems))


def stage_one(row, pin, staging, inputs: StageInputs, spdx: str):
    """Stage one skill and locate its canonical SPDX template."""
    checkout, folder = checkout_for(inputs.checkouts, row, pin)
    name = choose_name(row, inputs.occupied)
    if inputs.apply:
        expand_sparse(checkout, folder)
    raw_entries = tree_files(checkout, pin, folder)
    entries, filter_notes = filter_entries(raw_entries, folder)
    validate_files(entries, allow_missing=not inputs.apply)
    skill = staging / 'skills' / name
    skill.mkdir(parents=True)
    copy_blobs(checkout, entries, skill, allow_fetch=not inputs.apply)
    fenced_files = rewrite_skill_fences(skill)
    normalized, patched = normalize_skill_markdown(inputs.root, skill)
    lic_note = licence_text(row, checkout, pin, skill, inputs)
    note = build_notes(skill, filter_notes, normalized, {}, {}, lic_note, patched=patched)
    attribution(skill, row, folder, pin, note, fence_changed=bool(fenced_files))
    template_dir = _stage_licence_template(inputs.root, inputs.template_cache, spdx, inputs.runner)
    year = git(checkout, 'show', '-s', '--format=%cs', pin).decode().strip()[:4]
    return skill, year, folder, (filter_notes, normalized, lic_note, fenced_files, patched), template_dir


def initial_metadata(root):
    """Read the REUSE and credits files and validate the REUSE TOML."""
    reuse = (root / REUSE_FILE).read_text(encoding='utf-8')
    tomllib.loads(reuse)
    credits_text = (root / CREDITS_FILE).read_text(encoding='utf-8')
    return reuse, credits_text


def add_metadata(reuse, credits_text, row, name, year):
    """Add a skill's REUSE annotation and update its upstream credits row."""
    spdx = candidate_licence_id(row['licence'])
    path_line = f'path = ["skills/{name}/**"]\n'
    annotation = ('\n[[annotations]]\n' + path_line +
                  'precedence = "override"\n' +
                  f'SPDX-FileCopyrightText = {json.dumps(year + " " + row["source"].split("/")[0] + " and contributors")}\n' +
                  f'SPDX-License-Identifier = {json.dumps(spdx)}\n')
    if path_line not in reuse:
        reuse = reuse + annotation
    url = f'https://github.com/{row["source"]}'
    lines = credits_text.splitlines()
    found = False
    for index, line in enumerate(lines):
        cells = line.split('|')
        if len(cells) == 5 and re.search(re.escape(url) + r'(?=[>\s)])', cells[1], re.IGNORECASE):
            licences = sorted(set(cells[2].strip().split(', ')) | {spdx})
            names = sorted(set(re.findall(r'`([^`]+)`', cells[3])) | {name})
            lines[index] = f'| <{url}> | {", ".join(licences)} | ' + ', '.join(f'`{n}`' for n in names) + ' |'
            found = True
            break
    if not found:
        lines.append(f'| <{url}> | {spdx} | `{name}` |')
    return reuse, '\n'.join(lines) + '\n'


def report_text(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, REPORT_FIELDS, delimiter='\t', lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def summary(rows, candidates):
    counts = Counter((candidate['functions'].split(';')[0] or 'none', row['verdict'])
                     for candidate, row in zip(candidates, rows))
    return '\n'.join(f'function {function}: ' + ', '.join(
        f'{verdict}={counts[function, verdict]}' for verdict in ('adopted', 'present', 'refused', 'skipped-grouped'))
        for function in sorted({function for function, _ in counts}))


def prepare_staging(root, staging):
    git(staging, 'init', '--quiet')
    for name in ('.standards.yaml', '.standards-baseline.json'):
        manifest = root / name
        if manifest.is_file():
            shutil.copyfile(manifest, staging / name)


def extract_source(origin_path: Path) -> str:
    if not origin_path.is_file():
        return 'peritia'
    rec = check_skills.fields(origin_path.read_text(encoding='utf-8'))
    upstream = rec.get('upstream', '').strip().strip('<>').strip()
    if not upstream or upstream == 'own':
        return 'peritia'
    m = re.search(r'github\.com/([^/]+/[^/]+?)(?:\.git|/|\Z)', upstream)
    if m:
        return m.group(1)
    if REPOSITORY.fullmatch(upstream):
        return upstream
    return upstream or 'peritia'


def _origin_folder(path: str) -> PurePosixPath:
    """Derive the indexed folder from a source folder or SKILL.md path."""
    target = PurePosixPath(path)
    return target.parent if target.name == 'SKILL.md' else target


def read_present_skills(skills_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Index already-adopted skills by normalized upstream repository and recorded origin folder."""
    if not skills_dir.is_dir():
        return {}
    present = {}
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        origin_file = d / 'ORIGIN.md'
        if not origin_file.is_file():
            continue
        try:
            origin_text = origin_file.read_text(encoding='utf-8')
            rec = check_skills.fields(origin_text)
            upstream = extract_source(origin_file)
            path_match = re.search(r'^path:[ \t]*(.*)$', origin_text, re.MULTILINE)
            raw_path = path_match.group(1).strip() if path_match else ''
            if not upstream or upstream == 'peritia' or not raw_path:
                continue
            origin_path = str(_origin_folder(raw_path))
            commit = rec.get('commit', '').strip()
            key = (upstream.lower(), origin_path)
            if key not in present:
                present[key] = {
                    'directory': f'skills/{d.name}',
                    'commit': commit,
                    'name': d.name,
                }
        except (OSError, UnicodeDecodeError):
            continue
    return present


def candidate_folder(row: dict) -> str | None:
    try:
        path = safe_path(row['path'])
        if path.name != 'SKILL.md':
            return None
        return str(path.parent)
    except Refused:
        return None


def _skill_directories(skills_dir: Path, staged_skills: Path | None = None) -> list[Path]:
    """List existing and newly staged skill folders without overwriting existing names."""
    by_name: dict[str, Path] = {}
    for folder in (skills_dir, staged_skills):
        if folder is not None and folder.is_dir():
            for path in sorted(p for p in folder.iterdir() if p.is_dir())[:check_skills.MAX_SKILLS]:
                by_name.setdefault(path.name, path)
    return [by_name[name] for name in sorted(by_name)[:check_skills.MAX_SKILLS]]


def _index_destination(root: Path, target: Path | None, staged_skills: Path | None) -> Path | None:
    """Return the index path to write when existing or staged skills are present."""
    if not (root / 'skills').is_dir() and not (staged_skills is not None and staged_skills.is_dir()):
        return None
    return (root / 'skills' / 'INDEX.tsv') if target is None else target


def rebuild_index(root: Path, adopted_map: dict[str, dict] | None = None,
                  group_map: dict[str, str] | None = None, *,
                  staged_skills: Path | None = None) -> list[dict[str, str]]:
    """Build index rows for the skill folders under root and, when given, the staged ones."""
    skills_dir = root / SKILLS_DIR
    dirs = _skill_directories(skills_dir, staged_skills)
    if not dirs:
        return []
    index_file = root / INDEX_FILE
    existing = {}
    if index_file.is_file():
        try:
            with index_file.open(encoding='utf-8', newline='') as handle:
                reader = csv.DictReader(handle, delimiter='\t')
                for row in reader:
                    if row.get('skill'):
                        existing[row['skill']] = row
        except Exception:
            existing = {}

    adopted_map = adopted_map or {}
    group_map = group_map or {}

    rows = []
    for d in dirs:
        name = d.name
        source = extract_source(d / 'ORIGIN.md')
        if name in adopted_map:
            cand = adopted_map[name]
            functions = cand.get('functions', '')
            domains = cand.get('domains', '')
            cid = candidate_id(cand)
            overlap_group = group_map.get(cid, '')
        elif name in existing:
            prev = existing[name]
            functions = prev.get('functions', '')
            domains = prev.get('domains', '')
            overlap_group = group_map.get(f'skills/{name}', group_map.get(name, prev.get('overlap_group', '')))
        else:
            functions = ''
            domains = ''
            overlap_group = group_map.get(f'skills/{name}', group_map.get(name, ''))

        rows.append({
            'skill': name,
            'source': source,
            'functions': functions,
            'domains': domains,
            'overlap_group': overlap_group,
        })
    rows.sort(key=lambda r: r['skill'])
    return rows


def write_index(root: Path, adopted_map: dict[str, dict] | None = None,
                group_map: dict[str, str] | None = None, *,
                target: Path | None = None, staged_skills: Path | None = None) -> bool:
    """Write skills/INDEX.tsv, or target, and report whether an index was written."""
    index_file = _index_destination(root, target, staged_skills)
    if index_file is None:
        return False
    rows = rebuild_index(root, adopted_map, group_map, staged_skills=staged_skills)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    with index_file.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS, delimiter='\t', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    return True


def _pinned_member_text(checkout: Path, pin: str | None, path: str) -> str:
    """Read the pinned skill up to MAX_BYTES when a sparse checkout omits its working-tree file."""
    if not pin or not checkout.is_dir():
        return ''
    spec = f'{pin}:{path}'
    try:
        size = int(git(checkout, 'cat-file', '-s', spec).decode('utf-8').strip())
        if size < 0 or size > MAX_BYTES:
            return ''
        data = git(checkout, 'show', spec)
        return data.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')[:overlap.MAX_SKILL_CHARS]
    except (Refused, OSError, ValueError, subprocess.SubprocessError):
        return ''


def _adopted_member_text(skills_dir: Path, source: str, path: str) -> str:
    """Read an in-tree adopted member when its source text is unavailable."""
    raw_path = path.strip()
    if not raw_path or not skills_dir.is_dir():
        return ''
    folder = _origin_folder(raw_path)
    present = read_present_skills(skills_dir).get((source.lower(), str(folder)))
    if present is None:
        return ''
    try:
        resolved_skills = skills_dir.resolve()
        skill_md = (skills_dir / present['name'] / 'SKILL.md').resolve()
        if not skill_md.is_relative_to(resolved_skills) or not skill_md.is_file():
            return ''
        with skill_md.open(encoding='utf-8', errors='replace') as handle:
            return handle.read(overlap.MAX_SKILL_CHARS)
    except OSError:
        return ''


def get_member_skill_text(member: str, checkouts: Path, skills_dir: Path, pins: dict[str, str] | None = None) -> str:
    """Read the member's overlap input from its checkout, pinned blob, or adopted copy."""
    text = get_skill_text(member, checkouts, skills_dir, max_chars=overlap.MAX_SKILL_CHARS)
    if text or ':' not in member:
        return text
    source, path = member.split(':', 1)
    checkout = checkouts / source.replace('/', '__')
    pinned_text = _pinned_member_text(checkout, (pins or {}).get(source), path)
    return pinned_text or _adopted_member_text(skills_dir, source, path)


def _copy_ranks(sources):
    """Load the source ranks used to select original skills."""
    if isinstance(sources, dict):
        return dict(sources)
    path = Path(sources) if sources is not None else Path(__file__).resolve().parent / 'census-sources.json'
    return {repository: source.rank for source in load_sources(path)
            for repository in (source.repository, source.repository.lower())}


def _copy_item(member: str, candidate: dict | None, text: str, skills_dir: Path) -> overlap.Item:
    """Build an overlap item using the same preprocessing as the cache producer."""
    if is_existing_skill(member):
        rel = member.removeprefix('skills/').strip('/')
        path = skills_dir / rel / 'SKILL.md'
        blob_id = git_blob_id(path.read_bytes()) if path.is_file() else ''
        return overlap.item_from_skill(member, blob_id, rel, text, is_existing=True)
    candidate = candidate or {}
    blob_id = candidate.get('blob_id', '').strip()
    if not blob_id and text:
        blob_id = git_blob_id(text.encode())
    return overlap.item_from_skill(member, blob_id, candidate.get('skill_name', ''), text)


@dataclass(frozen=True)
class CopyEmbeddingInputs:
    """Cache identity for grouped adoption."""

    cache: Path | None
    model: str
    endpoint: str
    dims: int | None
    refill_paths: tuple[Path, Path, Path] | None = None
    judge_model: str = ''

    def refill_command(self) -> str:
        """Show which overlap command populates the needed cache."""
        args = ['python3', 'tools/overlap.py', 'run', '--cache', str(self.cache or 'embed-cache.json'),
                '--embed-model', self.model or 'MODEL']
        if self.endpoint:
            args += ['--base-url', provider_id(self.endpoint)]
        if self.dims is not None:
            args += ['--embed-dims', str(self.dims)]
        if self.refill_paths is not None:
            for option, path in zip(('--candidates', '--checkouts', '--skills-dir'), self.refill_paths, strict=True):
                args += [option, str(path)]
        env_judge = os.environ.get('PERITIA_LLM_JUDGE_MODEL', '')
        if self.judge_model and not env_judge:
            args += ['--judge-model', self.judge_model]
        cmd = shlex.join(args)
        if not self.judge_model and not env_judge:
            cmd += ' --judge-model <model>'
        return cmd

    def require_cache(self) -> Path:
        """Require a cache and model only when grouped adoption reads embeddings."""
        if self.cache is None or not self.model:
            raise ValueError(f'--include-grouped requires --cache and --embed-model; run {self.refill_command()}')
        return self.cache


def _copy_embedding_inputs(cache, model, base_url, embed_base_url, dims, include_grouped, *, refill_paths=None):
    """Resolve the embedding endpoint only for grouped adoption or explicit options."""
    if base_url == '':
        validate_base_url(base_url, '--base-url')
    _, endpoint = resolve_base_urls(base_url, embed_base_url, inherit=include_grouped and embed_base_url is None)
    embedding = CopyEmbeddingInputs(Path(cache) if cache is not None else None,
                                    model or os.environ.get('PERITIA_LLM_EMBED_MODEL', ''),
                                    endpoint, dims, refill_paths)
    if include_grouped:
        embedding.require_cache()
    return embedding


def _group_copy_decisions(group, items, metadata, embeddings, ranks, embedding: CopyEmbeddingInputs):
    """Defer adoption where current vectors or explicit provenance cannot support it."""
    members = [m.strip() for m in group['members'].split(',') if m.strip()]
    missing = [m for m in members if m not in embeddings]
    if not missing:
        return collapse_copies(members, items, metadata, embeddings, ranks)[1]

    group_id = group.get('group_id', '')
    print(
        f"group {group_id}: missing embedding vectors for {', '.join(missing)}; "
        f"run {embedding.refill_command()} "
        "(files absent from the working tree are skipped by overlap; restore only the listed member path at the "
        "census pin, e.g. git -C <checkout> checkout --ignore-skip-worktree-bits <pin> -- <path>, before "
        "rerunning; checkouts that contain the files can run the printed command unchanged)",
        file=sys.stderr,
    )
    _, copies = collapse_copies(members, items, metadata, {}, ranks)
    originals = {reason.removeprefix('copy-of:') for reason in copies.values()}
    return {**dict.fromkeys((m for m in members if m not in originals), 'missing-embedding-vector'), **copies}


def resolve_copies(group_rows, checkouts, root, pins, all_candidates, sources, embedding: CopyEmbeddingInputs):
    """Resolve grouped copies using provenance and embeddings bound to the current inputs."""
    cache_path = embedding.require_cache()
    if not cache_path.is_file():
        raise ValueError(f'embedding cache not found: {cache_path}; run {embedding.refill_command()}')
    ranks = _copy_ranks(sources)
    cand_by_cid = {candidate_id(r): r for r in all_candidates}
    all_members = {m.strip() for grow in group_rows for m in grow['members'].split(',') if m.strip()}
    items: dict[str, overlap.Item] = {}
    metadata: dict[str, dict[str, str]] = {}
    skills_dir = root / 'skills'
    for m in all_members:
        text = get_member_skill_text(m, checkouts, skills_dir, pins)
        metadata[m] = front_matter_or_empty(text)
        if text:
            items[m] = _copy_item(m, cand_by_cid.get(m), text, skills_dir)
    try:
        embeddings = overlap.load_embeddings(cache_path, list(items.values()), embedding.model, embedding.endpoint,
                                             embedding.dims, require_namespace=True)
    except overlap.MissingEmbeddingNamespace as error:
        raise ValueError(f'{error}; run {embedding.refill_command()}') from error

    copies_map = {}
    for grow in group_rows:
        copies_map.update(_group_copy_decisions(grow, items, metadata, embeddings, ranks, embedding))
    return copies_map


@dataclass(frozen=True)
class EvaluationInputs:
    """Census, grouping and gate settings used to evaluate each candidate."""

    grouped: set[str]
    order: dict[str, int]
    pins: dict[str, str]
    include_grouped: bool
    copies: dict[str, str]
    present: dict[tuple[str, str], dict[str, str]]
    gates: GateOptions


def _present_result(row, result, inputs: EvaluationInputs) -> bool:
    """Record whether the source folder has already been adopted."""
    if not inputs.present or (folder := candidate_folder(row)) is None:
        return False
    info = inputs.present.get((row['source'].lower(), folder))
    if info is None:
        return False
    old_commit = info['commit']
    new_commit = inputs.pins.get(row['source'], row.get('pin', ''))
    reason = '' if old_commit == new_commit else f'present: pinned {old_commit[:12]}, census {new_commit[:12]}'
    result.update(verdict='present', directory=info['directory'], reason=reason)
    return True


def _staged_notice_scan(
    skill: Path, template_dir: Path, spdx: str, scan: check_skills.LicenceScan,
) -> tuple[check_skills.LicenceScan, frozenset[str]]:
    """Include proposed notices in a candidate-local scan until it passes the gates."""
    staged_scan = scan.copy()
    path = find_folder_licence_file(skill) or (skill / 'LICENSE')
    new = unreviewed_licence_notices(path, template_dir, spdx, scan.notices)
    if new:
        try:
            staged_scan.notices = check_skills.validate_licence_notices(sorted(scan.notices | new))
        except (TypeError, ValueError) as error:
            raise Refused(f'{LICENCE_NOTICES_FILE}: {error}') from error
    return staged_scan, new


def evaluate(row, staging, stage: StageInputs, inputs: EvaluationInputs):
    """Stage and verify a candidate, returning its adoption report row."""
    result = dict(zip(REPORT_FIELDS, (candidate_id(row), 'refused', '', '')))
    cid = result['candidate']
    if inputs.include_grouped and cid in inputs.copies:
        result.update(verdict='refused', reason=inputs.copies[cid])
        return result, None, ''
    if not inputs.include_grouped and cid in inputs.grouped:
        result.update(verdict='skipped-grouped', reason='awaiting-group-judge')
        return result, None, ''
    try:
        if cid not in inputs.order:
            raise Refused('candidate-not-in-census')
        if _present_result(row, result, inputs):
            return result, None, ''
        if len(stage.occupied) >= check_skills.MAX_SKILLS:
            raise Refused('skill-count-limit')
        pin = inputs.pins[row['source']]
        spdx = candidate_licence_id(row['licence'])
        if spdx is None:
            raise Refused(f"licence {row['licence']} not recognised")
        skill, year, folder, state, template_dir = stage_one(row, pin, staging, stage, spdx)
        staged_scan, notices = _staged_notice_scan(skill, template_dir, spdx, stage.licence_scan)
        _check_staged_skill(skill, template_dir, staged_scan)
        run_gates(stage.root, staging, skill, row, folder, pin, state, inputs.gates)
        # A future gate fixer must preserve the checker contract too.
        _check_staged_skill(skill, template_dir, staged_scan)
        result.update(verdict='adopted', directory=f'{SKILLS_DIR}/{skill.name}')
        if notices:
            result['reason'] = 'licence notices added: ' + '; '.join(sorted(notices))
            stage.licence_scan.notices = staged_scan.notices
        return result, skill, year
    except SourceReadError as error:
        detail = ' '.join(str(error).split())
        result['reason'] = f'licence-text-missing: {detail}' if detail else 'licence-text-missing'
        return result, None, ''
    except _LicenceOutputSymlinkError as error:
        if stage.apply:
            raise
        result['reason'] = str(error)
        return result, None, ''
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result['reason'] = ' '.join(str(error).split())
        return result, None, ''


def _called_process_detail(error: subprocess.CalledProcessError) -> str:
    """Extract a bounded message from a failed subprocess exception."""
    raw = error.stderr.decode('utf-8', errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
    return raw.strip()[:500] or ' '.join(str(error).split())[:500]


def _download_error(args, error: Exception) -> _LicenceDownloadError:
    """Format a licence download failure naming the SPDX identifier and cause."""
    spdx_id = args[-1] if args else 'unknown'
    prefix = f'licence download failed for {spdx_id}'
    if isinstance(error, ValueError) and str(error).startswith(f'{prefix}:'):
        return _LicenceDownloadError(str(error))
    if isinstance(error, subprocess.TimeoutExpired):
        limit = TIMEOUT if error.timeout is None else error.timeout
        detail = f'timed out after {limit:g} seconds'
    elif isinstance(error, Refused):
        prog = args[0] if args else ''
        detail = str(error).removeprefix(f'command: {prog}: ').strip() or 'non-zero exit status'
    elif isinstance(error, subprocess.CalledProcessError):
        detail = _called_process_detail(error)
    else:
        detail = ' '.join(str(error).split())[:500] or type(error).__name__
    return _LicenceDownloadError(f'{prefix}: {detail}')


def default_download_runner(args, *, cwd=None):
    """Run REUSE with the module deadline and report the SPDX identifier on failure."""
    if not args:
        raise _LicenceDownloadError('licence download failed for unknown')
    try:
        return command(args, cwd=cwd, timeout=TIMEOUT)
    except (Refused, subprocess.SubprocessError, OSError) as error:
        raise _download_error(args, error) from error


def _download_licence(root: Path, spdx: str, runner) -> Path:
    """Download one SPDX template to an explicit destination and require a regular file."""
    target = root / LICENCES_DIR / f'{spdx}.txt'
    args = ['reuse', 'download', '-o', str(target), spdx]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        runner(args, cwd=root)
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError(f'{target.name} not written')
    except Exception as error:
        with suppress(OSError):
            target.unlink(missing_ok=True)
        if isinstance(error, _LicenceDownloadError):
            raise
        raise _download_error(args, error) from error
    return target


def _stage_licence_template(root: Path, cache: Path, spdx: str, runner) -> Path:
    """Use a local SPDX template or download it once into the staging cache."""
    filename = f'{spdx}.txt'
    if _licence_target_present(root, filename):
        return root / LICENCES_DIR
    if not _licence_target_present(cache, filename):
        _download_licence(cache, spdx, runner)
    return cache / LICENCES_DIR


def _licence_target_present(root: Path, filename: str) -> bool:
    """Return True when root/LICENSES/filename is a regular file; refuse any symlink."""
    target = root / LICENCES_DIR / filename
    if target.is_symlink():
        raise _LicenceOutputSymlinkError(f'{LICENCES_DIR}/{filename}: output symlink refused')
    return target.is_file()


def _missing_spdx_ids(root: Path, reuse_text: str) -> list[str]:
    """List sorted SPDX identifiers in REUSE.toml that root/LICENSES lacks."""
    spdx_ids = set()
    for anno in tomllib.loads(reuse_text).get('annotations', [])[:MAX_ROWS]:
        val = anno.get('SPDX-License-Identifier')
        if val:
            for token in re.findall(r'[A-Za-z0-9.-]+', val)[:64]:
                if token not in ('AND', 'OR', 'WITH'):
                    spdx_ids.add(token)
    return [spdx_id for spdx_id in sorted(spdx_ids)[:MAX_ROWS]
            if not _licence_target_present(root, f'{spdx_id}.txt')]


def sync_licences(root: Path, reuse_text: str, runner=default_download_runner, *,
                  staging: Path | None = None, template_cache: Path | None = None) -> list[Path]:
    """Stage missing REUSE licence texts using verified cached bytes when available."""
    dest_root = root if staging is None else staging
    missing = _missing_spdx_ids(root, reuse_text)
    if not missing:
        return []
    licenses_dir = dest_root / LICENCES_DIR
    created_dir = not licenses_dir.exists()
    written = []
    try:
        for spdx_id in missing[:MAX_ROWS]:
            filename = f'{spdx_id}.txt'
            target = licenses_dir / filename
            if template_cache is not None and _licence_target_present(template_cache, filename):
                cached = template_cache / LICENCES_DIR / filename
                if cached != target:
                    licenses_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(cached, target)
            else:
                _download_licence(dest_root, spdx_id, runner)
            written.append(target)
    finally:
        if created_dir and licenses_dir.is_dir() and not any(licenses_dir.iterdir()):
            licenses_dir.rmdir()
    return written


# Root metadata files publish stages and moves like every other output.
METADATA_FILES = (REUSE_FILE, CREDITS_FILE)
APPLY_LOCK_PREFIX = '.adopt-skills'
APPLY_LOCK_NAME = f'{APPLY_LOCK_PREFIX}.lock'
JOURNAL_NAME = '.adopt-publish.journal.json'
JOURNAL_BACKUP_TARGETS = (*METADATA_FILES, INDEX_FILE)
JOURNAL_TARGETS_WITH_NOTICES = (*JOURNAL_BACKUP_TARGETS, LICENCE_NOTICES_FILE)
REFUSED_REPORT_TARGETS = (JOURNAL_NAME, APPLY_LOCK_NAME, *JOURNAL_BACKUP_TARGETS)
OUTPUT_SYMLINK_TARGETS = (*PUBLICATION_TREES, *PUBLICATION_FILES)


class _PublishRollbackError(RuntimeError):
    """Raised when one or more undo steps fail while rolling back a failed publish."""


def _skill_exists_error(dest: Path) -> FileExistsError:
    """Build the FileExistsError raised when a target skill directory already exists."""
    return FileExistsError(f'{dest}: skill directory already exists')


def _output_symlink_error(name: str) -> ValueError:
    """Build the ValueError raised when an output target is a symbolic link."""
    if name == 'report':
        return ValueError('report output symlink refused')
    return ValueError(f'{name}: output symlink refused')


def _journal_error(journal_path: Path, detail: str) -> ValueError:
    """Build a ValueError naming the publish journal and why it was refused."""
    return ValueError(f'{journal_path}: {detail}')


def _stage_publish(root: Path, staging: Path, saved: Path, rows: list[dict],
                   metadata: dict[str, str], adopted_map, group_map,
                   runner, *, template_cache: Path | None = None) -> tuple[list[Path], list[Path], bool]:
    """Stage adopted skills, metadata, index and downloaded licences before touching root."""
    staged_skills = []
    for row in rows[:MAX_ROWS]:
        if row['verdict'] != 'adopted':
            continue
        dest = root / row['directory']
        if dest.exists() or dest.is_symlink():
            raise _skill_exists_error(dest)
        target = staging / 'skills' / Path(row['directory']).name
        shutil.copytree(saved / target.name, target)
        staged_skills.append(target)
    for name in METADATA_FILES:
        _staged_target_path(staging, root / name).write_text(metadata[name], encoding='utf-8')
    staged_licences = sync_licences(root, metadata[REUSE_FILE], runner=runner,
                                    staging=staging, template_cache=template_cache)
    index_staged = write_index(
        root, adopted_map=adopted_map, group_map=group_map,
        target=_staged_target_path(staging, root / 'skills/INDEX.tsv'), staged_skills=staging / 'skills',
    )
    return staged_licences, staged_skills, index_staged


def _backup_slot_path(staging: Path, index: int, target: Path) -> Path:
    """Return the canonical staged backup path for one JOURNAL_BACKUP_TARGETS slot."""
    return staging / 'backup' / f'{index}-{target.name}'


def _staged_target_path(staging: Path, target: Path) -> Path:
    """Return the canonical staged replacement path for one JOURNAL_BACKUP_TARGETS entry."""
    return staging / target.name


def _backup_publish_targets(root: Path, staging: Path) -> dict[Path, Path | None]:
    """Copy existing root metadata and index files into staging before replacing them."""
    backup_dir = staging / 'backup'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path | None] = {}
    targets = (
        JOURNAL_TARGETS_WITH_NOTICES
        if _staged_target_path(staging, root / LICENCE_NOTICES_FILE).is_file()
        else JOURNAL_BACKUP_TARGETS
    )
    for idx, rel in enumerate(targets):
        target = root / rel
        if target.is_file():
            saved = _backup_slot_path(staging, idx, target)
            shutil.copy2(target, saved)
            backups[target] = saved
        else:
            backups[target] = None
    return backups


def _restore_backup_file(backup: Path, target: Path) -> None:
    """Stage a copy in backup.parent and atomically replace target while keeping backup."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=backup.parent, prefix=f'.{target.name}.restore.', suffix='.tmp')
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(backup.read_bytes())
        shutil.copystat(backup, tmp_path)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _staged_rename_ran(source: Path | None) -> bool:
    """Return True only when a staged replacement had a source path and that source is gone."""
    return source is not None and not (source.exists() or source.is_symlink())


def _apply_undo_step(kind: str, target: Path, other: Path | None) -> None:
    """Undo one recorded filesystem change under root."""
    if kind == 'move' and other is not None:
        if (target.exists() or target.is_symlink()) and _staged_rename_ran(other):
            os.replace(target, other)
    elif kind == 'restore':
        if other is not None:
            _restore_backup_file(other, target)
        else:
            target.unlink(missing_ok=True)
    elif kind == 'unlink':
        target.unlink(missing_ok=True)
    elif kind == 'rmdir' and target.is_dir() and not any(target.iterdir()):
        target.rmdir()


def _rollback_publish(
    undo: list[tuple[str, Path, Path | None]],
) -> list[tuple[str, Path, Path | None]]:
    """Revert recorded publish steps in reverse order and return any steps that failed."""
    failures: list[tuple[str, Path, Path | None]] = []
    for kind, target, other in reversed(undo):
        try:
            _apply_undo_step(kind, target, other)
        except OSError:
            failures.append((kind, target, other))
    return failures


def _format_rollback_failure(kind: str, target: Path, backup: Path | None) -> str:
    """Describe one failed undo step with its target and backup path."""
    if backup is not None:
        return f'{kind} {target} (restore from {backup})'
    return f'{kind} {target} (backup: none)'


def _rollback_error(
    staging: Path,
    failures: list[tuple[str, Path, Path | None]],
    journal_path: Path | None = None,
) -> _PublishRollbackError:
    """Build an error naming the preserved staging folder, optional journal, and failed undo steps."""
    where = f'staging kept at {staging}'
    if journal_path is not None:
        where = f'journal at {journal_path}, {where}'
    if not failures:
        return _PublishRollbackError(f'publish rollback failed ({where}): could not remove publish journal')
    details = '; '.join(_format_rollback_failure(kind, target, other) for kind, target, other in failures)
    return _PublishRollbackError(f'publish rollback failed ({where}): {details}')


def _ensure_publish_dir(folder: Path, undo: list[tuple[str, Path, Path | None]]) -> None:
    """Create an output directory when absent and record its removal for rollback."""
    if not folder.exists():
        undo.append(('rmdir', folder, None))
        folder.mkdir(parents=True, exist_ok=True)


def _journal_backup_record(
    root: Path, staging: Path, rel: str, backup: Path | None, index_staged: bool,
) -> dict[str, str | None]:
    """Build one backup-and-source record for JOURNAL_BACKUP_TARGETS[rel]."""
    staged = _staged_target_path(staging, root / rel) if rel != INDEX_FILE or index_staged else None
    return {
        'backup': backup.relative_to(root).as_posix() if backup is not None else None,
        'source': staged.relative_to(root).as_posix() if staged is not None else None,
    }


def _build_publish_journal(
    root: Path,
    staging: Path,
    backups: dict[Path, Path | None],
    licences: list[Path],
    skills: list[Path],
    index_staged: bool,
) -> dict:
    """Describe the staging directory, backups, planned moves and new directories relative to root."""
    backup_map = {
        rel: _journal_backup_record(root, staging, rel, backups[root / rel], index_staged)
        for rel in JOURNAL_TARGETS_WITH_NOTICES if root / rel in backups
    }
    moves = [
        {
            'kind': 'licence',
            'source': lic.relative_to(root).as_posix(),
            'destination': f'LICENSES/{lic.name}',
        }
        for lic in licences[:MAX_ROWS]
    ]
    moves.extend(
        {
            'kind': 'skill',
            'source': skill.relative_to(root).as_posix(),
            'destination': f'skills/{skill.name}',
        }
        for skill in skills[:check_skills.MAX_SKILLS]
    )
    created_dirs = []
    if licences and not (root / 'LICENSES').exists():
        created_dirs.append('LICENSES')
    if (index_staged or skills or root / LICENCE_NOTICES_FILE in backups) and not (root / SKILLS_DIR).exists():
        created_dirs.append('skills')
    return {
        'version': 1,
        'staging': staging.relative_to(root).as_posix(),
        'backups': backup_map,
        'moves': moves,
        'created_dirs': created_dirs,
    }


def _write_publish_journal(root: Path, payload: dict) -> Path:
    """Write root/.adopt-publish.journal.json atomically with flush and fsync."""
    journal_path = root / JOURNAL_NAME
    if journal_path.is_symlink():
        raise _journal_error(journal_path, 'symlink refused')
    fd, raw_tmp = tempfile.mkstemp(dir=root, prefix=f'{JOURNAL_NAME}.', suffix='.tmp')
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, journal_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return journal_path


def _journal_rel_path(root: Path, raw: object, journal_path: Path) -> Path:
    """Validate a relative path from the publish journal, refusing escapes and symlinks."""
    if not isinstance(raw, str):
        raise _journal_error(journal_path, 'malformed path entry')
    try:
        rel = safe_path(raw)
    except Refused as error:
        raise _journal_error(journal_path, f'invalid path {raw!r}') from error
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise _journal_error(journal_path, f'symlink target refused: {raw}')
    root_resolved = root.resolve()
    resolved = current.resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise _journal_error(journal_path, f'path outside root: {raw}')
    return current


def _read_journal_json(journal_path: Path) -> dict:
    """Read and parse the JSON mapping stored in the publish journal."""
    if journal_path.is_symlink() or not journal_path.is_file():
        raise _journal_error(journal_path, 'journal must be a regular file')
    try:
        if journal_path.stat().st_size > MAX_BYTES:
            raise _journal_error(journal_path, 'journal exceeds size limit')
        data = json.loads(journal_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _journal_error(journal_path, f'malformed journal ({error})') from error
    expected_keys = {'version', 'staging', 'backups', 'moves', 'created_dirs'}
    if not isinstance(data, dict) or set(data) != expected_keys or data.get('version') != 1:
        raise _journal_error(journal_path, 'malformed journal schema')
    return data


def _parse_journal_backup_entry(
    root: Path, staging_path: Path, idx: int, rel: str, raw_entry: object, journal_path: Path,
) -> tuple[Path, Path | None, Path | None]:
    """Validate one target's backup and staged-source slot in the publish journal."""
    if not isinstance(raw_entry, dict) or set(raw_entry) != {'backup', 'source'}:
        raise _journal_error(journal_path, 'malformed backups mapping')
    target_path = _journal_rel_path(root, rel, journal_path)
    backup_rel = raw_entry['backup']
    backup_path: Path | None = None
    if backup_rel is not None:
        backup_path = _journal_rel_path(root, backup_rel, journal_path)
        if backup_path != _backup_slot_path(staging_path, idx, target_path) or not backup_path.is_file():
            raise _journal_error(journal_path, f'invalid backup path: {backup_rel!r}')
    source_rel = raw_entry['source']
    if source_rel is None:
        if rel != INDEX_FILE:
            raise _journal_error(journal_path, f'invalid backup source: {source_rel!r}')
        return target_path, backup_path, None
    source_path = _journal_rel_path(root, source_rel, journal_path)
    if source_path != _staged_target_path(staging_path, target_path):
        raise _journal_error(journal_path, f'invalid backup source: {source_rel!r}')
    return target_path, backup_path, source_path


def _parse_journal_backups(
    root: Path, staging_path: Path, raw_backups: object, journal_path: Path,
) -> list[tuple[Path, Path | None, Path | None]]:
    """Validate the journal backup entries, including a notice register when staged."""
    if not isinstance(raw_backups, dict):
        raise _journal_error(journal_path, 'malformed backups mapping')
    targets = JOURNAL_TARGETS_WITH_NOTICES if LICENCE_NOTICES_FILE in raw_backups else JOURNAL_BACKUP_TARGETS
    if set(raw_backups) != set(targets):
        raise _journal_error(journal_path, 'malformed backups mapping')
    return [
        _parse_journal_backup_entry(root, staging_path, idx, rel, raw_backups[rel], journal_path)
        for idx, rel in enumerate(targets)
    ]


def _validate_journal_move(
    root: Path, staging_path: Path, item: object, journal_path: Path,
) -> tuple[str, Path, Path]:
    """Validate one planned move entry from the publish journal."""
    if not isinstance(item, dict) or set(item) != {'kind', 'source', 'destination'}:
        raise _journal_error(journal_path, 'malformed move entry')
    kind = item['kind']
    src = _journal_rel_path(root, item['source'], journal_path)
    dest = _journal_rel_path(root, item['destination'], journal_path)
    if not src.is_relative_to(staging_path):
        raise _journal_error(journal_path, f'move source outside staging: {item["source"]!r}')
    if kind == 'licence' and dest.parent == root / 'LICENSES' and dest.suffix == '.txt':
        if src != staging_path / 'LICENSES' / dest.name:
            raise _journal_error(journal_path, f'invalid move source: {item["source"]!r}')
        return kind, src, dest
    if kind == 'skill' and dest.parent == root / 'skills' and NAME.fullmatch(dest.name):
        if src != staging_path / 'skills' / dest.name:
            raise _journal_error(journal_path, f'invalid move source: {item["source"]!r}')
        return kind, src, dest
    raise _journal_error(journal_path, f'invalid move destination: {item["destination"]!r}')


def _parse_journal_moves(
    root: Path, staging_path: Path, raw_moves: object, journal_path: Path,
) -> list[tuple[str, Path, Path]]:
    """Validate the planned licence and skill moves recorded in the publish journal."""
    limit = MAX_ROWS + check_skills.MAX_SKILLS
    if not isinstance(raw_moves, list) or len(raw_moves) > limit:
        raise _journal_error(journal_path, 'malformed moves list')
    return [_validate_journal_move(root, staging_path, item, journal_path) for item in raw_moves[:limit]]


def _load_publish_journal(
    root: Path, journal_path: Path,
) -> tuple[Path, list[tuple[Path, Path | None, Path | None]], list[tuple[str, Path, Path]], list[Path]]:
    """Read and validate root/.adopt-publish.journal.json before modifying root."""
    data = _read_journal_json(journal_path)
    staging = _journal_rel_path(root, data['staging'], journal_path)
    if staging.parent != root or not staging.name.startswith('.adopt-publish-'):
        raise _journal_error(journal_path, 'invalid staging directory')
    if staging.exists() and not staging.is_dir():
        raise _journal_error(journal_path, 'staging path is not a directory')
    backups = _parse_journal_backups(root, staging, data['backups'], journal_path)
    moves = _parse_journal_moves(root, staging, data['moves'], journal_path)
    raw_dirs = data['created_dirs']
    if not isinstance(raw_dirs, list) or len(raw_dirs) > 4 or not set(raw_dirs).issubset({'LICENSES', 'skills'}):
        raise _journal_error(journal_path, 'malformed created_dirs list')
    created_dirs = [_journal_rel_path(root, rel, journal_path) for rel in raw_dirs[:4]]
    return staging, backups, moves, created_dirs


def _undo_journal_entry(
    kind: str, path: Path, other: Path | None = None, source: Path | None = None,
) -> None:
    """Execute one idempotent journal recovery step when its staged rename completed."""
    if kind == 'rmdir':
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
        return
    if not _staged_rename_ran(source):
        return
    if kind == 'skill':
        if path.is_dir():
            shutil.rmtree(path)
    elif kind == 'licence':
        path.unlink(missing_ok=True)
    elif kind == 'backup':
        if other is None:
            path.unlink(missing_ok=True)
        else:
            _restore_backup_file(other, path)


def _collect_journal_undos(
    backups: list[tuple[Path, Path | None, Path | None]],
    moves: list[tuple[str, Path, Path]],
    created_dirs: list[Path],
) -> list[tuple[str, Path, Path | None, Path | None]]:
    """Build the ordered recovery steps for an interrupted publish journal."""
    steps: list[tuple[str, Path, Path | None, Path | None]] = [
        (kind, dest, None, src) for kind, src, dest in reversed(moves)
    ]
    steps.extend(('backup', target, backup, src) for target, backup, src in backups)
    steps.extend(('rmdir', folder, None, None) for folder in reversed(created_dirs))
    return steps


def _reconcile_publish_journal(root: Path, *, apply: bool) -> None:
    """Roll back an interrupted publication on --apply, or refuse preview when a journal exists."""
    journal_path = root / JOURNAL_NAME
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink():
        raise _journal_error(journal_path, 'symlink refused')
    if not apply:
        raise _journal_error(
            journal_path,
            'interrupted publication pending; rerun with --apply to roll the interrupted publication back first',
        )
    staging, backups, moves, created_dirs = _load_publish_journal(root, journal_path)
    steps = _collect_journal_undos(backups, moves, created_dirs)
    failures: list[tuple[str, Path, Path | None]] = []
    first_error: OSError | None = None
    for kind, target, other, source in steps[:MAX_ROWS + check_skills.MAX_SKILLS + 8]:
        try:
            _undo_journal_entry(kind, target, other, source)
        except OSError as error:
            if first_error is None:
                first_error = error
            failures.append((kind, target, other))
    if failures:
        raise _rollback_error(staging, failures, journal_path) from first_error
    journal_path.unlink(missing_ok=True)
    if staging.exists():
        shutil.rmtree(staging)


def _apply_commit_moves(
    root: Path,
    staging: Path,
    backups: dict[Path, Path | None],
    licences: list[Path],
    skills: list[Path],
    index_staged: bool,
    undo: list[tuple[str, Path, Path | None]],
) -> None:
    """Record each planned undo step before executing its filesystem replacement."""
    if licences:
        _ensure_publish_dir(root / 'LICENSES', undo)
        for lic in licences[:MAX_ROWS]:
            dest = root / 'LICENSES' / lic.name
            undo.append(('unlink', dest, None))
            os.replace(lic, dest)
    for name in METADATA_FILES:
        target = root / name
        undo.append(('restore', target, backups[target]))
        os.replace(_staged_target_path(staging, target), target)
    if index_staged:
        _ensure_publish_dir(root / 'skills', undo)
        index_target = root / 'skills' / 'INDEX.tsv'
        undo.append(('restore', index_target, backups[index_target]))
        os.replace(_staged_target_path(staging, index_target), index_target)
    register = root / LICENCE_NOTICES_FILE
    if register in backups:
        _ensure_publish_dir(root / SKILLS_DIR, undo)
        undo.append(('restore', register, backups[register]))
        os.replace(_staged_target_path(staging, register), register)
    for staged_skill in skills[:check_skills.MAX_SKILLS]:
        _ensure_publish_dir(root / SKILLS_DIR, undo)
        dest = root / 'skills' / staged_skill.name
        if dest.exists() or dest.is_symlink():
            raise _skill_exists_error(dest)
        undo.append(('move', dest, staged_skill))
        os.replace(staged_skill, dest)


def _commit_publish(root: Path, staging: Path, staged_licences: list[Path],
                    staged_skills: list[Path], index_staged: bool) -> None:
    """Journal planned moves, move staged artefacts into root, and roll back on failure."""
    skills = list(staged_skills[:check_skills.MAX_SKILLS])
    for staged_skill in skills:
        dest = root / 'skills' / staged_skill.name
        if dest.exists() or dest.is_symlink():
            raise _skill_exists_error(dest)
    licences = [lic for lic in staged_licences[:MAX_ROWS]
                if not _licence_target_present(root, lic.name)]
    backups = _backup_publish_targets(root, staging)
    journal_path = _write_publish_journal(
        root, _build_publish_journal(root, staging, backups, licences, skills, index_staged),
    )
    undo: list[tuple[str, Path, Path | None]] = []
    try:
        _apply_commit_moves(root, staging, backups, licences, skills, index_staged, undo)
        journal_path.unlink()
    except BaseException as commit_error:
        failures = _rollback_publish(undo)
        if not failures:
            # A journal that cannot be removed is reported just below, naming its path.
            with suppress(OSError):
                journal_path.unlink(missing_ok=True)
        if failures or journal_path.exists() or journal_path.is_symlink():
            raise _rollback_error(staging, failures, journal_path) from commit_error
        raise


def _render_notice_register(notices: frozenset[str]) -> bytes:
    """Serialize a notice register within the checker's UTF-8 byte limit."""
    data = check_skills.licence_notices_yaml(notices).encode('utf-8')
    if len(data) > MAX_LICENCE_BYTES:
        raise ValueError(
            f'{LICENCE_NOTICES_FILE}: serialized size {len(data)} bytes '
            f'exceeds {MAX_LICENCE_BYTES} bytes'
        )
    return data


@contextmanager
def _publication_staging(root: Path | None):
    """Clean staging unless journal recovery needs to retain it."""
    staging = Path(tempfile.mkdtemp(dir=root, prefix='.adopt-publish-'))
    keep_staging = False
    try:
        yield staging
    except _PublishRollbackError:
        keep_staging = True
        raise
    finally:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


def publish(root, saved, rows, metadata, adopted_map=None, group_map=None,
            runner=default_download_runner, *, staging=None):
    """Stage and journal verified skills, licence templates and the (REUSE, credits) metadata pair."""
    _check_output_symlinks(root)
    reuse, credits_text = metadata
    outputs = {REUSE_FILE: reuse, CREDITS_FILE: credits_text}
    with _publication_staging(root) if staging is None else nullcontext(staging) as work:
        licences, skills, index_staged = _stage_publish(
            root, work, saved, rows, outputs, adopted_map, group_map, runner,
            template_cache=staging,
        )
        _commit_publish(root, work, licences, skills, index_staged)


def resolve_report_path(report: Path | str | None, root: Path) -> Path | None:
    """Derive the final report file and refuse conflicts with publication."""
    if report is None:
        return None
    raw_report = Path(report)
    dest = raw_report
    if not dest.is_symlink():
        dest = dest.resolve()
        if dest.is_dir() or os.fspath(report).endswith(('/', '\\')):
            dest /= DEFAULT_REPORT_FILE
    if dest.is_symlink():
        raise ValueError('report output symlink refused')
    dest, root = dest.resolve(), root.resolve()
    if dest in {root, root / DEFAULT_REPORT_FILE}:
        raise ValueError('repository root refused as report location')
    lexical_dest = raw_report.parent.resolve() / raw_report.name
    for name in (*PUBLICATION_FILES, *PUBLICATION_TREES):
        output = (root / name).resolve()
        if dest.is_relative_to(output) or output.is_relative_to(dest) or lexical_dest == root / name:
            overlap_error = f'report path overlaps publication output: {name}'
            if name in REFUSED_REPORT_TARGETS:
                raise ValueError(f'{name} refused as report location: {overlap_error}')
            raise ValueError(overlap_error)
    for name in (JOURNAL_NAME, APPLY_LOCK_NAME):
        reserved = root / name
        if dest.is_relative_to(reserved) or lexical_dest == reserved:
            raise ValueError(f'{name} refused as report location')
    return dest


def _evaluate_candidates(selected, stage: StageInputs, evaluation: EvaluationInputs, saved, metadata):
    """Keep accepted skills and prepare their metadata without publishing yet."""
    reuse, credits_text = metadata
    rows = []
    adopted_map = {}
    for row in selected[:MAX_ROWS]:
        with tempfile.TemporaryDirectory(prefix='adopt-gate-') as work:
            staging = Path(work)
            prepare_staging(stage.root, staging)
            result, skill, year = evaluate(row, staging, stage, evaluation)
            rows.append(result)
            if skill is not None:
                reuse, credits_text = add_metadata(reuse, credits_text, row, skill.name, year)
                stage.occupied.add(skill.name)
                shutil.move(str(skill), saved / skill.name)
                adopted_map[skill.name] = row
    return rows, adopted_map, reuse, credits_text


def run(candidates, groups, census, checkouts, root=Path('.'), *, apply=False,
        reader=default_reader, gate=markdown_gate, hiss_gate=hiss_gate,
        report=None, runner=None, download_runner=None,
        include_grouped=False, cache=None, sources=None, embed_model=None, base_url=None, embed_dims=None,
        embed_base_url=None):
    """Preview adoption with the selected overlap cache namespace, or reconcile and publish when applying."""
    embedding = _copy_embedding_inputs(cache, embed_model, base_url, embed_base_url, embed_dims, include_grouped,
                                       refill_paths=(candidates, checkouts, root / 'skills'))
    if download_runner is not None:
        runner = download_runner
    if runner is None:
        runner = default_download_runner
    root, checkouts = root.resolve(), checkouts.resolve()
    report_file = resolve_report_path(report, root)
    parsed = inputs(candidates, groups, census)
    if (report is not None and Path(report).is_symlink()) or (
        report_file is not None and report_file.is_symlink()
    ):
        raise _output_symlink_error('report')
    with output_lock(root / APPLY_LOCK_PREFIX) if apply else nullcontext():
        _reconcile_publish_journal(root, apply=apply)
        _check_output_symlinks(root, report_file, raw_report=report)
        opts = _RunOptions(
            apply=apply, reader=reader, gates=GateOptions(gate=gate, hiss_gate=hiss_gate),
            include_grouped=include_grouped, embedding=embedding, sources=sources, runner=runner,
        )
        rows = _evaluate_and_publish(root, checkouts, parsed, opts)
    if report_file is not None:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(report_text(rows), encoding='utf-8')
    return rows, summary(rows, parsed[0])


@dataclass(frozen=True)
class _RunOptions:
    """Execution callbacks and grouped-adoption settings for one run."""

    apply: bool
    reader: object
    gates: GateOptions
    include_grouped: bool
    embedding: CopyEmbeddingInputs
    sources: object
    runner: object


def _check_output_symlinks(
    root: Path, report_file: Path | None = None, raw_report: Path | None = None,
) -> None:
    """Refuse symlinked root outputs or report targets before reading or writing."""
    for name in OUTPUT_SYMLINK_TARGETS:
        if (root / name).is_symlink():
            raise _output_symlink_error(name)
    if (raw_report is not None and Path(raw_report).is_symlink()) or (
        report_file is not None and report_file.is_symlink()
    ):
        raise _output_symlink_error('report')


def _evaluate_and_publish(root: Path, checkouts: Path, parsed: tuple, opts: _RunOptions) -> list[dict]:
    """Read current root state, stage selected skills, and publish when applying."""
    selected, grouped, order, pins, group_rows, group_map, all_candidates = parsed
    copies_map = (resolve_copies(group_rows, checkouts, root, pins, all_candidates, opts.sources, opts.embedding)
                  if opts.include_grouped else {})
    metadata = initial_metadata(root)
    skills_dir = root / SKILLS_DIR
    occupied = {path.name for path in _skill_directories(skills_dir)}
    present = read_present_skills(skills_dir)
    licence_scan = check_skills.read_licence_scan(root)
    existing_notices = licence_scan.notices
    with (_publication_staging(root if opts.apply else None) as template_cache,
          tempfile.TemporaryDirectory(prefix='adopt-skills-') as tmp):
        saved = Path(tmp)
        stage = StageInputs(checkouts, root, occupied, opts.apply, opts.reader, template_cache,
                            opts.runner, licence_scan)
        evaluation = EvaluationInputs(grouped, order, pins, opts.include_grouped, copies_map, present, opts.gates)
        rows, adopted_map, reuse, credits_text = _evaluate_candidates(selected, stage, evaluation, saved, metadata)
        register = (
            _render_notice_register(licence_scan.notices) if licence_scan.notices != existing_notices else None
        )
        if opts.apply:
            if register is not None:
                _staged_target_path(template_cache, root / LICENCE_NOTICES_FILE).write_bytes(register)
            publish(root, saved, rows, (reuse, credits_text), adopted_map, group_map, opts.runner,
                    staging=template_cache)
    return rows


def main(argv=None):
    """Print the requested adoption report or a contextual error."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidates', 'groups', 'census', 'checkouts'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--apply', action='store_true', help='write accepted skills and attribution')
    parser.add_argument('--report', help=f'write {DEFAULT_REPORT_FILE} to this path')
    parser.add_argument('--include-grouped', action='store_true', help='adopt overlap-group members except collapsed copies')
    parser.add_argument('--cache', type=Path, default=None, help='embedding cache JSON path (required by --include-grouped)')
    parser.add_argument('--embed-model', default=os.environ.get('PERITIA_LLM_EMBED_MODEL'),
                        help='model for cached embeddings (PERITIA_LLM_EMBED_MODEL)')
    parser.add_argument('--base-url', help='default provider URL (PERITIA_LLM_BASE_URL)')
    parser.add_argument('--embed-base-url', help='embedding endpoint (defaults to --base-url)')
    parser.add_argument('--embed-dims', type=int, help='dimension of the cached embedding vectors')
    args = parser.parse_args(argv)
    try:
        _copy_embedding_inputs(args.cache, args.embed_model, args.base_url, args.embed_base_url,
                               args.embed_dims, args.include_grouped)
    except ValueError as error:
        parser.error(str(error))
    try:
        rows, totals = run(args.candidates, args.groups, args.census, args.checkouts, args.root,
                           apply=args.apply, report=args.report,
                           include_grouped=args.include_grouped, cache=args.cache, embed_model=args.embed_model,
                           base_url=args.base_url, embed_dims=args.embed_dims, embed_base_url=args.embed_base_url)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'adopt-skills: {error}', file=sys.stderr)
        return 1
    print(report_text(rows), end='')
    print(totals)
    return 0


if __name__ == '__main__':
    sys.exit(main())
