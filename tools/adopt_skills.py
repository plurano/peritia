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
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from collections import Counter
from pathlib import Path, PurePosixPath

import census
import check_skills
import judge_groups
from candidates import LICENSE_FILENAMES, MAX_ROWS, find_folder_licence_file, normalize_name
from census import LiveReader, SourceReadError
from judge_groups import collapse_copies, get_skill_text, is_existing_skill, load_sources
from overlap import Item, git_blob_id, load_cache, parse_frontmatter

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


def read_rows(path, required):
    with path.open(encoding='utf-8', newline='') as handle:
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


def candidate_id(row):
    return f"{row['source']}:{row['path']}"


def inputs(candidates, groups, census):
    candidate_rows = read_rows(candidates, {'source', 'path', 'skill_name', 'verdict',
                                            'licence', 'licence_evidence', 'functions'})
    census = read_rows(census, {'source', 'path', 'pin'})
    groups = read_rows(groups, {'members'})
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
    # Cone mode already includes root files; non-cone mode needs a literal pattern.
    cone = git(checkout, 'config', '--get', 'core.sparseCheckoutCone').strip() == b'true'
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
    out = []
    in_fence = False
    fence_char = None
    fence_len = 0
    changed = False

    fence_re = re.compile(r'^([ \t]*)(`{3,}|~{3,})([^\n\r]*)([\r\n]*)$')

    for line in lines:
        m = fence_re.match(line)
        if not in_fence:
            if m:
                indent, fence, rest, newline = m.groups()
                info = rest.strip()
                in_fence = True
                fence_char = fence[0]
                fence_len = len(fence)
                if not info:
                    out.append(f'{indent}{fence}text{newline}')
                    changed = True
                    continue
            out.append(line)
        else:
            if m:
                indent, fence, rest, newline = m.groups()
                info = rest.strip()
                if not info and fence[0] == fence_char and len(fence) >= fence_len:
                    in_fence = False
                    fence_char = None
                    fence_len = 0
            out.append(line)
    return ''.join(out), changed


def rewrite_skill_fences(skill: Path) -> bool:
    any_changed = False
    for path in sorted(skill.rglob('*')):
        if path.is_file() and path.name != 'ORIGIN.md' and path.suffix.lower() in MARKDOWN_EXTENSIONS:
            text = path.read_text(encoding='utf-8')
            new_text, changed = rewrite_fences(text)
            if changed:
                path.write_text(new_text, encoding='utf-8')
                any_changed = True
    return any_changed


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


def stage_one(row, pin, checkouts, staging, occupied, apply, reader):
    checkout, folder = checkout_for(checkouts, row, pin)
    name = choose_name(row, occupied)
    if apply:
        expand_sparse(checkout, folder)
    entries = tree_files(checkout, pin, folder)
    validate_files(entries, allow_missing=not apply)
    skill = staging / 'skills' / name
    skill.mkdir(parents=True)
    copy_blobs(checkout, entries, skill, allow_fetch=not apply)
    fence_changed = rewrite_skill_fences(skill)
    note = licence_text(row, checkout, pin, skill, apply, reader)
    attribution(skill, row, folder, pin, note, fence_changed=fence_changed)
    problems = check_skills.skill_problems(skill)
    if problems:
        raise Refused('skill-check: ' + '; '.join(problems))
    year = git(checkout, 'show', '-s', '--format=%cs', pin).decode().strip()[:4]
    return skill, year


def initial_metadata(root):
    reuse = (root / 'REUSE.toml').read_text(encoding='utf-8')
    tomllib.loads(reuse)
    credits = (root / 'CREDITS.md').read_text(encoding='utf-8')
    return reuse, credits


def add_metadata(reuse, credits, row, name, year):
    annotation = ('\n[[annotations]]\n' + f'path = ["skills/{name}/**"]\n'
                  'precedence = "override"\n' +
                  f'SPDX-FileCopyrightText = {json.dumps(year + " " + row["source"].split("/")[0] + " and contributors")}\n' +
                  f'SPDX-License-Identifier = {json.dumps(row["licence"])}\n')
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
    return reuse + annotation, '\n'.join(lines) + '\n'


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


