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
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# isort: split
# Codacy's Ruff sorts tomllib without the py311 target.
import tomllib

# isort: split

import census
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

MAX_FILES = 200
MAX_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
TIMEOUT = 60
REPORT_FIELDS = ['candidate', 'verdict', 'directory', 'reason']
INDEX_FIELDS = ['skill', 'source', 'functions', 'domains', 'overlap_group']
INDEX_HEADER = '\t'.join(INDEX_FIELDS)
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


def command(args, *, cwd=None, data=None, timeout=TIMEOUT, allow_fetch=False):
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
    return (name in LICENSE_FILENAMES or
            bool(re.fullmatch(r'(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?', name, re.IGNORECASE)))


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


def rewrite_skill_fences(skill: Path) -> set[str]:
    changed_files: set[str] = set()
    for path in sorted(skill.rglob('*'))[:MAX_ROWS]:
        if path.is_file() and path.name != 'ORIGIN.md' and path.suffix.lower() in MARKDOWN_EXTENSIONS:
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


def licence_text(row, checkout, pin, skill, apply, reader):
    evidence = row['licence_evidence']
    note = ''
    try:
        if evidence == 'folder':
            licence = find_folder_licence_file(skill)
            data = licence.read_bytes() if licence else b''
        elif evidence in ('repository', 'repository:self', 'frontmatter'):
            entries = tree_files(checkout, pin, PurePosixPath('.'), recursive=False)
            entries = [entry for entry in entries if entry[0].name in LICENSE_FILENAMES or
                       re.fullmatch(r'(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?', entry[0].name, re.I)]
            with tempfile.TemporaryDirectory(prefix='adopt-licence-') as tmp:
                if apply:
                    for entry in entries:
                        expand_sparse(checkout, entry[0], file=True)
                    entries = [(path, mode, kind, oid, git(checkout, 'cat-file', '-s', oid).decode().strip())
                                for path, mode, kind, oid, _ in entries]
                validate_files(entries, allow_missing=not apply)
                copy_blobs(checkout, entries, Path(tmp), allow_fetch=not apply)
                licence = find_folder_licence_file(Path(tmp))
                data = licence.read_bytes() if licence else b''
        elif evidence.startswith('upstream:'):
            data, note = upstream_licence(evidence.removeprefix('upstream:'), reader)
        else:
            data = b''
        if not data.strip() or len(data) > MAX_BYTES:
            raise Refused('licence-text-missing')
        data.decode('utf-8')
    except SourceReadError as error:
        detail = ' '.join(str(error).split())
        raise Refused(f'licence-text-missing: {detail}' if detail else 'licence-text-missing') from error
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise Refused('licence-text-missing') from error
    (skill / 'LICENSE').write_bytes(data)
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
        if path.is_file() and path.name != 'ORIGIN.md' and path.suffix.lower() in MARKDOWN_EXTENSIONS
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


def stage_one(row, pin, checkouts, root, staging, occupied, apply, reader):
    checkout, folder = checkout_for(checkouts, row, pin)
    name = choose_name(row, occupied)
    if apply:
        expand_sparse(checkout, folder)
    raw_entries = tree_files(checkout, pin, folder)
    entries, filter_notes = filter_entries(raw_entries, folder)
    validate_files(entries, allow_missing=not apply)
    skill = staging / 'skills' / name
    skill.mkdir(parents=True)
    copy_blobs(checkout, entries, skill, allow_fetch=not apply)
    fenced_files = rewrite_skill_fences(skill)
    normalized, patched = normalize_skill_markdown(root, skill)
    lic_note = licence_text(row, checkout, pin, skill, apply, reader)
    note = build_notes(skill, filter_notes, normalized, {}, {}, lic_note, patched=patched)
    attribution(skill, row, folder, pin, note, fence_changed=bool(fenced_files))
    problems = check_skills.skill_problems(skill)
    if problems:
        raise Refused('skill-check: ' + '; '.join(problems))
    year = git(checkout, 'show', '-s', '--format=%cs', pin).decode().strip()[:4]
    return skill, year, folder, (filter_notes, normalized, lic_note, fenced_files, patched)


def initial_metadata(root):
    reuse = (root / 'REUSE.toml').read_text(encoding='utf-8')
    tomllib.loads(reuse)
    credits = (root / 'CREDITS.md').read_text(encoding='utf-8')
    return reuse, credits


def add_metadata(reuse, credits, row, name, year):
    path_line = f'path = ["skills/{name}/**"]\n'
    annotation = ('\n[[annotations]]\n' + path_line +
                  'precedence = "override"\n' +
                  f'SPDX-FileCopyrightText = {json.dumps(year + " " + row["source"].split("/")[0] + " and contributors")}\n' +
                  f'SPDX-License-Identifier = {json.dumps(row["licence"])}\n')
    if path_line not in reuse:
        reuse = reuse + annotation
    url = f'https://github.com/{row["source"]}'
    lines = credits.splitlines()
    found = False
    for index, line in enumerate(lines):
        cells = line.split('|')
        if len(cells) == 5 and re.search(re.escape(url) + r'(?=[>\s)])', cells[1], re.I):
            licences = sorted(set(cells[2].strip().split(', ')) | {row['licence']})
            names = sorted(set(re.findall(r'`([^`]+)`', cells[3])) | {name})
            lines[index] = f'| <{url}> | {", ".join(licences)} | ' + ', '.join(f'`{n}`' for n in names) + ' |'
            found = True
            break
    if not found:
        lines.append(f'| <{url}> | {row["licence"]} | `{name}` |')
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
            for path in sorted(folder.iterdir())[:check_skills.MAX_SKILLS]:
                if path.is_dir():
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
    skills_dir = root / 'skills'
    dirs = _skill_directories(skills_dir, staged_skills)
    if not dirs:
        return []
    index_file = skills_dir / 'INDEX.tsv'
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


def evaluate(row, grouped, order, pins, checkouts, root, staging, occupied, apply, reader,
             gate=markdown_gate, hiss_gate=hiss_gate, include_grouped=False, copies=None,
             present=None):
    result = dict(zip(REPORT_FIELDS, (candidate_id(row), 'refused', '', '')))
    cid = result['candidate']
    if include_grouped:
        if copies and cid in copies:
            result.update(verdict='refused', reason=copies[cid])
            return result, None, ''
    else:
        if cid in grouped:
            result.update(verdict='skipped-grouped', reason='awaiting-group-judge')
            return result, None, ''
    try:
        if cid not in order:
            raise Refused('candidate-not-in-census')
        if present is None and root is not None:
            present = read_present_skills(root / 'skills')
        if present:
            cand_folder = candidate_folder(row)
            if cand_folder is not None:
                key = (row['source'].lower(), cand_folder)
                if key in present:
                    info = present[key]
                    old_commit = info['commit']
                    new_commit = pins.get(row['source'], row.get('pin', ''))
                    reason = ('' if old_commit == new_commit else
                              f'present: pinned {old_commit[:12]}, census {new_commit[:12]}')
                    result.update(verdict='present', directory=info['directory'], reason=reason)
                    return result, None, ''
        if len(occupied) >= check_skills.MAX_SKILLS:
            raise Refused('skill-count-limit')
        pin = pins[row['source']]
        skill, year, folder, state = stage_one(
            row, pin, checkouts, root, staging, occupied, apply, reader)
        run_gates(root, staging, skill, row, folder, pin, state, GateOptions(gate=gate, hiss_gate=hiss_gate))
        # A future gate fixer must preserve the checker contract too.
        problems = check_skills.skill_problems(skill)
        if problems:
            raise Refused('skill-check: ' + '; '.join(problems))
        result.update(verdict='adopted', directory=f'skills/{skill.name}')
        return result, skill, year
    except SourceReadError as error:
        detail = ' '.join(str(error).split())
        result['reason'] = f'licence-text-missing: {detail}' if detail else 'licence-text-missing'
        return result, None, ''
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result['reason'] = ' '.join(str(error).split())
        return result, None, ''