def read_present_skills(skills_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not skills_dir.is_dir():
        return {}
    present = {}
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        origin_file = d / 'ORIGIN.md'
        if not origin_file.is_file():
            continue
        try:
            rec = check_skills.fields(origin_file.read_text(encoding='utf-8'))
            upstream = extract_source(origin_file)
            if not upstream or upstream == 'peritia':
                continue
            raw_path = rec.get('path', '').strip()
            if raw_path.endswith('/SKILL.md'):
                origin_path = str(PurePosixPath(raw_path).parent)
            elif raw_path:
                origin_path = str(PurePosixPath(raw_path))
            else:
                origin_path = '.'
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


def rebuild_index(root: Path, adopted_map: dict[str, dict] | None = None,
                  group_map: dict[str, str] | None = None) -> list[dict[str, str]]:
    skills_dir = root / 'skills'
    if not skills_dir.is_dir():
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

    dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())[:check_skills.MAX_SKILLS]
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
                group_map: dict[str, str] | None = None) -> None:
    skills_dir = root / 'skills'
    if not skills_dir.is_dir():
        return
    rows = rebuild_index(root, adopted_map, group_map)
    index_file = skills_dir / 'INDEX.tsv'
    with index_file.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS, delimiter='\t', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def get_member_skill_text(member: str, checkouts: Path, skills_dir: Path, pins: dict[str, str] | None = None) -> str:
    text = get_skill_text(member, checkouts, skills_dir)
    if text:
        return text
    if ':' in member and pins:
        source, path_str = member.split(':', 1)
        if source in pins:
            checkout = checkouts / source.replace('/', '__')
            if checkout.is_dir():
                try:
                    data = git(checkout, 'show', f'{pins[source]}:{path_str}')
                    return data.decode('utf-8', errors='replace')[:judge_groups.MAX_SKILL_CHARS]
                except Exception:
                    pass
    if ':' in member and skills_dir.is_dir():
        source, path_str = member.split(':', 1)
        present = read_present_skills(skills_dir)
        folder = str(PurePosixPath(path_str).parent) if path_str.endswith('/SKILL.md') else str(PurePosixPath(path_str))
        key = (source.lower(), folder)
        if key in present:
            skill_md = skills_dir / present[key]['name'] / 'SKILL.md'
            if skill_md.is_file():
                try:
                    return skill_md.read_text(encoding='utf-8', errors='replace')[:judge_groups.MAX_SKILL_CHARS]
                except Exception:
                    pass
    return ''


def resolve_copies(group_rows, checkouts, root, pins, all_candidates, cache, sources):
    if cache is None:
        raise ValueError('--include-grouped requires --cache')
    cache_path = Path(cache)
    if not cache_path.is_file():
        raise ValueError(f'embedding cache not found: {cache_path}')
    embeddings = load_cache(cache_path)

    if sources is None:
        sources_path = Path(__file__).resolve().parent / 'census-sources.json'
    else:
        sources_path = Path(sources)
    if isinstance(sources, dict):
        ranks = dict(sources)
    else:
        loaded_src = load_sources(sources_path)
        ranks = {s.repository: s.rank for s in loaded_src}
        for s in loaded_src:
            ranks[s.repository.lower()] = s.rank

    cand_by_cid = {candidate_id(r): r for r in all_candidates}
    all_members = {m.strip() for grow in group_rows for m in grow['members'].split(',') if m.strip()}
    items: dict[str, Item] = {}
    metadata: dict[str, dict[str, str]] = {}
    for m in all_members:
        text = get_member_skill_text(m, checkouts, root / 'skills', pins)
        meta = parse_frontmatter(text)
        metadata[m] = meta
        if is_existing_skill(m):
            rel = m[len('skills/'):].strip('/')
            sf = root / 'skills' / rel / 'SKILL.md'
            b_id = git_blob_id(sf.read_bytes()) if sf.is_file() else ''
            name = meta.get('name') or rel
            desc = meta.get('description', '')
            items[m] = Item(item_id=m, blob_id=b_id, skill_name=name, description=desc, is_existing=True)
        else:
            cand = cand_by_cid.get(m)
            b_id = cand.get('blob_id', '').strip() if cand else ''
            if not b_id and text:
                b_id = git_blob_id(text.encode('utf-8'))
            name = (cand.get('skill_name') if cand else None) or meta.get('name') or ''
            desc = meta.get('description', '')
            items[m] = Item(item_id=m, blob_id=b_id, skill_name=name, description=desc, is_existing=False)

    copies_map = {}
    for grow in group_rows:
        members = [m.strip() for m in grow['members'].split(',') if m.strip()]
        _, g_copies = collapse_copies(members, items, metadata, embeddings, ranks)
        for copy_member, reason in g_copies.items():
            copies_map[copy_member] = reason
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
        skill, year = stage_one(row, pins[row['source']], checkouts, staging, occupied, apply, reader)
        if gate:
            gate(root, staging, skill)
        if hiss_gate:
            hiss_gate(root, staging, skill)
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