def _called_process_detail(error: subprocess.CalledProcessError) -> str:
    """Extract a bounded message from a failed subprocess exception."""
    raw = error.stderr.decode('utf-8', errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
    return raw.strip()[:500] or ' '.join(str(error).split())[:500]


def _download_error(args, error: Exception) -> ValueError:
    """Format a licence download failure naming the SPDX identifier and cause."""
    spdx_id = args[-1] if args else 'unknown'
    prefix = f'licence download failed for {spdx_id}'
    if isinstance(error, ValueError) and str(error).startswith(f'{prefix}:'):
        return error
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
    return ValueError(f'{prefix}: {detail}')


def default_download_runner(args, *, cwd=None):
    """Run one licence download under the module deadline; failures raise ValueError."""
    try:
        return command(args, cwd=cwd, timeout=TIMEOUT)
    except (Refused, subprocess.SubprocessError, OSError) as error:
        raise _download_error(args, error) from error


def _missing_spdx_ids(root: Path, reuse_text: str) -> list[str]:
    """List sorted SPDX identifiers in REUSE.toml that root/LICENSES lacks."""
    licenses_dir = root / 'LICENSES'
    spdx_ids = set()
    for anno in tomllib.loads(reuse_text).get('annotations', [])[:MAX_ROWS]:
        val = anno.get('SPDX-License-Identifier')
        if val:
            for token in re.findall(r'[A-Za-z0-9.-]+', val)[:64]:
                if token not in ('AND', 'OR', 'WITH'):
                    spdx_ids.add(token)
    return [spdx_id for spdx_id in sorted(spdx_ids)[:MAX_ROWS]
            if not (licenses_dir / f'{spdx_id}.txt').is_file()]


def sync_licences(root: Path, reuse_text: str, runner=default_download_runner, *,
                  staging: Path | None = None) -> list[Path]:
    """Download each licence text root/LICENSES lacks, into staging when given; return the files written."""
    dest_root = root if staging is None else staging
    missing = _missing_spdx_ids(root, reuse_text)
    if not missing:
        return []
    licenses_dir = dest_root / 'LICENSES'
    created_dir = not licenses_dir.exists()
    licenses_dir.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for spdx_id in missing[:MAX_ROWS]:
            lic_file = licenses_dir / f'{spdx_id}.txt'
            cmd = ['reuse', 'download', '-o', str(lic_file), spdx_id]
            try:
                runner(cmd, cwd=dest_root)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                mapped = _download_error(cmd, error)
                raise mapped from (error.__cause__ if mapped is error else error)
            if not lic_file.is_file():
                raise ValueError(f'licence download failed for {spdx_id}: {lic_file.name} not written')
            written.append(lic_file)
    finally:
        if created_dir and licenses_dir.is_dir() and not any(licenses_dir.iterdir()):
            licenses_dir.rmdir()
    return written


# Root metadata files publish stages and moves like every other output.
METADATA_FILES = ('REUSE.toml', 'CREDITS.md')


def _stage_publish(root: Path, staging: Path, saved: Path, rows: list[dict],
                   metadata: dict[str, str], adopted_map, group_map,
                   runner) -> tuple[list[Path], list[Path], bool]:
    """Stage adopted skills, metadata, index and downloaded licences before touching root."""
    staged_skills = []
    for row in rows[:MAX_ROWS]:
        if row['verdict'] != 'adopted':
            continue
        dest = root / row['directory']
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(f'{dest}: skill directory already exists')
        target = staging / 'skills' / Path(row['directory']).name
        shutil.copytree(saved / target.name, target)
        staged_skills.append(target)
    for name in METADATA_FILES:
        (staging / name).write_text(metadata[name], encoding='utf-8')
    staged_licences = sync_licences(root, metadata['REUSE.toml'], runner=runner,
                                    staging=staging)
    index_staged = write_index(root, adopted_map=adopted_map, group_map=group_map,
                               target=staging / 'INDEX.tsv', staged_skills=staging / 'skills')
    return staged_licences, staged_skills, index_staged


def _backup_publish_targets(root: Path, staging: Path, index_staged: bool) -> dict[Path, Path | None]:
    """Copy existing root metadata and index files into staging before replacing them."""
    backup_dir = staging / 'backup'
    backup_dir.mkdir(parents=True, exist_ok=True)
    targets = [root / name for name in METADATA_FILES]
    if index_staged:
        targets.append(root / 'skills' / 'INDEX.tsv')
    backups: dict[Path, Path | None] = {}
    for idx, target in enumerate(targets):
        if target.is_file():
            saved = backup_dir / f'{idx}-{target.name}'
            shutil.copy2(target, saved)
            backups[target] = saved
        else:
            backups[target] = None
    return backups


def _apply_undo_step(kind: str, target: Path, other: Path | None) -> None:
    """Undo one recorded filesystem change under root."""
    if kind == 'move' and other is not None:
        os.replace(target, other)
    elif kind == 'restore':
        if other is not None:
            os.replace(other, target)
        else:
            target.unlink(missing_ok=True)
    elif kind == 'unlink':
        target.unlink(missing_ok=True)
    elif kind == 'rmdir' and target.is_dir() and not any(target.iterdir()):
        target.rmdir()


def _rollback_publish(undo: list[tuple[str, Path, Path | None]]) -> None:
    """Revert recorded publish steps in reverse order on commit failure."""
    for kind, target, other in reversed(undo):
        try:
            _apply_undo_step(kind, target, other)
        except OSError:
            continue


def _ensure_publish_dir(folder: Path, undo: list[tuple[str, Path, Path | None]]) -> None:
    """Create an output directory when absent and record its removal for rollback."""
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        undo.append(('rmdir', folder, None))


def _commit_publish(root: Path, staging: Path, staged_licences: list[Path],
                    staged_skills: list[Path], index_staged: bool) -> None:
    """Move staged artefacts into root, rolling back in reverse order on any failure."""
    backups = _backup_publish_targets(root, staging, index_staged)
    undo: list[tuple[str, Path, Path | None]] = []
    committed = False
    try:
        if staged_licences:
            _ensure_publish_dir(root / 'LICENSES', undo)
            for lic in staged_licences[:MAX_ROWS]:
                dest = root / 'LICENSES' / lic.name
                os.replace(lic, dest)
                undo.append(('unlink', dest, None))
        for name in METADATA_FILES:
            target = root / name
            os.replace(staging / name, target)
            undo.append(('restore', target, backups[target]))
        if index_staged:
            _ensure_publish_dir(root / 'skills', undo)
            index_target = root / 'skills' / 'INDEX.tsv'
            os.replace(staging / 'INDEX.tsv', index_target)
            undo.append(('restore', index_target, backups[index_target]))
        for staged_skill in staged_skills[:check_skills.MAX_SKILLS]:
            _ensure_publish_dir(root / 'skills', undo)
            dest = root / 'skills' / staged_skill.name
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(f'{dest}: skill directory already exists')
            os.replace(staged_skill, dest)
            undo.append(('move', dest, staged_skill))
        committed = True
    finally:
        if not committed:
            _rollback_publish(undo)


def publish(root, saved, rows, reuse, credits, adopted_map=None, group_map=None, runner=default_download_runner):
    """Stage every output, then move it under root; a failed download leaves root unchanged."""
    # All gates and licence downloads complete in staging before modifying root. Never overwrite a skill.
    staging = Path(tempfile.mkdtemp(dir=root, prefix='.adopt-publish-'))
    try:
        licences, skills, index_staged = _stage_publish(
            root, staging, saved, rows, {'REUSE.toml': reuse, 'CREDITS.md': credits},
            adopted_map, group_map, runner,
        )
        _commit_publish(root, staging, licences, skills, index_staged)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def resolve_report_path(report: Path | None, root: Path) -> Path | None:
    if report is None:
        return None
    dest = report.resolve()
    if dest.is_dir() or str(report).endswith(('/', '\\')):
        dest = dest / 'adopt-report.tsv'
    root_resolved = root.resolve()
    if dest == root_resolved or dest == (root_resolved / 'adopt-report.tsv'):
        raise ValueError('repository root refused as report location')
    return dest


def run(candidates, groups, census, checkouts, root=Path('.'), *, apply=False,
        reader=default_reader, gate=markdown_gate, hiss_gate=hiss_gate,
        report=None, runner=None, download_runner=None,
        include_grouped=False, cache=None, sources=None, embed_model=None, base_url=None, embed_dims=None,
        embed_base_url=None):
    """Preview or apply adoption using the selected overlap cache namespace."""
    embedding = _copy_embedding_inputs(cache, embed_model, base_url, embed_base_url, embed_dims, include_grouped,
                                       refill_paths=(candidates, checkouts, root / 'skills'))
    if download_runner is not None:
        runner = download_runner
    if runner is None:
        runner = default_download_runner
    root, checkouts = root.resolve(), checkouts.resolve()
    report_file = resolve_report_path(report, root)
    parsed = inputs(candidates, groups, census)
    _check_output_symlinks(root, report_file)
    opts = _RunOptions(
        apply=apply, reader=reader, gates=GateOptions(gate=gate, hiss_gate=hiss_gate),
        include_grouped=include_grouped, embedding=embedding, sources=sources, runner=runner,
    )
    with output_lock(root / '.adopt-skills') if apply else nullcontext():
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


def _check_output_symlinks(root: Path, report_file: Path | None) -> None:
    """Refuse symlinked root outputs or report targets before locking or writing."""
    for name in ('skills', 'REUSE.toml', 'CREDITS.md', '.adopt-skills.lock'):
        if (root / name).is_symlink():
            raise ValueError(f'{name}: output symlink refused')
    if report_file is not None and report_file.is_symlink():
        raise ValueError('report output symlink refused')


def _evaluate_and_publish(root: Path, checkouts: Path, parsed: tuple, opts: _RunOptions) -> list[dict]:
    """Read current root state, stage selected skills, and publish when applying."""
    selected, grouped, order, pins, group_rows, group_map, all_candidates = parsed
    copies_map = (resolve_copies(group_rows, checkouts, root, pins, all_candidates, opts.sources, opts.embedding)
                  if opts.include_grouped else {})
    reuse_text, credits_text = initial_metadata(root)
    skills_dir = root / 'skills'
    occupied = {path.name for path in skills_dir.iterdir() if path.is_dir()} if skills_dir.exists() else set()
    present = read_present_skills(skills_dir)
    rows: list[dict] = []
    adopted_map: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix='adopt-skills-') as tmp:
        saved = Path(tmp) / 'accepted'
        saved.mkdir()
        for row in selected:
            with tempfile.TemporaryDirectory(prefix='adopt-gate-') as work:
                staging = Path(work)
                prepare_staging(root, staging)
                result, skill, year = evaluate(
                    row, grouped, order, pins, checkouts, root, staging, occupied, opts.apply, opts.reader,
                    gate=opts.gates.gate, hiss_gate=opts.gates.hiss_gate,
                    include_grouped=opts.include_grouped, copies=copies_map, present=present,
                )
                rows.append(result)
                if skill is not None:
                    reuse_text, credits_text = add_metadata(reuse_text, credits_text, row, skill.name, year)
                    occupied.add(skill.name)
                    shutil.move(str(skill), saved / skill.name)
                    adopted_map[skill.name] = row
        if opts.apply:
            publish(root, saved, rows, reuse_text, credits_text, adopted_map=adopted_map,
                    group_map=group_map, runner=opts.runner)
    return rows


def _parse_args(argv=None):
    """Validate adoption CLI inputs, including any explicitly supplied endpoints."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidates', 'groups', 'census', 'checkouts'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--apply', action='store_true', help='write accepted skills and attribution')
    parser.add_argument('--report', type=Path, default=None, help='write adopt-report.tsv to this path')
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
    return args


def main(argv=None):
    """Print the requested adoption report or a contextual error."""
    args = _parse_args(argv)
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