def default_download_runner(args, *, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', errors='replace').strip()[:500]
        spdx_id = args[-1] if args else 'unknown'
        msg = f'licence download failed for {spdx_id}: {detail}' if detail else f'licence download failed for {spdx_id}'
        raise ValueError(msg)
    return proc.stdout


def sync_licences(root: Path, reuse_text: str, runner=default_download_runner):
    licenses_dir = root / 'LICENSES'
    licenses_dir.mkdir(parents=True, exist_ok=True)
    data = tomllib.loads(reuse_text)
    spdx_ids = set()
    for anno in data.get('annotations', []):
        val = anno.get('SPDX-License-Identifier')
        if val:
            for token in re.findall(r'[A-Za-z0-9.-]+', val):
                if token not in ('AND', 'OR', 'WITH'):
                    spdx_ids.add(token)
    for spdx_id in sorted(spdx_ids):
        lic_file = licenses_dir / f'{spdx_id}.txt'
        if not lic_file.is_file():
            try:
                runner(['reuse', 'download', spdx_id], cwd=root)
            except Exception as error:
                raise ValueError(f'licence download failed for {spdx_id}') from error


def publish(root, saved, rows, reuse, credits, adopted_map=None, group_map=None, runner=default_download_runner):
    # All gates have completed before publishing any skill. Never overwrite a skill.
    for row in rows:
        if row['verdict'] == 'adopted':
            shutil.copytree(saved / Path(row['directory']).name, root / row['directory'])
    for name, text in [('REUSE.toml', reuse), ('CREDITS.md', credits)]:
        (root / name).write_text(text, encoding='utf-8')
    sync_licences(root, reuse, runner=runner)
    write_index(root, adopted_map=adopted_map, group_map=group_map)


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
        include_grouped=False, cache=None, sources=None):
    if download_runner is not None:
        runner = download_runner
    if runner is None:
        runner = default_download_runner
    root, checkouts = root.resolve(), checkouts.resolve()
    report_file = resolve_report_path(report, root)
    selected, grouped, order, pins, group_rows, group_map, all_candidates = inputs(candidates, groups, census)
    for name in ('skills', 'REUSE.toml', 'CREDITS.md'):
        if (root / name).is_symlink():
            raise ValueError(f'{name}: output symlink refused')
    if report_file is not None and report_file.is_symlink():
        raise ValueError('report output symlink refused')

    copies_map = {}
    if include_grouped:
        copies_map = resolve_copies(group_rows, checkouts, root, pins, all_candidates, cache, sources)

    reuse, credits = initial_metadata(root)
    occupied = {path.name for path in (root / 'skills').iterdir() if path.is_dir()} if (root / 'skills').exists() else set()
    present = read_present_skills(root / 'skills')
    rows = []
    adopted_map = {}
    with tempfile.TemporaryDirectory(prefix='adopt-skills-') as tmp:
        saved = Path(tmp) / 'accepted'
        saved.mkdir()
        for row in selected:
            with tempfile.TemporaryDirectory(prefix='adopt-gate-') as work:
                staging = Path(work)
                prepare_staging(root, staging)
                result, skill, year = evaluate(row, grouped, order, pins, checkouts, root, staging,
                                               occupied, apply, reader, gate=gate, hiss_gate=hiss_gate,
                                               include_grouped=include_grouped, copies=copies_map,
                                               present=present)
                rows.append(result)
                if skill is not None:
                    reuse, credits = add_metadata(reuse, credits, row, skill.name, year)
                    occupied.add(skill.name)
                    shutil.move(str(skill), saved / skill.name)
                    adopted_map[skill.name] = row
        if apply:
            publish(root, saved, rows, reuse, credits, adopted_map=adopted_map, group_map=group_map, runner=runner)
    if report_file is not None:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(report_text(rows), encoding='utf-8')
    return rows, summary(rows, selected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidates', 'groups', 'census', 'checkouts'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--apply', action='store_true', help='write accepted skills and attribution')
    parser.add_argument('--report', type=Path, default=None, help='write adopt-report.tsv to this path')
    parser.add_argument('--include-grouped', action='store_true', help='adopt overlap-group members except collapsed copies')
    parser.add_argument('--cache', type=Path, default=None, help='embedding cache JSON path (required by --include-grouped)')
    args = parser.parse_args(argv)
    try:
        rows, totals = run(args.candidates, args.groups, args.census, args.checkouts, args.root,
                           apply=args.apply, report=args.report,
                           include_grouped=args.include_grouped, cache=args.cache)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'adopt-skills: {error}', file=sys.stderr)
        return 1
    print(report_text(rows), end='')
    print(totals)
    return 0


if __name__ == '__main__':
    sys.exit(main())
