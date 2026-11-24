#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Temporary sparse repositories and a fake GitHub reader; no live adoption."""
import base64
import csv
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

# isort: split
# Codacy's Ruff sorts tomllib without the py311 target.
import tomllib

# isort: split

import adopt_skills as adopt
import census
import check_skills
import input_identity
import overlap

REPO = Path(__file__).resolve().parent.parent
MD = '---\nname: {name}\ndescription: Use when testing adoption.\n---\n\n# Example\n\nCheck the result.\n'
COPY_MD = '---\nname: {name}\ndescription: Use when testing copied adoption.\n---\n\n# Example\n\nCheck the copy.\n'
LICENCE = 'MIT License\n\nCopyright (c) 2025 owner\n'
CANDIDATE_FIELDS = ['source', 'path', 'skill_name', 'verdict', 'licence', 'licence_evidence', 'functions']
EMBED_MODEL = 'fixture-embed'
EMBED_PROVIDER = 'https://models.example.test/v1'
REAL_RUN = subprocess.run


def run_fixture_command(args, **kwargs):
    """Run local fixtures without inheriting Git's configuration or repository state."""
    explicit_env = kwargs.get('env')
    original_env = os.environ if explicit_env is None else explicit_env
    env = {key: value for key, value in original_env.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    if explicit_env is not None:
        for key in ('GIT_OPTIONAL_LOCKS', 'GIT_NO_LAZY_FETCH'):
            if key in original_env:
                env[key] = original_env[key]
    if args[0] == 'git' and 'commit-tree' in args:
        env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.org',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.org',
                   GIT_AUTHOR_DATE='2025-01-01T00:00:00Z', GIT_COMMITTER_DATE='2025-01-01T00:00:00Z')
    kwargs.setdefault('timeout', adopt.TIMEOUT)
    return REAL_RUN(args, **{**kwargs, 'env': env})


def tsv(path, fields, rows):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fields, delimiter='\t', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def snapshot(root):
    return {str(p.relative_to(root)): ('link', str(p.readlink())) if p.is_symlink() else
            ('file', hashlib.sha256(p.read_bytes()).hexdigest()) if p.is_file() else ('dir', '')
            for p in root.rglob('*')}


def passing_gate(root, staging, skill):
    return None


class _OsReplaceProxy:
    """Delegate os attributes to stdlib os while overriding os.replace inside adopt."""

    def __init__(self, replace_fn):
        self.replace = replace_fn

    def __getattr__(self, name):
        return getattr(os, name)


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        """Prepare an isolated temporary worktree and synthetic source repositories."""
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        clean_env['GIT_CONFIG_GLOBAL'] = os.devnull
        clean_env['GIT_CONFIG_NOSYSTEM'] = '1'
        self.enterContext(patch.dict(os.environ, clean_env, clear=True))
        self.enterContext(patch.object(subprocess, 'run', run_fixture_command))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
        self.root.mkdir()
        for name in ('.standards.yaml', '.standards-baseline.json'):
            src = REPO / name
            if src.is_file():
                shutil.copyfile(src, self.root / name)
        (self.root / 'REUSE.toml').write_text('version = 1\n', encoding='utf-8')
        (self.root / 'CREDITS.md').write_text('# Credits\n\n| Upstream | Licence | Skills taken |\n'
                                             '| --- | --- | --- |\n', encoding='utf-8')
        self.checkouts = self.base / 'checkouts'
        self.checkouts.mkdir()
        self.candidates, self.census, self.groups = [], [], []
        self.reader_calls = []

    def source(self, files, source='owner/repo', symlink=None, cone=False):
        """Create a sparse synthetic checkout and return its pinned commit ID."""
        checkout = self.checkouts / source.replace('/', '__')
        checkout.mkdir()
        adopt.git(checkout, 'init', '--quiet')
        for name, content in files.items():
            target = checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        if symlink:
            (checkout / symlink).symlink_to('../outside')
        adopt.git(checkout, 'add', '--force', '--all')
        tree = adopt.git(checkout, 'write-tree').decode().strip()
        # Synthetic fixture objects only: no git commit, hooks, or working branch edits.
        pin = adopt.git(checkout, 'commit-tree', tree, data=b'Fixture\n').decode().strip()
        adopt.git(checkout, 'update-ref', 'HEAD', pin)
        mode = '--cone' if cone else '--no-cone'
        patterns = b'' if cone else b'**/SKILL.md\n**/LICENSE*\n'
        adopt.git(checkout, 'sparse-checkout', 'set', mode, '--stdin', data=patterns)
        return pin

    def candidate(self, name='demo', folder='pack/demo', source='owner/repo', pin=None, evidence='folder',
                  blob_id=None, domains=None):
        row = dict(source=source, path=folder + '/SKILL.md' if folder else 'SKILL.md', skill_name=name,
                   verdict='candidate', licence='MIT', licence_evidence=evidence, functions='testing;coding')
        if blob_id is not None:
            row['blob_id'] = blob_id
        if domains is not None:
            row['domains'] = domains
        self.candidates.append(row)
        self.census.append({key: row[key] for key in ('source', 'path')} | {'pin': pin})
        return row

    def fixture(self, *, extra=None, symlink=None, evidence='folder', cone=False):
        files = {'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE}
        files.update(extra or {})
        pin = self.source(files, symlink=symlink, cone=cone)
        return self.candidate(pin=pin, evidence=evidence)

    def reader(self, endpoint):
        self.reader_calls.append(endpoint)
        answers = {'repos/upstream/original': {'default_branch': 'main'},
                   'repos/upstream/original/commits/main': {'sha': 'b' * 40},
                   'repos/upstream/original/license?ref=' + 'b' * 40:
                   {'encoding': 'base64', 'content': base64.b64encode(LICENCE.encode()).decode()}}
        return answers[endpoint]

    def invoke(self, *, apply=True, gate=passing_gate, hiss_gate=passing_gate, reader=None,
               report=None, runner=None, include_grouped=False, cache=None, sources=None, embed_model=EMBED_MODEL):
        """Write fixture tables and invoke adoption with controlled readers and gates."""
        cand_fields = list(CANDIDATE_FIELDS)
        for key in ('blob_id', 'domains'):
            if any(key in c for c in self.candidates) and key not in cand_fields:
                cand_fields.append(key)
        tsv(self.base / 'candidates.tsv', cand_fields, self.candidates)
        tsv(self.base / 'census.tsv', ['source', 'path', 'pin'], self.census)
        group_fields = ['group_id', 'members'] if any('group_id' in g for g in self.groups) else ['members']
        tsv(self.base / 'groups.tsv', group_fields, self.groups)
        return adopt.run(self.base / 'candidates.tsv', self.base / 'groups.tsv', self.base / 'census.tsv',
                         self.checkouts, self.root, apply=apply,
                         reader=self.reader if reader is None else reader, gate=gate,
                         hiss_gate=hiss_gate, report=report, runner=runner,
                         include_grouped=include_grouped, cache=cache, sources=sources,
                         embed_model=embed_model, base_url=EMBED_PROVIDER)

    def embedding_cache(self, vectors):
        """Write fixture embeddings using overlap's item preprocessing and cache format."""
        items = []
        by_text = {}
        for row in self.candidates:
            if row['blob_id'] not in vectors:
                continue
            path = self.checkouts / row['source'].replace('/', '__') / row['path']
            with path.open(encoding='utf-8') as handle:
                item = overlap.item_from_skill(adopt.candidate_id(row), row['blob_id'], row['skill_name'],
                                               handle.read(overlap.MAX_SKILL_CHARS))
            items.append(item)
            embed_text = overlap.make_embed_text(item)
            vector = vectors[item.blob_id]
            if embed_text in by_text and by_text[embed_text] != vector:
                raise ValueError(f'conflicting vectors for identical embed text: {embed_text}')
            by_text[embed_text] = vector
        path = self.base / 'cache.json'
        overlap.get_embeddings(items, lambda texts: [by_text[text] for text in texts], path,
                               model=EMBED_MODEL, provider=EMBED_PROVIDER)
        return path

    def test_folder_licence_origin_reuse_and_checker(self):
        self.fixture(extra={'pack/demo/scripts/helper.py': 'print("example")\n'})
        checkout = self.checkouts / 'owner__repo'
        self.assertFalse((checkout / 'pack/demo/scripts/helper.py').exists())
        rows, totals = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        self.assertEqual((skill / 'LICENSE').read_text(), LICENCE)
        self.assertEqual((skill / 'scripts/helper.py').read_text(), 'print("example")\n')
        self.assertTrue((checkout / 'pack/demo/scripts/helper.py').is_file())
        origin = check_skills.fields((skill / 'ORIGIN.md').read_text())
        self.assertEqual(origin['commit'], self.census[0]['pin'])
        self.assertEqual(origin['path'], 'pack/demo')
        self.assertEqual(origin['upstream'], '<https://github.com/owner/repo>')
        annotation = tomllib.loads((self.root / 'REUSE.toml').read_text())['annotations'][0]
        self.assertEqual(annotation['path'], ['skills/demo/**'])
        self.assertEqual(annotation['SPDX-FileCopyrightText'], '2025 owner and contributors')
        self.assertEqual(annotation['SPDX-License-Identifier'], 'MIT')
        self.assertEqual(check_skills.check(self.root), 0)
        self.assertIn('function testing: adopted=1', totals)
        self.assertFalse((self.root / 'adopt-report.tsv').exists())

    def test_collision_rewrites_frontmatter_and_records_rename(self):
        self.fixture()
        (self.root / 'skills/demo').mkdir(parents=True)
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['directory'], 'skills/demo-owner')
        skill = self.root / 'skills/demo-owner'
        self.assertIn('name: demo-owner\n', (skill / 'SKILL.md').read_text())
        self.assertIn('name changed from demo to demo-owner', (skill / 'ORIGIN.md').read_text())
        self.assertEqual(check_skills.skill_problems(skill), [])

    def test_adoption_inputs_require_matching_overlap_generation(self):
        """Consume verified group rows and refuse adoption after either file changes."""
        candidates = self.base / 'candidates.tsv'
        census_path = self.base / 'census.tsv'
        groups = self.base / 'groups.tsv'
        tsv(candidates, CANDIDATE_FIELDS, [])
        tsv(census_path, ['source', 'path', 'pin'], [])
        overlap._write_overlap_generation(groups, [], [])
        self.assertEqual(adopt.inputs(candidates, groups, census_path)[4], [])
        pairs = overlap._current_pairs_path(groups)
        pairs.write_bytes(pairs.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, r'python3 tools/overlap\.py run'):
            adopt.inputs(candidates, groups, census_path)

    def test_grouped_candidate_is_skipped(self):
        row = self.fixture()
        self.groups = [{'members': f'skills/existing,{adopt.candidate_id(row)}'}]
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'skipped-grouped')
        self.assertFalse((self.root / 'skills').exists())
        self.assertEqual(self.reader_calls, [])

    def test_runtime_install_refused_before_markdown(self):
        self.fixture(extra={'pack/demo/SKILL.md': MD.format(name='demo') + '\npip install unsafe\n'})
        calls = []
        rows, _ = self.invoke(gate=lambda *args: calls.append(args))
        self.assertEqual(rows[0]['verdict'], 'refused')
        self.assertIn('installs or fetches code', rows[0]['reason'])
        self.assertEqual(calls, [])
        self.assertFalse((self.root / 'skills').exists())

    def test_symlink_refused(self):
        self.fixture(symlink='pack/demo/link')
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'refused')
        self.assertIn('symlink:', rows[0]['reason'])

    def test_missing_licence_refused(self):
        pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo')})
        self.candidate(pin=pin)
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['reason'], 'licence-text-missing')

    def test_dry_run_writes_nothing_and_matches_apply(self):
        self.fixture(extra={'pack/demo/data.txt': 'payload'})
        before_root, before_checkouts = snapshot(self.root), snapshot(self.checkouts)
        dry = self.invoke(apply=False)
        self.assertEqual(snapshot(self.root), before_root)
        self.assertEqual(snapshot(self.checkouts), before_checkouts)
        self.assertEqual(dry, self.invoke())

    def test_default_cli_is_dry_run(self):
        """Run the CLI with the test interpreter and verify dry-run leaves files alone."""
        self.fixture()
        self.invoke(apply=False)
        argv = [str(REPO / 'tools/adopt_skills.py'), '--candidates', str(self.base / 'candidates.tsv'),
                '--groups', str(self.base / 'groups.tsv'), '--census', str(self.base / 'census.tsv'),
                '--checkouts', str(self.checkouts), '--root', str(self.root)]
        before = snapshot(self.root)
        # The absent markdown gate refuses this fixture, but still produces a report without writes.
        output = adopt.command([sys.executable, '-B', *argv]).decode()
        self.assertIn('candidate\tverdict\tdirectory\treason', output)
        self.assertEqual(snapshot(self.root), before)

    def test_dry_run_fetches_missing_blob_and_adopts(self):
        self.fixture(extra={'pack/demo/payload.txt': 'not initially checked out'})
        checkout = self.checkouts / 'owner__repo'
        remote = self.base / 'remote.git'
        adopt.command(['git', 'clone', '--bare', str(checkout), str(remote)])
        adopt.git(checkout, 'config', 'remote.origin.url', str(remote))
        adopt.git(checkout, 'config', 'remote.origin.promisor', 'true')
        adopt.git(checkout, 'config', 'remote.origin.partialclonefilter', 'blob:none')
        oid = adopt.git(checkout, 'rev-parse', 'HEAD:pack/demo/payload.txt').decode().strip()
        (checkout / '.git/objects' / oid[:2] / oid[2:]).unlink()
        before_patterns = adopt.git(checkout, 'sparse-checkout', 'list').decode()
        rows, _ = self.invoke(apply=False)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        self.assertFalse((self.root / 'skills').exists())
        self.assertEqual(adopt.git(checkout, 'sparse-checkout', 'list').decode(), before_patterns)
        rows, _ = self.invoke(apply=True)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        self.assertEqual((self.root / 'skills/demo/payload.txt').read_text(), 'not initially checked out')

    def test_blob_missing_from_remote_refuses(self):
        self.fixture(extra={'pack/demo/payload.txt': 'not initially checked out'})
        checkout = self.checkouts / 'owner__repo'
        remote = self.base / 'remote.git'
        adopt.command(['git', 'clone', '--bare', str(checkout), str(remote)])
        adopt.git(checkout, 'config', 'remote.origin.url', str(remote))
        adopt.git(checkout, 'config', 'remote.origin.promisor', 'true')
        adopt.git(checkout, 'config', 'remote.origin.partialclonefilter', 'blob:none')
        oid = adopt.git(checkout, 'rev-parse', 'HEAD:pack/demo/payload.txt').decode().strip()
        (checkout / '.git/objects' / oid[:2] / oid[2:]).unlink()
        (remote / 'objects' / oid[:2] / oid[2:]).unlink()
        rows, _ = self.invoke(apply=False)
        self.assertEqual(rows[0]['verdict'], 'refused', rows)
        self.assertIn('missing-git-object: payload.txt', rows[0]['reason'])
        self.assertIn('(fetch failed:', rows[0]['reason'])
        self.assertFalse((self.root / 'skills').exists())

    def test_markdown_refusal_lists_rules_and_file(self):
        heading_md = '---\nname: demo\ndescription: Use when testing.\n---\n\n**Heading**\n\nSome body text.\n'
        self.fixture(extra={'pack/demo/SKILL.md': heading_md})
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'refused', rows)
        self.assertIn('MD036', rows[0]['reason'])
        self.assertEqual(rows[0]['reason'], 'markdown: MD036 in SKILL.md')
        self.assertFalse((self.root / 'skills').exists())

    def test_code_fence_rewrite_to_text_and_origin_changes(self):
        fence_md = (
            '---\nname: demo\ndescription: Use when testing fences.\n---\n\n'
            '# Fences\n\n'
            '```\n'
            'plain text block\n'
            '```\n\n'
            '```python\n'
            'x = 42\n'
            '```\n'
        )
        tilde_md = '# Tildes\n\n~~~\ntilde block\n~~~\n'
        self.fixture(extra={'pack/demo/SKILL.md': fence_md, 'pack/demo/notes.md': tilde_md})
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        staged_md = (skill / 'SKILL.md').read_text(encoding='utf-8')
        staged_notes = (skill / 'notes.md').read_text(encoding='utf-8')
        # bare fence becomes text and the skill passes
        self.assertIn('```text\nplain text block\n```\n', staged_md)
        self.assertIn('~~~text\ntilde block\n~~~\n', staged_notes)
        # a fence with python stays
        self.assertIn('```python\nx = 42\n```\n', staged_md)
        # a closing fence stays bare
        self.assertNotIn('```text\n\n', staged_md)
        self.assertNotIn('~~~text\n\n', staged_notes)
        # changes line present only when a fence changed
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn('code fences without a language marked text', origin)
        self.assertEqual(check_skills.check(self.root), 0)

    def test_code_fence_unchanged_has_no_fences_change_in_origin(self):
        no_bare_md = (
            '---\nname: demo\ndescription: Use when testing.\n---\n\n'
            '# Fences\n\n'
            '```python\n'
            'x = 1\n'
            '```\n'
        )
        self.fixture(extra={'pack/demo/SKILL.md': no_bare_md})
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertNotIn('code fences without a language marked text', origin)

    def test_root_licence_for_repository_and_frontmatter(self):
        for evidence in ('repository', 'frontmatter', 'repository:self'):
            with self.subTest(evidence=evidence):
                if not self.candidates:
                    pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'LICENSE.txt': LICENCE})
                    self.candidate(pin=pin, evidence=evidence)
                self.candidates[0]['licence_evidence'] = evidence
                rows, _ = self.invoke(apply=False)
                self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        self.assertEqual((self.root / 'skills/demo/LICENSE').read_text(), LICENCE)

    def test_upstream_api_is_pinned_and_recorded(self):
        self.fixture(evidence='upstream:upstream/original')
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        origin = (self.root / 'skills/demo/ORIGIN.md').read_text()
        self.assertIn('<https://github.com/upstream/original> at commit ' + 'b' * 40, origin)
        self.assertIn('commit: ' + self.census[0]['pin'], origin)
        self.assertEqual(self.reader_calls[-1], 'repos/upstream/original/license?ref=' + 'b' * 40)

    def test_upstream_api_failure_refused(self):
        self.fixture(evidence='upstream:missing/repo')
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['reason'], 'licence-text-missing')

    def test_census_order_collision_and_credit_deduplication(self):
        files = {f'pack/{name}/{file}': text for name in ('first', 'second')
                 for file, text in [('SKILL.md', MD.format(name='DEMO_name')), ('LICENSE', LICENCE)]}
        pin = self.source(files)
        self.candidate(name='DEMO_name', folder='pack/first', pin=pin)
        self.candidate(name='DEMO_name', folder='pack/second', pin=pin)
        self.candidates.reverse()
        rows, _ = self.invoke()
        self.assertEqual([r['directory'] for r in rows], ['skills/demo-name', 'skills/demo-name-owner'])
        self.assertIn('pack/first', rows[0]['candidate'])
        credits = (self.root / 'CREDITS.md').read_text()
        self.assertEqual(credits.count('https://github.com/owner/repo'), 1)
        self.assertIn('`demo-name`, `demo-name-owner`', credits)
        self.assertEqual(check_skills.check(self.root), 0)

    def test_second_collision_refused_without_overwrite(self):
        self.fixture()
        for name in ('demo', 'demo-owner'):
            (self.root / 'skills' / name).mkdir(parents=True)
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['reason'], 'name-collision')

    def test_pin_mismatch_and_path_escape_refused(self):
        self.fixture()
        self.census[0]['pin'] = 'a' * 40
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['reason'], 'checkout-pin-mismatch')
        self.candidates[0]['path'] = self.census[0]['path'] = '../SKILL.md'
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['reason'], 'path-outside-folder')

    def test_file_limit_boundary(self):
        self.fixture(extra={f'pack/demo/files/{n}.txt': 'x' for n in range(adopt.MAX_FILES - 2)})
        rows, _ = self.invoke(apply=False)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        with patch.object(adopt, 'MAX_FILES', 199):
            rows, _ = self.invoke()
        self.assertIn('file-limit', rows[0]['reason'])

    def test_size_limit_boundary(self):
        remaining = adopt.MAX_BYTES - len(MD.format(name='demo').encode()) - len(LICENCE.encode())
        self.fixture(extra={'pack/demo/payload': b'x' * remaining})
        rows, _ = self.invoke(apply=False)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        with patch.object(adopt, 'MAX_BYTES', adopt.MAX_BYTES - 1):
            rows, _ = self.invoke()
        self.assertIn('size-limit', rows[0]['reason'])

    def test_gate_failure_refuses_one_skill_and_continues(self):
        pin = self.source({f'pack/{name}/{file}': text for name in ('bad', 'good')
                           for file, text in [('SKILL.md', MD.format(name=name)), ('LICENSE', LICENCE)]})
        for name in ('bad', 'good'):
            self.candidate(name=name, folder='pack/' + name, pin=pin)
        def gate(root, staging, skill):
            if skill.name == 'bad':
                raise adopt.Refused('markdown')
        rows, _ = self.invoke(gate=gate)
        self.assertEqual([r['verdict'] for r in rows], ['refused', 'adopted'])
        self.assertEqual(rows[0]['reason'], 'markdown')
        self.assertFalse((self.root / 'skills/bad').exists())
        self.assertEqual(check_skills.check(self.root), 0)

    def test_cone_checkout_expands(self):
        self.fixture(extra={'pack/demo/data.txt': 'payload'}, cone=True)
        rows, _ = self.invoke()
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)

    def test_real_repository_markdown_gate(self):
        self.fixture(extra={'pack/demo/notes.md': '# Notes\n\nValid supporting documentation.\n'})
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        self.assertEqual(check_skills.check(self.root), 0)

    def test_real_gate_checks_supporting_markdown_even_when_ignored(self):
        self.fixture(extra={'pack/demo/notes.md': '#Malformed\n', 'pack/demo/.gitignore': 'notes.md\n'})
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['reason'], 'markdown: MD018 in notes.md', rows)
        self.assertFalse((self.root / 'skills').exists())

    def test_source_read_error_refuses_only_that_candidate(self):
        pin = self.source({f'pack/{name}/{file}': text for name in ('broken', 'good')
                           for file, text in [('SKILL.md', MD.format(name=name)), ('LICENSE', LICENCE)]})
        self.candidate(name='broken', folder='pack/broken', pin=pin, evidence='upstream:broken/repo')
        self.candidate(name='good', folder='pack/good', pin=pin, evidence='folder')

        def reader(endpoint):
            if 'broken/repo' in endpoint:
                raise census.SourceReadError('rate limit exceeded')
            return self.reader(endpoint)

        rows, _ = self.invoke(reader=reader)
        self.assertEqual([r['verdict'] for r in rows], ['refused', 'adopted'])
        self.assertEqual(rows[0]['reason'], 'licence-text-missing: rate limit exceeded')
        self.assertTrue((self.root / 'skills/good').is_dir())
        self.assertFalse((self.root / 'skills/broken').exists())
        self.assertEqual(check_skills.check(self.root), 0)

    def test_hiss_gate_refuses_planted_long_function_and_passes_clean(self):
        if not shutil.which('praetorctl'):
            self.skipTest('praetorctl not on PATH')
        long_func = 'def f():\n' + ''.join(f'    x{i} = {i}\n' for i in range(70))
        pin = self.source({
            'pack/bad/SKILL.md': MD.format(name='bad'),
            'pack/bad/LICENSE': LICENCE,
            'pack/bad/scripts/tool.py': long_func,
            'pack/good/SKILL.md': MD.format(name='good'),
            'pack/good/LICENSE': LICENCE,
            'pack/good/scripts/tool.py': 'def f():\n    pass\n',
        })
        self.candidate(name='bad', folder='pack/bad', pin=pin)
        self.candidate(name='good', folder='pack/good', pin=pin)
        rows, _ = self.invoke(hiss_gate=adopt.hiss_gate)
        self.assertEqual([r['verdict'] for r in rows], ['refused', 'adopted'])
        self.assertEqual(rows[0]['reason'], 'hiss: HISS-04 in scripts/tool.py')
        self.assertFalse((self.root / 'skills/bad').exists())
        self.assertTrue((self.root / 'skills/good').exists())

    def test_missing_licence_text_triggers_download_through_runner(self):
        """Download a missing licence into staging with reuse download -o before root changes."""
        files = {'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': 'MIT-0\n'}
        pin = self.source(files)
        row = {
            'source': 'owner/repo', 'path': 'pack/demo/SKILL.md', 'skill_name': 'demo',
            'verdict': 'candidate', 'licence': 'MIT-0', 'licence_evidence': 'folder', 'functions': 'testing',
        }
        self.candidates.append(row)
        self.census.append({'source': 'owner/repo', 'path': 'pack/demo/SKILL.md', 'pin': pin})

        commands = []

        def fake_runner(cmd, *, cwd=None):
            """Check the staged download target and write the licence text there."""
            commands.append((list(cmd), cwd))
            self.assertEqual(cwd.parent, self.root)
            self.assertTrue(cwd.name.startswith('.adopt-publish-'))
            self.assertFalse((self.root / 'skills/demo').exists())
            self.assertFalse((self.root / 'LICENSES/MIT-0.txt').exists())
            out_path = Path(cmd[cmd.index('-o') + 1])
            self.assertEqual(out_path, cwd / 'LICENSES/MIT-0.txt')
            out_path.write_text('Downloaded MIT-0\n', encoding='utf-8')
            return b'Downloaded'

        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertEqual(len(commands), 1)
        cmd, stage_cwd = commands[0]
        self.assertEqual(cmd, ['reuse', 'download', '-o', str(stage_cwd / 'LICENSES/MIT-0.txt'), 'MIT-0'])
        self.assertFalse(stage_cwd.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])
        self.assertEqual((self.root / 'LICENSES/MIT-0.txt').read_text(encoding='utf-8'), 'Downloaded MIT-0\n')
        self.assertTrue((self.root / 'skills/demo').is_dir())
        self.assertIn('demo\towner/repo\ttesting\t\t', (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8'))
        self.assertEqual(check_skills.check(self.root), 0)

    def test_failed_licence_download_fails_run(self):
        """Keep existing skills, metadata, index and licences byte-identical when download fails."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        self.fixture()
        self.invoke(apply=True)
        before = snapshot(self.root)
        self.candidates, self.census = [], []
        pin = self.source({'pack/extra/SKILL.md': MD.format(name='extra'), 'pack/extra/LICENSE': 'MIT-0\n'},
                          source='other/repo')
        self.candidates.append({
            'source': 'other/repo', 'path': 'pack/extra/SKILL.md',
            'skill_name': 'extra', 'verdict': 'candidate', 'licence': 'MIT-0',
            'licence_evidence': 'folder', 'functions': 'testing',
        })
        self.census.append({'source': 'other/repo', 'path': 'pack/extra/SKILL.md', 'pin': pin})

        def failing_runner(cmd, *, cwd=None):
            """Raise a subprocess failure with diagnostic stderr."""
            self.assertIsNotNone(cwd)
            raise subprocess.CalledProcessError(1, cmd, stderr=b'network error')

        with self.assertRaises(ValueError) as ctx:
            self.invoke(apply=True, runner=failing_runner)
        self.assertIn('MIT-0', str(ctx.exception))
        self.assertIn('network error', str(ctx.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_timed_out_licence_download_leaves_root_unchanged(self):
        """Keep root byte-identical with no staging leftovers and name the deadline on timeout."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        self.fixture()
        self.invoke(apply=True)
        before = snapshot(self.root)
        self.candidates, self.census = [], []
        pin = self.source({'pack/extra/SKILL.md': MD.format(name='extra'), 'pack/extra/LICENSE': 'MIT-0\n'},
                          source='other/repo')
        self.candidates.append({
            'source': 'other/repo', 'path': 'pack/extra/SKILL.md',
            'skill_name': 'extra', 'verdict': 'candidate', 'licence': 'MIT-0',
            'licence_evidence': 'folder', 'functions': 'testing',
        })
        self.census.append({'source': 'other/repo', 'path': 'pack/extra/SKILL.md', 'pin': pin})

        def timeout_runner(cmd, *, cwd=None):
            """Simulate a timed-out licence download."""
            self.assertIsNotNone(cwd)
            raise subprocess.TimeoutExpired(cmd, adopt.TIMEOUT)

        with self.assertRaises(ValueError) as ctx:
            self.invoke(apply=True, runner=timeout_runner)
        self.assertIn('MIT-0', str(ctx.exception))
        self.assertIn(f'{adopt.TIMEOUT} seconds', str(ctx.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_present_licence_skips_download_and_writes_index(self):
        """Skip downloads when LICENSES already holds the text or no licence is missing."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text('Existing MIT text\n', encoding='utf-8')
        self.fixture()

        def unexpected_runner(cmd, *, cwd=None):
            """Fail if a download is attempted for a licence already present."""
            self.assertIsNotNone(cwd)
            self.fail(f'unexpected download runner call: {cmd}')

        rows, _ = self.invoke(apply=True, runner=unexpected_runner)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertEqual((self.root / 'LICENSES/MIT.txt').read_text(encoding='utf-8'), 'Existing MIT text\n')
        self.assertIn('demo\towner/repo\ttesting;coding\t\t',
                      (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8'))
        (self.root / 'skills/INDEX.tsv').unlink()
        adopt.publish(self.root, self.base, [], 'version = 1\n', '# Credits\n', runner=unexpected_runner)
        self.assertIn('demo\towner/repo\t\t\t', (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8'))
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

    def test_default_download_runner_enforces_deadline_and_maps_errors(self):
        """Pass TIMEOUT to the command runner and map non-zero exit or timeout to ValueError."""
        cmd = ['reuse', 'download', '-o', str(self.base / 'MIT-0.txt'), 'MIT-0']
        ok = subprocess.CompletedProcess(cmd, 0, stdout=b'ok', stderr=b'')
        with patch.object(adopt.subprocess, 'run', return_value=ok) as mocked:
            self.assertEqual(adopt.default_download_runner(cmd, cwd=self.root), b'ok')
        self.assertEqual(mocked.call_args.kwargs['timeout'], adopt.TIMEOUT)
        self.assertEqual(mocked.call_args.kwargs['cwd'], self.root)

        bad = subprocess.CompletedProcess(cmd, 1, stdout=b'', stderr=b'bad spdx identifier\n')
        with (patch.object(adopt.subprocess, 'run', return_value=bad),
              self.assertRaisesRegex(ValueError, r'licence download failed for MIT-0: bad spdx identifier')):
            adopt.default_download_runner(cmd, cwd=self.root)

        expired = subprocess.TimeoutExpired(cmd, adopt.TIMEOUT)
        timed_out = rf'licence download failed for MIT-0: timed out after {adopt.TIMEOUT} seconds'
        with (patch.object(adopt.subprocess, 'run', side_effect=expired),
              self.assertRaisesRegex(ValueError, timed_out)):
            adopt.default_download_runner(cmd, cwd=self.root)

    def test_silent_missing_licence_file_raises_and_leaves_root_unchanged(self):
        """Fail before touching root when the download runner exits cleanly without writing LICENSES/<id>.txt."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text('Existing MIT\n', encoding='utf-8')
        self.fixture()
        self.invoke(apply=True)
        before = snapshot(self.root)

        self.candidates, self.census = [], []
        pin = self.source({'pack/extra/SKILL.md': MD.format(name='extra'), 'pack/extra/LICENSE': 'MIT-0\n'},
                          source='other/repo')
        self.candidates.append({
            'source': 'other/repo', 'path': 'pack/extra/SKILL.md',
            'skill_name': 'extra', 'verdict': 'candidate', 'licence': 'MIT-0',
            'licence_evidence': 'folder', 'functions': 'testing',
        })
        self.census.append({'source': 'other/repo', 'path': 'pack/extra/SKILL.md', 'pin': pin})

        def silent_noop_runner(_cmd, *, cwd=None):
            """Return without creating the requested output file."""
            self.assertTrue(cwd.is_dir())
            return b'ok'

        with self.assertRaisesRegex(ValueError, r'licence download failed for MIT-0: MIT-0\.txt not written'):
            self.invoke(apply=True, runner=silent_noop_runner)
        self.assertEqual(snapshot(self.root), before)

        direct_root = self.base / 'direct-root'
        direct_root.mkdir()
        direct_before = snapshot(direct_root)
        reuse_doc = (
            'version = 1\n\n[[annotations]]\npath = ["skills/x/**"]\n'
            'SPDX-FileCopyrightText = "2025 x"\nSPDX-License-Identifier = "MIT-0"\n'
        )
        with self.assertRaisesRegex(ValueError, r'licence download failed for MIT-0: MIT-0\.txt not written'):
            adopt.sync_licences(direct_root, reuse_doc, runner=silent_noop_runner)
        self.assertEqual(snapshot(direct_root), direct_before)

    def _two_mit0_candidates(self):
        """Populate two MIT-0 candidates and return a deterministic download runner and call log."""
        pin = self.source({
            'pack/first/SKILL.md': MD.format(name='first'), 'pack/first/LICENSE': 'MIT-0\n',
            'pack/second/SKILL.md': MD.format(name='second'), 'pack/second/LICENSE': 'MIT-0\n',
        })
        for name, dom in (('first', 'science'), ('second', 'dev')):
            self.candidates.append({
                'source': 'owner/repo', 'path': f'pack/{name}/SKILL.md',
                'skill_name': name, 'verdict': 'candidate', 'licence': 'MIT-0',
                'licence_evidence': 'folder', 'functions': 'testing', 'domains': dom,
            })
            self.census.append({'source': 'owner/repo', 'path': f'pack/{name}/SKILL.md', 'pin': pin})
        downloads = []

        def fake_runner(cmd, *, cwd=None):
            """Write a deterministic licence file into the staged output target."""
            self.assertIsNotNone(cwd)
            downloads.append(list(cmd))
            Path(cmd[cmd.index('-o') + 1]).write_text('MIT-0 text\n', encoding='utf-8')
            return b'ok'

        return fake_runner, downloads

    def _seed_existing_skill(self):
        """Adopt one MIT skill so root starts with populated metadata, LICENSES and INDEX.tsv."""
        (self.root / 'LICENSES').mkdir(exist_ok=True)
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        pin = self.source({'pack/seed/SKILL.md': MD.format(name='seed'), 'pack/seed/LICENSE': LICENCE},
                          source='seed/repo')
        self.candidate(name='seed', folder='pack/seed', source='seed/repo', pin=pin, domains='core')
        self.invoke(apply=True)
        self.candidates, self.census = [], []

    def test_crash_between_publish_renames_and_next_apply_repairs(self):
        """Roll back root byte-identical on a mid-publish failure and retry both accepted and withdrawn."""
        fake_runner, downloads = self._two_mit0_candidates()
        (self.root / '.adopt-skills.lock').touch()
        before_empty = snapshot(self.root)
        real_replace = os.replace

        def fail_second_skill(src, dst):
            """Fail right after installing the first skill directory."""
            if Path(dst) == self.root / 'skills/second':
                err = OSError('simulated crash between skill renames')
                raise err
            real_replace(src, dst)

        with (patch.object(adopt.os, 'replace', side_effect=fail_second_skill),
              self.assertRaisesRegex(OSError, 'simulated crash between skill renames')):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_empty)
        self.assertEqual(len(downloads), 1)

        def refuse_second(_root, _staging, skill):
            """Simulate withdrawing or refusing the second skill on retry."""
            if skill.name == 'second':
                err = adopt.Refused('markdown: withdrawn')
                raise err

        rows_withdrawn, _ = self.invoke(apply=True, runner=fake_runner, gate=refuse_second)
        self.assertEqual([r['verdict'] for r in rows_withdrawn], ['adopted', 'refused'])
        self.assertEqual(len(downloads), 2)
        self.assertEqual(check_skills.check(self.root), 0)
        for rel in ('REUSE.toml', 'CREDITS.md', 'skills/INDEX.tsv'):
            self.assertNotIn('second', (self.root / rel).read_text(encoding='utf-8'))

        rows_accepted, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual([r['verdict'] for r in rows_accepted], ['present', 'adopted'])
        self.assertEqual(len(downloads), 2)
        self.assertEqual(check_skills.check(self.root), 0)
        index_lines = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8').splitlines()
        self.assertIn('first\towner/repo\ttesting\tscience\t', index_lines)
        self.assertIn('second\towner/repo\ttesting\tdev\t', index_lines)

    def test_crash_rollback_restores_populated_root_byte_identical(self):
        """Restore pre-existing metadata, index and skills byte-identical when a later batch fails mid-commit."""
        fake_runner, _ = self._two_mit0_candidates()
        self.candidates = [self.candidates[0]]
        self.census = [self.census[0]]
        self.invoke(apply=True, runner=fake_runner)
        before_populated = snapshot(self.root)

        pin_extra = self.source({
            'pack/two/SKILL.md': MD.format(name='two'), 'pack/two/LICENSE': 'MIT-0\n',
            'pack/three/SKILL.md': MD.format(name='three'), 'pack/three/LICENSE': 'MIT-0\n',
        }, source='other/repo')
        for name in ('two', 'three'):
            self.candidates.append({
                'source': 'other/repo', 'path': f'pack/{name}/SKILL.md',
                'skill_name': name, 'verdict': 'candidate', 'licence': 'MIT-0',
                'licence_evidence': 'folder', 'functions': 'testing', 'domains': 'ops',
            })
            self.census.append({'source': 'other/repo', 'path': f'pack/{name}/SKILL.md', 'pin': pin_extra})

        real_replace = os.replace

        def fail_three(src, dst):
            """Fail on the final rename after overwriting metadata and installing skill two."""
            if Path(dst) == self.root / 'skills/three':
                err = OSError('simulated crash on three')
                raise err
            real_replace(src, dst)

        with (patch.object(adopt.os, 'replace', side_effect=fail_three),
              self.assertRaisesRegex(OSError, 'simulated crash on three')):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_populated)

    def test_output_symlink_refuses_index_and_licenses_targets(self):
        """Refuse symlinked skills/INDEX.tsv (in-root, outside, dangling) and LICENSES; publish regular files."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self.candidates = [self.candidates[0]]
        self.census = [self.census[0]]
        index_path = self.root / 'skills/INDEX.tsv'
        regular_index = index_path.read_bytes()
        outside_index = self.base / 'outside-index.tsv'
        outside_index.write_bytes(regular_index)
        outside_licenses = self.base / 'outside-licenses'
        outside_licenses.mkdir()

        for link_target in (self.root / 'CREDITS.md', outside_index, self.base / 'dangling-index.tsv'):
            index_path.unlink()
            index_path.symlink_to(link_target)
            before = snapshot(self.root)
            for apply_mode in (False, True):
                with self.assertRaisesRegex(ValueError, r'skills/INDEX\.tsv: output symlink refused'):
                    self.invoke(apply=apply_mode, runner=fake_runner)
                self.assertEqual(snapshot(self.root), before)

        index_path.unlink()
        index_path.write_bytes(regular_index)
        shutil.rmtree(self.root / 'LICENSES')
        for lic_target in (outside_licenses, self.base / 'dangling-licenses'):
            (self.root / 'LICENSES').symlink_to(lic_target)
            before = snapshot(self.root)
            for apply_mode in (False, True):
                with self.assertRaisesRegex(ValueError, r'LICENSES: output symlink refused'):
                    self.invoke(apply=apply_mode, runner=fake_runner)
                self.assertEqual(snapshot(self.root), before)
            (self.root / 'LICENSES').unlink()

        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertFalse(index_path.is_symlink())
        self.assertTrue((self.root / 'LICENSES/MIT-0.txt').is_file())
        self.assertEqual(list(outside_licenses.iterdir()), [])
        self.assertEqual(check_skills.check(self.root), 0)

    def test_rollback_failure_keeps_staging_names_backups_and_runs_other_undos(self):
        """Keep staging, report failed target and backup path, preserve cause, and complete remaining undos."""
        self._seed_existing_skill()
        pre_reuse = (self.root / 'REUSE.toml').read_text(encoding='utf-8')
        pre_credits = (self.root / 'CREDITS.md').read_text(encoding='utf-8')
        pre_index = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8')
        fake_runner, _ = self._two_mit0_candidates()
        real_replace = os.replace
        reuse_writes = 0

        def fail_commit_and_reuse_restore(src, dst):
            """Fail on the second skill rename and then fail restoring REUSE.toml during rollback."""
            nonlocal reuse_writes
            dst_path = Path(dst)
            if dst_path == self.root / 'REUSE.toml':
                reuse_writes += 1
                if reuse_writes >= 2:
                    err = OSError('simulated reuse restore failure')
                    raise err
            if dst_path == self.root / 'skills/second':
                err = OSError('simulated commit failure')
                raise err
            real_replace(src, dst)

        with (patch.object(adopt.os, 'replace', side_effect=fail_commit_and_reuse_restore),
              self.assertRaises(RuntimeError) as ctx):
            self.invoke(apply=True, runner=fake_runner)

        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertIn('simulated commit failure', str(ctx.exception.__cause__))
        staged_dirs = [p for p in self.root.iterdir() if p.is_dir() and p.name.startswith('.adopt-publish-')]
        self.assertEqual(len(staged_dirs), 1)
        staging = staged_dirs[0]
        backup_reuse = staging / 'backup/0-REUSE.toml'
        self.assertTrue(backup_reuse.is_file())
        self.assertEqual(backup_reuse.read_text(encoding='utf-8'), pre_reuse)
        self.assertIn('REUSE.toml', str(ctx.exception))
        self.assertIn(str(backup_reuse), str(ctx.exception))
        self.assertFalse((self.root / 'skills/first').exists())
        self.assertTrue((staging / 'skills/first').is_dir())
        self.assertFalse((self.root / 'LICENSES/MIT-0.txt').exists())
        self.assertEqual((self.root / 'CREDITS.md').read_text(encoding='utf-8'), pre_credits)
        self.assertEqual((self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8'), pre_index)

    def test_rollback_without_failure_and_clean_publish_remove_staging(self):
        """Remove staging both when rollback succeeds completely and when publish succeeds."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        real_replace = os.replace

        def fail_second_only(src, dst):
            """Fail only the second skill rename so rollback succeeds completely."""
            if Path(dst) == self.root / 'skills/second':
                err = OSError('simulated commit failure')
                raise err
            real_replace(src, dst)

        with (patch.object(adopt.os, 'replace', side_effect=fail_second_only),
              self.assertRaises(OSError)):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual([r['verdict'] for r in rows], ['adopted', 'adopted'])
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

    def _fork_kill_before_replace(self, fake_runner, stop_dest: Path, exit_code: int = 99):
        """Run --apply in a child process that exits via os._exit right before replacing stop_dest."""
        if not hasattr(os, 'fork'):
            self.skipTest('os.fork unavailable')
        real_replace = os.replace
        pid = os.fork()
        if pid == 0:
            def kill_at_dest(src, dst):
                """Exit immediately without running Python cleanup handlers."""
                if Path(dst) == stop_dest:
                    os._exit(exit_code)
                real_replace(src, dst)

            with patch.object(adopt.os, 'replace', side_effect=kill_at_dest):
                self.invoke(apply=True, runner=fake_runner)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == exit_code)

    def _fork_kill_on_second_skill(self, fake_runner):
        """Run --apply in a child process that exits via os._exit right before installing skills/second."""
        self._fork_kill_before_replace(fake_runner, self.root / 'skills/second', exit_code=99)

    def test_hard_kill_between_publish_renames_measured(self):
        """Reconcile a hard-killed publish on the next --apply so withdrawn skills leave no attribution."""
        self._seed_existing_skill()
        before_seed = snapshot(self.root)
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)

        self.assertTrue((self.root / adopt.JOURNAL_NAME).is_file())
        self.assertTrue((self.root / 'LICENSES/MIT-0.txt').is_file())
        self.assertTrue((self.root / 'skills/first').is_dir())
        self.assertIn('skills/second/**', (self.root / 'REUSE.toml').read_text(encoding='utf-8'))

        root_withdrawn = self.base / 'root-withdrawn'
        root_empty = self.base / 'root-empty'
        shutil.copytree(self.root, root_withdrawn)
        shutil.copytree(self.root, root_empty)

        tsv(self.base / 'cand-one.tsv', [*CANDIDATE_FIELDS, 'domains'], [self.candidates[0]])
        rows_withdraw, _ = adopt.run(self.base / 'cand-one.tsv', self.base / 'groups.tsv',
                                     self.base / 'census.tsv', self.checkouts, root_withdrawn,
                                     apply=True, gate=passing_gate, hiss_gate=passing_gate, runner=fake_runner)
        self.assertEqual([r['verdict'] for r in rows_withdraw], ['adopted'])
        self.assertEqual(check_skills.check(root_withdrawn), 0)
        reuse_text = (root_withdrawn / 'REUSE.toml').read_text(encoding='utf-8')
        credits_text = (root_withdrawn / 'CREDITS.md').read_text(encoding='utf-8')
        self.assertIn('skills/seed/**', reuse_text)
        self.assertIn('skills/first/**', reuse_text)
        self.assertNotIn('second', reuse_text)
        self.assertIn('`seed`', credits_text)
        self.assertIn('`first`', credits_text)
        self.assertNotIn('second', credits_text)
        self.assertEqual([p.name for p in root_withdrawn.iterdir() if p.name.startswith('.adopt-publish')], [])

        tsv(self.base / 'cand-empty.tsv', CANDIDATE_FIELDS, [])
        adopt.run(self.base / 'cand-empty.tsv', self.base / 'groups.tsv', self.base / 'census.tsv',
                  self.checkouts, root_empty, apply=True, gate=passing_gate, hiss_gate=passing_gate,
                  runner=fake_runner)
        self.assertEqual(snapshot(root_empty), before_seed)

    def test_reconcile_interrupted_mid_recovery_is_idempotent(self):
        """Complete reconciliation cleanly after a hard kill interrupts recovery past a real undo."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)
        self.assertTrue((self.root / 'skills/first').is_dir())

        real_undo = adopt._undo_journal_entry
        pid = os.fork()
        if pid == 0:
            def kill_after_first_skill_undo(kind, path, other=None, source=None):
                """Exit immediately once recovery removes the installed skills/first directory."""
                real_undo(kind, path, other, source)
                if path == self.root / 'skills/first' and not path.exists():
                    os._exit(98)

            with patch.object(adopt, '_undo_journal_entry', side_effect=kill_after_first_skill_undo):
                self.invoke(apply=True, runner=fake_runner)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 98)
        self.assertFalse((self.root / 'skills/first').exists())
        self.assertTrue((self.root / adopt.JOURNAL_NAME).is_file())

        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual([r['verdict'] for r in rows], ['adopted', 'adopted'])
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])
        self.assertEqual(check_skills.check(self.root), 0)

    def test_journal_preview_malformed_outside_root_and_symlink_refused(self):
        """Refuse previews, malformed journals, escaped paths and symlinks without modifying root."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self.assertEqual([r['verdict'] for r in self.invoke(apply=False)[0]], ['adopted', 'adopted'])
        self._fork_kill_on_second_skill(fake_runner)
        journal_file = self.root / adopt.JOURNAL_NAME
        valid_payload = json.loads(journal_file.read_text(encoding='utf-8'))

        before_preview = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, r'\.adopt-publish\.journal\.json.*--apply'):
            self.invoke(apply=False, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_preview)

        bad_payloads = [
            '{malformed',
            json.dumps({**valid_payload, 'version': 2}),
            json.dumps({**valid_payload, 'staging': '../outside'}),
            json.dumps({**valid_payload, 'moves': [
                {'kind': 'skill', 'source': f"{valid_payload['staging']}/skills/second", 'destination': '../evil'},
            ]}),
        ]
        for raw_json in bad_payloads:
            journal_file.write_text(raw_json, encoding='utf-8')
            before_bad = snapshot(self.root)
            with self.assertRaisesRegex(ValueError, r'\.adopt-publish\.journal\.json'):
                self.invoke(apply=True, runner=fake_runner)
            self.assertEqual(snapshot(self.root), before_bad)

        journal_file.write_text(json.dumps(valid_payload), encoding='utf-8')
        outside_dir = self.base / 'outside-skill'
        outside_dir.mkdir()
        shutil.rmtree(self.root / 'skills/first')
        (self.root / 'skills/first').symlink_to(outside_dir)
        before_sym = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, r'\.adopt-publish\.journal\.json'):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_sym)

        journal_file.unlink()
        journal_file.symlink_to(outside_dir / 'missing.json')
        before_jlink = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, r'\.adopt-publish\.journal\.json'):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_jlink)

    def test_report_path_written_and_root_refused(self):
        self.fixture()
        # Default: no report at root
        rows, _ = self.invoke()
        self.assertFalse((self.root / 'adopt-report.tsv').exists())

        # Specific report path
        out_report = self.base / 'custom' / 'report.tsv'
        self.assertEqual(adopt.resolve_report_path(out_report, self.root), out_report.resolve())
        rows, _ = self.invoke(report=out_report)
        self.assertTrue(out_report.is_file())
        self.assertEqual(out_report.read_text(encoding='utf-8'), adopt.report_text(rows))
        self.assertFalse((self.root / 'adopt-report.tsv').exists())

        # Root and tool-owned root outputs refused as report locations
        for bad_root in (self.root, self.root / 'adopt-report.tsv'):
            with self.assertRaisesRegex(ValueError, 'repository root refused'):
                self.invoke(report=bad_root)
        for rel in (adopt.JOURNAL_NAME, '.adopt-skills.lock', *adopt.JOURNAL_BACKUP_TARGETS):
            with self.assertRaisesRegex(ValueError, 'refused as report location'):
                adopt.resolve_report_path(self.root / rel, self.root)
            with self.assertRaisesRegex(ValueError, 'refused as report location'):
                self.invoke(report=self.root / rel)

    def test_group_adopts_original_and_refuses_composio_copy(self):
        """Collapse a copy only when matching embeddings support it."""
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob', domains='coding;testing')
        copy_pin = self.source({'pack/demo/SKILL.md': COPY_MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob', domains='testing')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.embedding_cache({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]})
        with self.assertRaisesRegex(ValueError, r'python3 tools/overlap\.py run --cache .* --embed-model other-model') as error:
            self.invoke(apply=False, include_grouped=True, cache=cache_file, embed_model='other-model')
        self.assertIn(str(cache_file), str(error.exception))
        copy_checkout = self.checkouts / copy_cand['source'].replace('/', '__')
        adopt.git(copy_checkout, 'sparse-checkout', 'set', '--no-cone', '--stdin', data=b'**/LICENSE\n')
        self.assertFalse((copy_checkout / copy_cand['path']).exists())
        rows, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file)
        self.assertEqual(len(rows), 2)
        # Anthropics original adopted
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertEqual(rows[0]['directory'], 'skills/demo')
        # Composio copy refused as copy-of original
        self.assertEqual(rows[1]['verdict'], 'refused')
        self.assertEqual(rows[1]['reason'], f'copy-of:{orig_cand["source"]}:{orig_cand["path"]}')
        self.assertTrue((self.root / 'skills/demo').is_dir())
        self.assertFalse((self.root / 'skills/demo-composiohq').exists())
        # Check INDEX.tsv has demo with overlap_group 101
        index_text = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8')
        lines = index_text.splitlines()
        self.assertEqual(lines[0], adopt.INDEX_HEADER)
        row_demo = [l for l in lines[1:] if l.startswith('demo\t')][0]
        parts = row_demo.split('\t')
        self.assertEqual(parts[0], 'demo')
        self.assertEqual(parts[1], 'anthropics/skills')
        self.assertEqual(parts[2], 'testing;coding')
        self.assertEqual(parts[3], 'coding;testing')
        self.assertEqual(parts[4], '101')
        self.assertEqual(check_skills.check(self.root), 0)

    def test_partial_group_cache_is_reported_before_apply(self):
        """Defer both members when a matching namespace lacks a current vector."""
        for name, source in (('alpha', 'anthropics/skills'), ('beta', 'obra/superpowers')):
            pin = self.source({'SKILL.md': MD.format(name=name), 'LICENSE': LICENCE}, source=source)
            self.candidate(name=name, folder='', source=source, pin=pin, blob_id=name)
        members = [adopt.candidate_id(row) for row in self.candidates]
        self.groups = [{'group_id': '42', 'members': ','.join(members)}]
        cache_file = self.embedding_cache({'alpha': [1.0, 0.0]})
        stderr = io.StringIO()

        def publish_after_report(*args, **kwargs):
            """Check that the missing-vector report precedes application."""
            self.assertIn(f'group 42: missing embedding vectors for {members[1]}', stderr.getvalue())
            self.assertIn('python3 tools/overlap.py run --cache', stderr.getvalue())
            publish(*args, **kwargs)

        publish = adopt.publish
        with redirect_stderr(stderr), patch.object(adopt, 'publish', side_effect=publish_after_report):
            rows, _ = self.invoke(include_grouped=True, cache=cache_file)
        self.assertEqual([row['reason'] for row in rows], ['missing-embedding-vector'] * 2)
        self.assertFalse((self.root / 'skills/alpha').exists())
        self.assertFalse((self.root / 'skills/beta').exists())

    def test_partial_group_cache_still_resolves_explicit_provenance(self):
        """Adopt the named original and refuse its copy without vector evidence for either."""
        upstream = 'anthropics/skills'
        for name, source, provenance in (
            ('original', upstream, ''), ('copy', 'obra/superpowers', f'\nsource_repo: {upstream}'),
            ('unrelated', 'owner/repo', ''),
        ):
            text = MD.format(name=name).replace('\n---\n', f'{provenance}\n---\n')
            pin = self.source({'SKILL.md': text, 'LICENSE': LICENCE}, source=source)
            self.candidate(name=name, folder='', source=source, pin=pin, blob_id=name)
        members = [adopt.candidate_id(row) for row in self.candidates]
        self.groups = [{'group_id': '43', 'members': ','.join(members)}]
        cache_file = self.embedding_cache({'unrelated': [1.0, 0.0]})
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rows, _ = self.invoke(include_grouped=True, cache=cache_file)
        self.assertIn(f'group 43: missing embedding vectors for {members[0]}, {members[1]}', stderr.getvalue())
        self.assertEqual([row['verdict'] for row in rows], ['adopted', 'refused', 'refused'])
        self.assertEqual(rows[1]['reason'], f'copy-of:{members[0]}')
        self.assertEqual(rows[2]['reason'], 'missing-embedding-vector')

    def test_group_different_skills_adopts_both_with_group_id(self):
        """Adopt both same-name members when orthogonal embeddings show distinct uses."""
        md_a = (
            '---\nname: alpha_skill\ndescription: Use when testing scientific workflows.\n---\n\n'
            '# Alpha\n\nCheck science.\n'
        )
        md_b = (
            '---\nname: alpha-skill\ndescription: Use when testing developer workflows.\n---\n\n'
            '# Alpha\n\nCheck dev.\n'
        )
        pin_a = self.source({'pack/alpha/SKILL.md': md_a, 'pack/alpha/LICENSE': LICENCE},
                            source='anthropics/skills')
        cand_a = self.candidate(name='alpha_skill', folder='pack/alpha', source='anthropics/skills', pin=pin_a,
                                blob_id='alpha_blob', domains='science')
        pin_b = self.source({'pack/beta/SKILL.md': md_b, 'pack/beta/LICENSE': LICENCE},
                            source='obra/superpowers')
        cand_b = self.candidate(name='alpha-skill', folder='pack/beta', source='obra/superpowers', pin=pin_b,
                                blob_id='beta_blob', domains='dev')
        self.assertEqual(adopt.normalize_name(cand_a['skill_name']), adopt.normalize_name(cand_b['skill_name']))
        self.groups = [{
            'group_id': '42',
            'members': f'{cand_a["source"]}:{cand_a["path"]},{cand_b["source"]}:{cand_b["path"]}',
        }]
        cache_file = self.embedding_cache({'alpha_blob': [1.0, 0.0], 'beta_blob': [0.0, 1.0]})
        rows, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file)
        self.assertEqual([r['verdict'] for r in rows], ['adopted', 'adopted'])
        self.assertEqual([r['directory'] for r in rows], ['skills/alpha-skill', 'skills/alpha-skill-obra'])
        self.assertTrue((self.root / 'skills/alpha-skill').is_dir())
        self.assertTrue((self.root / 'skills/alpha-skill-obra').is_dir())
        lines = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8').splitlines()
        row_alpha = next(line for line in lines[1:] if line.startswith('alpha-skill\t'))
        row_beta = next(line for line in lines[1:]
                        if line.startswith('alpha-skill-obra\t'))
        self.assertEqual(row_alpha.split('\t')[3:5], ['science', '42'])
        self.assertEqual(row_beta.split('\t')[3:5], ['dev', '42'])
        self.assertEqual(check_skills.check(self.root), 0)

    def test_original_refused_by_gate_leaves_copy_refused(self):
        """Keep a copy refused when its original fails the adoption gate."""
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob')
        copy_pin = self.source({'pack/demo/SKILL.md': COPY_MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.embedding_cache({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]})

        def gate_reject_demo(root, staging, skill):
            """Refuse the original skill staged under its canonical name."""
            if skill.name == 'demo':
                raise adopt.Refused('markdown: reject original')

        rows, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file, gate=gate_reject_demo)
        self.assertEqual(rows[0]['verdict'], 'refused')
        self.assertEqual(rows[0]['reason'], 'markdown: reject original')
        self.assertEqual(rows[1]['verdict'], 'refused')
        self.assertEqual(rows[1]['reason'], f'copy-of:{orig_cand["source"]}:{orig_cand["path"]}')
        self.assertFalse((self.root / 'skills/demo').exists())
        self.assertFalse((self.root / 'skills/demo-composiohq').exists())

    def test_index_tsv_lists_every_skill_and_check_refuses_missing_and_extra(self):
        self.fixture()
        self.invoke(apply=True)
        self.assertTrue((self.root / 'skills/demo').is_dir())
        self.assertTrue((self.root / 'skills/INDEX.tsv').is_file())
        self.assertEqual(check_skills.check(self.root), 0)
        # Check missing row refused
        check_skills.write_index(self.root, [])
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any('missing row for demo' in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)
        # Check extra row refused
        check_skills.write_index(self.root, [{'skill': 'demo', 'source': 'owner/repo'}, {'skill': 'extra', 'source': 'owner/repo'}])
        problems = check_skills.index_problems(self.root)
        self.assertTrue(any('extra row for extra' in p for p in problems), problems)
        self.assertEqual(check_skills.check(self.root), 1)

    def test_already_adopted_candidate_is_present_and_no_second_directory(self):
        self.fixture()
        rows, totals = self.invoke(apply=True)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertTrue((self.root / 'skills/demo').is_dir())
        self.assertEqual(check_skills.check(self.root), 0)

        rows2, totals2 = self.invoke(apply=True)
        self.assertEqual(rows2[0]['verdict'], 'present')
        self.assertEqual(rows2[0]['directory'], 'skills/demo')
        self.assertEqual(rows2[0]['reason'], '')
        self.assertFalse((self.root / 'skills/demo-owner').exists())
        dirs = [p.name for p in (self.root / 'skills').iterdir() if p.is_dir()]
        self.assertEqual(dirs, ['demo'])
        self.assertIn('present=1', totals2)
        self.assertIn('adopted=0', totals2)
        self.assertEqual(check_skills.check(self.root), 0)

    def test_present_candidate_different_commit_reported_not_rewritten(self):
        self.fixture()
        pin1 = self.census[0]['pin']
        rows1, _ = self.invoke(apply=True)
        self.assertEqual(rows1[0]['verdict'], 'adopted')
        origin1 = (self.root / 'skills/demo/ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn(f'commit: {pin1}', origin1)

        pin2 = 'f' * 40
        self.census[0]['pin'] = pin2
        rows2, _ = self.invoke(apply=True)
        self.assertEqual(rows2[0]['verdict'], 'present')
        self.assertEqual(rows2[0]['directory'], 'skills/demo')
        self.assertEqual(rows2[0]['reason'], f'present: pinned {pin1[:12]}, census {pin2[:12]}')
        origin2 = (self.root / 'skills/demo/ORIGIN.md').read_text(encoding='utf-8')
        self.assertEqual(origin1, origin2)
        self.assertIn(f'commit: {pin1}', origin2)
        self.assertNotIn(pin2, origin2)

    def test_group_whose_original_is_present_keeps_copy_refused(self):
        """Keep a copy refused when its original has already been adopted."""
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                                source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob', domains='coding;testing')
        copy_pin = self.source({'pack/demo/SKILL.md': COPY_MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                                source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob', domains='testing')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.embedding_cache({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]})

        rows1, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file)
        self.assertEqual(rows1[0]['verdict'], 'adopted')
        self.assertEqual(rows1[1]['verdict'], 'refused')
        self.assertEqual(rows1[1]['reason'], f'copy-of:{orig_cand["source"]}:{orig_cand["path"]}')
        self.assertTrue((self.root / 'skills/demo').is_dir())

        rows2, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file)
        self.assertEqual(rows2[0]['verdict'], 'present')
        self.assertEqual(rows2[0]['directory'], 'skills/demo')
        self.assertEqual(rows2[1]['verdict'], 'refused')
        self.assertEqual(rows2[1]['reason'], f'copy-of:{orig_cand["source"]}:{orig_cand["path"]}')
        self.assertFalse((self.root / 'skills/demo-composiohq').exists())
        dirs = [p.name for p in (self.root / 'skills').iterdir() if p.is_dir()]
        self.assertEqual(dirs, ['demo'])

    def test_cli_grouped_cache_requires_embed_model(self):
        """Reject grouped adoption without an embedding model before inspecting inputs."""
        argv = ['--candidates', 'candidates.tsv', '--groups', 'groups.tsv', '--census', 'census.tsv',
                '--checkouts', 'checkouts', '--cache', 'cache.json', '--include-grouped']
        stderr = io.StringIO()
        with (patch.dict(os.environ, {'PERITIA_LLM_EMBED_MODEL': ''}), redirect_stderr(stderr),
              self.assertRaises(SystemExit) as error):
            os.environ.pop('PERITIA_LLM_BASE_URL', None)
            adopt.main(argv)
        self.assertEqual(error.exception.code, 2)
        self.assertIn('--include-grouped requires --cache and --embed-model', stderr.getvalue())

    def test_root_skill_member_falls_back_to_adopted_text(self):
        """Find a root-level member by the dot path in the adopted-skill index."""
        skills = self.root / 'skills'
        adopted = skills / 'demo'
        adopted.mkdir(parents=True)
        text = MD.format(name='demo')
        (adopted / 'SKILL.md').write_text(text, encoding='utf-8')
        (adopted / 'ORIGIN.md').write_text('upstream: <https://github.com/owner/repo>\npath: .\n', encoding='utf-8')
        self.assertEqual(adopt.get_member_skill_text('owner/repo:SKILL.md', self.checkouts, skills), text)

    def test_fresh_candidate_with_colliding_name_gets_owner_suffix(self):
        pin_other = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                                source='other/repo')
        self.candidate(name='demo', folder='pack/demo', source='other/repo', pin=pin_other)
        rows1, _ = self.invoke(apply=True)
        self.assertEqual(rows1[0]['verdict'], 'adopted')
        self.assertEqual(rows1[0]['directory'], 'skills/demo')
        self.assertTrue((self.root / 'skills/demo').is_dir())

        self.candidates, self.census = [], []
        pin_fresh = self.source({'pack/custom/SKILL.md': MD.format(name='demo'), 'pack/custom/LICENSE': LICENCE},
                                source='owner/repo')
        self.candidate(name='demo', folder='pack/custom', source='owner/repo', pin=pin_fresh)
        rows2, _ = self.invoke(apply=True)
        self.assertEqual(rows2[0]['verdict'], 'adopted')
        self.assertEqual(rows2[0]['directory'], 'skills/demo-owner')
        skill = self.root / 'skills/demo-owner'
        self.assertTrue(skill.is_dir())
        self.assertIn('name: demo-owner\n', (skill / 'SKILL.md').read_text())
        self.assertIn('name changed from demo to demo-owner', (skill / 'ORIGIN.md').read_text())
        self.assertEqual(check_skills.check(self.root), 0)

    def test_checkout_without_git_is_refused(self):
        checkout = self.checkouts / 'owner__nogit'
        checkout.mkdir()
        (checkout / 'SKILL.md').write_text(MD.format(name='nogit'), encoding='utf-8')
        (checkout / 'LICENSE').write_text(LICENCE, encoding='utf-8')
        self.candidate(name='nogit', folder='', source='owner/nogit', pin='4' * 40)
        rows, _ = self.invoke(apply=True)
        self.assertEqual(rows[0]['verdict'], 'refused', rows)
        self.assertEqual(rows[0]['reason'], 'missing-git-checkout')
        self.assertFalse((self.root / 'skills/nogit').exists())

    def test_normalizer_refuses_inputs_outside_the_skill_folder(self):
        """An absolute path, a .. segment or a link out of the folder is refused before any install or write."""
        folder = self.base / 'norm-inputs'
        folder.mkdir()
        (folder / 'ok.md').write_text('# Ok\n', encoding='utf-8')
        outside = self.base / 'outside.md'
        outside.write_text('# Outside\n', encoding='utf-8')
        (folder / 'link.md').symlink_to(outside)
        script = REPO / 'tools/markdownlint/normalize.mjs'
        node_bin = shutil.which('node')
        if not node_bin:
            self.fail('node executable not found on PATH')
        for bad in (str(outside), '../outside.md', 'link.md'):
            proc = subprocess.run([node_bin, str(script), bad], cwd=folder, capture_output=True,
                                  check=False, timeout=60)
            self.assertNotEqual(proc.returncode, 0, bad)
            self.assertIn(b'refusing Markdown input', proc.stderr, (bad, proc.stderr[:300]))
        self.assertEqual(outside.read_text(encoding='utf-8'), '# Outside\n')

    def test_whitespace_arbiter_omits_a_file_the_normalizer_changes_beyond_whitespace(self):
        """Ruling peritia-code-review-skill-markdown-refinement-0013: a file whose text changes beyond whitespace
        (fence tags aside) stays out and ORIGIN.md lists it; the other files stay normalized."""
        pin = self.source({
            'pack/demo/SKILL.md': MD.format(name='demo'),
            'pack/demo/LICENSE': LICENCE,
            'pack/demo/reference/guide.md': '# Guide\n\nIntro paragraph.\n',
            'pack/demo/reference/keep.md': '# Keep\n\nStays as is.\n',
        })
        self.candidate(pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        fake = self.root / 'tools/markdownlint/normalize.mjs'
        fake.write_text(
            'import fs from "node:fs";\n'
            'const out = {};\n'
            'for (const f of process.argv.slice(2)) {\n'
            '  if (f.endsWith("guide.md")) {\n'
            '    fs.writeFileSync(f, fs.readFileSync(f, "utf8").replace("Intro", "Introduction"));\n'
            '    out[f] = ["MD022"];\n'
            '  }\n'
            '}\n'
            'process.stdout.write(JSON.stringify(out));\n',
            encoding='utf-8')
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        self.assertFalse((skill / 'reference/guide.md').exists())
        self.assertTrue((skill / 'reference/keep.md').is_file())
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn('omitted files the whitespace normalizer would change beyond whitespace (reference/guide.md)', origin)

    def test_markdown_normalizer_fixes_supporting_files_and_records_rules_and_fails_closed(self):
        """Record markdown repairs and refuse adoption if the normalizer is unavailable."""
        unnormalized_ref = (
            '# Reference\n'
            '## Tight Heading\n'
            'Intro paragraph:\n'
            '- item one\n'
            '```python\n'
            'x = 1\n'
            '```\n'
            'After fence.\n\n'
            '| Header One | Header Two |\n'
            '|---|---|\n'
            '| 1 | 2 |'
        )
        pin = self.source({
            'pack/demo/SKILL.md': MD.format(name='demo'),
            'pack/demo/LICENSE': LICENCE,
            'pack/demo/reference/guide.md': unnormalized_ref,
        })
        self.candidate(pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        self.assertTrue((skill / 'reference/guide.md').is_file())
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn(
            'normalized Markdown whitespace (reference/guide.md (MD022, MD031, MD032, MD047, MD060))',
            origin,
        )
        fenced_upstream = self.base / 'fenced-upstream.md'
        fenced_text, _ = adopt.rewrite_fences(unnormalized_ref)
        fenced_upstream.write_text(fenced_text, encoding='utf-8')
        git_bin = shutil.which('git')
        if not git_bin:
            self.fail('git executable not found on PATH')
        diff_proc = subprocess.run(
            [git_bin, 'diff', '--no-index', '-w', '--ignore-blank-lines',
             str(fenced_upstream), str(skill / 'reference/guide.md')],
            capture_output=True, check=False, timeout=adopt.TIMEOUT,
        )
        self.assertEqual(diff_proc.returncode, 0, diff_proc.stdout.decode('utf-8', errors='replace'))
        self.assertEqual(diff_proc.stdout, b'')
        # Fail-closed when normalizer script is broken or absent
        shutil.rmtree(skill)
        (self.root / 'tools/markdownlint/normalize.mjs').unlink()
        rows_fail, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows_fail[0]['verdict'], 'refused', rows_fail)
        self.assertEqual(rows_fail[0]['reason'], 'markdown-normalize')
        self.assertFalse(skill.exists())

    def test_root_skill_omits_furniture_workbench_and_gate_refused_files(self):
        files = {
            'SKILL.md': MD.format(name='root-skill'),
            'LICENSE': LICENCE,
            'README.md': '# Repo readme\n',
            'index.html': '<html></html>\n',
            '.gitignore': '*.pyc\n',
            '.hive/PROTOCOL.md': '# Hive\n',
            'reference/good.md': '# Good\n\nValid reference text.\n',
            'reference/bad.md': '#BadHeading\n',
        }
        pin = self.source(files, source='owner/root-skill')
        self.candidate(name='root-skill', folder='', source='owner/root-skill', pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/root-skill'
        self.assertTrue((skill / 'SKILL.md').is_file())
        self.assertTrue((skill / 'reference/good.md').is_file())
        self.assertFalse((skill / 'README.md').exists())
        self.assertFalse((skill / 'index.html').exists())
        self.assertFalse((skill / '.gitignore').exists())
        self.assertFalse((skill / '.hive').exists())
        self.assertFalse((skill / 'reference/bad.md').exists())
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn('path: .', origin)
        self.assertIn('omitted repository furniture (.gitignore, README.md, index.html)', origin)
        self.assertIn('omitted agent workbench files (.hive/PROTOCOL.md)', origin)
        self.assertIn('omitted files refused by markdown gate (reference/bad.md (MD018))', origin)
        self.assertEqual(check_skills.check(self.root), 0)

    def test_arbiter_planted_listed_patch_passes(self):
        pin = self.source({
            'pack/demo/SKILL.md': MD.format(name='demo'),
            'pack/demo/LICENSE': LICENCE,
            'pack/demo/reference/guide.md': '# Guide\n## Tight\nOld line.\n',
        })
        self.candidate(pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        patch_file = self.root / 'tools/skill-patches/demo/reference/guide.md.patch'
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(
            '# threads: PRRT_test1, PRRT_test2\n'
            '--- a/reference/guide.md\n'
            '+++ b/reference/guide.md\n'
            '@@ -3,3 +3,3 @@\n'
            ' ## Tight\n'
            ' \n'
            '-Old line.\n'
            '+New line.\n',
            encoding='utf-8',
        )
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        self.assertEqual((skill / 'reference/guide.md').read_text(encoding='utf-8'), '# Guide\n\n## Tight\n\nNew line.\n')
        origin = (skill / 'ORIGIN.md').read_text(encoding='utf-8')
        self.assertIn('applied review patches (reference/guide.md (PRRT_test1, PRRT_test2))', origin)

    def test_arbiter_planted_unlisted_edit_is_refused(self):
        pin = self.source({
            'pack/demo/SKILL.md': MD.format(name='demo'),
            'pack/demo/LICENSE': LICENCE,
            'pack/demo/reference/listed.md': '# Listed\n\nOld text.\n',
            'pack/demo/reference/unlisted.md': '# Unlisted\n\nOriginal text.\n',
        })
        self.candidate(pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        patch_file = self.root / 'tools/skill-patches/demo/reference/listed.md.patch'
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(
            '# threads: PRRT_listed\n'
            '--- a/reference/listed.md\n'
            '+++ b/reference/listed.md\n'
            '@@ -1,3 +1,3 @@\n'
            ' # Listed\n'
            ' \n'
            '-Old text.\n'
            '+Patched text.\n',
            encoding='utf-8',
        )
        fake = self.root / 'tools/markdownlint/normalize.mjs'
        fake.write_text(
            'import fs from "node:fs";\n'
            'const out = {};\n'
            'for (const f of process.argv.slice(2)) {\n'
            '  if (f.endsWith("unlisted.md")) {\n'
            '    fs.writeFileSync(f, fs.readFileSync(f, "utf8").replace("Original", "Tampered"));\n'
            '    out[f] = ["MD022"];\n'
            '  }\n'
            '}\n'
            'process.stdout.write(JSON.stringify(out));\n',
            encoding='utf-8',
        )
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'adopted', rows)
        skill = self.root / 'skills/demo'
        self.assertTrue((skill / 'reference/listed.md').is_file())
        self.assertEqual((skill / 'reference/listed.md').read_text(encoding='utf-8'), '# Listed\n\nPatched text.\n')
        self.assertFalse((skill / 'reference/unlisted.md').exists())
        # Direct arbiter check: even a file with a patch is dropped if an extra unlisted edit is present.
        (skill / 'reference/listed.md').write_text('# Listed\n\nPatched text plus unlisted edit.\n', encoding='utf-8')
        dropped = adopt.whitespace_arbiter(
            skill,
            {'reference/listed.md': '# Listed\n\nOld text.\n'},
            patch_dir=self.root / 'tools/skill-patches/demo',
        )
        self.assertEqual(dropped, ['reference/listed.md'])
        self.assertFalse((skill / 'reference/listed.md').exists())

    def test_arbiter_planted_unapplicable_patch_fails_adoption(self):
        pin = self.source({
            'pack/demo/SKILL.md': MD.format(name='demo'),
            'pack/demo/LICENSE': LICENCE,
            'pack/demo/reference/guide.md': '# Guide\n\nUpstream bumped this line.\n',
        })
        self.candidate(pin=pin)
        shutil.copytree(REPO / 'tools/markdownlint', self.root / 'tools/markdownlint')
        patch_file = self.root / 'tools/skill-patches/demo/reference/guide.md.patch'
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(
            '# threads: PRRT_stale\n'
            '--- a/reference/guide.md\n'
            '+++ b/reference/guide.md\n'
            '@@ -1,3 +1,3 @@\n'
            ' # Guide\n'
            ' \n'
            '-Old line that no longer exists.\n'
            '+Patched line.\n',
            encoding='utf-8',
        )
        rows, _ = self.invoke(apply=True, gate=adopt.markdown_gate)
        self.assertEqual(rows[0]['verdict'], 'refused', rows)
        self.assertIn('reference/guide.md', rows[0]['reason'])
        self.assertFalse((self.root / 'skills/demo').exists())

    def test_pinned_member_text_size_cap_boundary_and_unreadable(self):
        """Return truncated text at MAX_BYTES, refuse MAX_BYTES + 1 before git show, and ignore missing blobs."""
        prefix = '---\r\nname: demo\r\ndescription: Use when testing cap.\r\n---\r\n\r\n'
        prefix_bytes = prefix.encode('utf-8')
        at_cap = prefix_bytes + (b'a' * (adopt.MAX_BYTES - len(prefix_bytes)))
        over_cap = at_cap + b'z'
        self.assertEqual(len(at_cap), adopt.MAX_BYTES)
        self.assertEqual(len(over_cap), adopt.MAX_BYTES + 1)
        pin = self.source({'pack/cap/SKILL.md': at_cap, 'pack/over/SKILL.md': over_cap, 'LICENSE': LICENCE})
        checkout = self.checkouts / 'owner__repo'
        adopt.git(checkout, 'sparse-checkout', 'set', '--no-cone', '--stdin', data=b'**/LICENSE\n')
        self.assertFalse((checkout / 'pack/cap/SKILL.md').exists())
        self.assertFalse((checkout / 'pack/over/SKILL.md').exists())

        got = adopt._pinned_member_text(checkout, pin, 'pack/cap/SKILL.md')
        self.assertEqual(len(got), overlap.MAX_SKILL_CHARS)
        self.assertNotIn('\r', got)
        self.assertTrue(got.startswith('---\nname: demo\n'))

        calls = []
        real_git = adopt.git

        def spy_git(repo_dir, *args, **kwargs):
            """Record git subcommands invoked while reading pinned blobs."""
            calls.append(args)
            return real_git(repo_dir, *args, **kwargs)

        with patch.object(adopt, 'git', side_effect=spy_git):
            self.assertEqual(adopt._pinned_member_text(checkout, pin, 'pack/over/SKILL.md'), '')
            self.assertEqual(adopt._pinned_member_text(checkout, pin, 'pack/missing/SKILL.md'), '')
        self.assertTrue(any(args[:2] == ('cat-file', '-s') for args in calls))
        self.assertFalse(any(args and args[0] == 'show' for args in calls), calls)
        self.assertEqual(adopt._pinned_member_text(self.base / 'absent', pin, 'pack/cap/SKILL.md'), '')
        self.assertEqual(adopt._pinned_member_text(checkout, '', 'pack/cap/SKILL.md'), '')

    def test_adopted_member_fallback_confines_to_skills_dir(self):
        """Refuse fallback SKILL.md paths resolving outside skills_dir while keeping in-tree copies."""
        skills = self.root / 'skills'
        skills.mkdir()
        outside = self.root / 'skills-outside'
        outside.mkdir()
        (outside / 'SKILL.md').write_text(MD.format(name='demo'), encoding='utf-8')
        origin_text = 'upstream: <https://github.com/owner/repo>\npath: .\n'
        (outside / 'ORIGIN.md').write_text(origin_text, encoding='utf-8')

        (skills / 'demo').symlink_to(outside)
        self.assertEqual(adopt.get_member_skill_text('owner/repo:SKILL.md', self.checkouts, skills), '')
        (skills / 'demo').unlink()

        in_tree = skills / 'demo'
        in_tree.mkdir()
        (in_tree / 'ORIGIN.md').write_text(origin_text, encoding='utf-8')
        (in_tree / 'SKILL.md').symlink_to(outside / 'SKILL.md')
        self.assertEqual(adopt.get_member_skill_text('owner/repo:SKILL.md', self.checkouts, skills), '')

        (in_tree / 'SKILL.md').unlink()
        expected = MD.format(name='demo')
        (in_tree / 'SKILL.md').write_text(expected, encoding='utf-8')
        self.assertEqual(adopt.get_member_skill_text('owner/repo:SKILL.md', self.checkouts, skills), expected)

    def test_refill_command_includes_judge_model_placeholder_without_env(self):
        """Append --judge-model <model> when unset in env, and leave command unchanged when env sets it."""
        for name, source in (('alpha', 'anthropics/skills'), ('beta', 'obra/superpowers')):
            pin = self.source({'SKILL.md': MD.format(name=name), 'LICENSE': LICENCE}, source=source)
            self.candidate(name=name, folder='', source=source, pin=pin, blob_id=name)
        cand_path = self.base / 'candidates.tsv'
        tsv(cand_path, [*CANDIDATE_FIELDS, 'blob_id'], self.candidates)
        overlap_out = self.base / 'overlap.tsv'
        groups_out = self.base / 'groups.tsv'
        cache_path = self.base / 'orig-cache.json'
        overlap.run(
            candidates_tsv=cand_path, checkouts=self.checkouts, skills_dir=self.root / 'skills',
            overlap_out=overlap_out, groups_out=groups_out,
            embed_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            judge_fn=lambda _prompt: {'same_use': True, 'reason': 'same'},
            judge_model='cli-flag-judge', cache_path=cache_path,
            embed_model=EMBED_MODEL, provider=EMBED_PROVIDER,
        )
        partial_cache = self.embedding_cache({'alpha': [1.0, 0.0]})
        env_without = {k: v for k, v in os.environ.items() if k != 'PERITIA_LLM_JUDGE_MODEL'}
        stderr_unset = io.StringIO()
        with patch.dict(os.environ, env_without, clear=True), redirect_stderr(stderr_unset):
            adopt.run(cand_path, groups_out, self.base / 'census.tsv' if (self.base / 'census.tsv').exists() else self._write_census(),
                      self.checkouts, self.root, apply=False, gate=passing_gate, hiss_gate=passing_gate,
                      include_grouped=True, cache=partial_cache, embed_model=EMBED_MODEL, base_url=EMBED_PROVIDER)
        self.assertIn('--judge-model <model>', stderr_unset.getvalue())

        with patch.dict(os.environ, {**env_without, 'PERITIA_LLM_JUDGE_MODEL': 'env-judge'}, clear=True):
            inputs_with_env = adopt._copy_embedding_inputs(partial_cache, EMBED_MODEL, EMBED_PROVIDER, None, None, True)
            cmd_env = inputs_with_env.refill_command()
        self.assertNotIn('--judge-model', cmd_env)

    def _write_census(self):
        """Write current fixture census rows and return the TSV path."""
        census_path = self.base / 'census.tsv'
        tsv(census_path, ['source', 'path', 'pin'], self.census)
        return census_path

    def test_missing_vectors_stderr_mentions_working_tree_restore_hint(self):
        """Print the working-tree checkout hint and verify it restores only the named sparse path."""
        pins = {}
        for name, source in (('alpha', 'anthropics/skills'), ('beta', 'obra/superpowers')):
            pins[source] = self.source(
                {'SKILL.md': MD.format(name=name), 'LICENSE': LICENCE, 'OTHER.md': 'excluded\n'},
                source=source,
            )
            self.candidate(name=name, folder='', source=source, pin=pins[source], blob_id=name)
        members = [adopt.candidate_id(row) for row in self.candidates]
        self.groups = [{'group_id': '77', 'members': ','.join(members)}]
        cache_file = self.embedding_cache({'alpha': [1.0, 0.0]})
        expected_cmd = adopt._copy_embedding_inputs(
            cache_file, EMBED_MODEL, EMBED_PROVIDER, None, None, True,
            refill_paths=(self.base / 'candidates.tsv', self.checkouts, self.root / 'skills'),
        ).refill_command()
        hint_cmd = 'git -C <checkout> checkout --ignore-skip-worktree-bits <pin> -- <path>'

        stderr_present = io.StringIO()
        with redirect_stderr(stderr_present):
            self.invoke(apply=False, include_grouped=True, cache=cache_file)
        self.assertIn(f'run {expected_cmd}', stderr_present.getvalue())
        self.assertIn(hint_cmd, stderr_present.getvalue())
        self.assertIn('only the listed member path at the census pin', stderr_present.getvalue())

        beta_checkout = self.checkouts / 'obra__superpowers'
        beta_pin = pins['obra/superpowers']
        adopt.git(beta_checkout, 'sparse-checkout', 'set', '--no-cone', '--stdin', data=b'**/LICENSE\n')
        self.assertFalse((beta_checkout / 'SKILL.md').exists())
        self.assertFalse((beta_checkout / 'OTHER.md').exists())
        stderr_absent = io.StringIO()
        with redirect_stderr(stderr_absent):
            self.invoke(apply=False, include_grouped=True, cache=cache_file)
        printed = stderr_absent.getvalue()
        self.assertIn(f'run {expected_cmd}', printed)
        self.assertIn(hint_cmd, printed)

        with self.assertRaises(adopt.Refused):
            adopt.git(beta_checkout, 'checkout', beta_pin, '--', 'SKILL.md')
        self.assertFalse((beta_checkout / 'SKILL.md').exists())

        _, _, after_prefix = printed.partition('e.g. git -C <checkout> ')
        raw_args, _, _ = after_prefix.partition(', before rerunning')
        substitutions = {'<pin>': beta_pin, '<path>': 'SKILL.md'}
        adopt.git(beta_checkout, *(substitutions.get(tok, tok) for tok in raw_args.split()))
        self.assertTrue((beta_checkout / 'SKILL.md').is_file())
        self.assertFalse((beta_checkout / 'OTHER.md').exists())

    def test_origin_empty_path_not_present_while_dot_and_skill_md_are_present(self):
        """Ignore existing ORIGIN.md entries lacking a path while honoring explicit . and SKILL.md paths."""
        pin = self.source({'SKILL.md': MD.format(name='root-demo'), 'LICENSE': LICENCE}, source='owner/repo')
        self.candidate(name='root-demo', folder='', source='owner/repo', pin=pin)
        existing = self.root / 'skills/existing'
        existing.mkdir(parents=True)
        (existing / 'SKILL.md').write_text(MD.format(name='existing'), encoding='utf-8')
        (existing / 'LICENSE').write_text(LICENCE, encoding='utf-8')

        for bad_origin in (
            f'upstream: <https://github.com/owner/repo>\ncommit: {pin}\nlicence: MIT\nchanges: none\n',
            f'upstream: <https://github.com/owner/repo>\npath:   \ncommit: {pin}\nlicence: MIT\nchanges: none\n',
        ):
            (existing / 'ORIGIN.md').write_text(bad_origin, encoding='utf-8')
            self.assertEqual(adopt.read_present_skills(self.root / 'skills'), {})
            rows, _ = self.invoke(apply=False)
            self.assertEqual(rows[0]['verdict'], 'adopted')

        for valid_path in ('.', 'SKILL.md'):
            (existing / 'ORIGIN.md').write_text(
                f'upstream: <https://github.com/owner/repo>\npath: {valid_path}\ncommit: {pin}\n',
                encoding='utf-8',
            )
            rows, _ = self.invoke(apply=False)
            self.assertEqual(rows[0]['verdict'], 'present')
            self.assertEqual(rows[0]['directory'], 'skills/existing')

    def test_embedding_cache_distinct_vectors_and_conflict_check(self):
        """Keep distinct vectors for distinct copy descriptions, enforce 0.97 boundary, and reject conflicts."""
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob')
        copy_pin = self.source({'pack/demo/SKILL.md': COPY_MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob')
        orig_id, copy_id = adopt.candidate_id(orig_cand), adopt.candidate_id(copy_cand)
        self.groups = [{'group_id': '101', 'members': f'{orig_id},{copy_id}'}]
        cache_99 = self.embedding_cache({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]})
        items = [
            overlap.item_from_skill(orig_id, 'orig_blob', 'demo', MD.format(name='demo')),
            overlap.item_from_skill(copy_id, 'copy_blob', 'demo', COPY_MD.format(name='demo')),
        ]
        loaded = overlap.load_embeddings(cache_99, items, EMBED_MODEL, EMBED_PROVIDER, require_namespace=True)
        self.assertNotEqual(loaded[orig_id], loaded[copy_id])
        self.assertAlmostEqual(overlap.cosine_similarity(loaded[orig_id], loaded[copy_id]), 0.99, places=6)

        cache_99.unlink()
        cache_low = self.embedding_cache({'orig_blob': [1.0, 0.0], 'copy_blob': [0.96, math.sqrt(1 - 0.96**2)]})
        rows_low, _ = self.invoke(apply=False, include_grouped=True, cache=cache_low)
        self.assertEqual([r['verdict'] for r in rows_low], ['adopted', 'adopted'])

        self.candidates, self.census = [], []
        dup_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                              source='other/repo')
        self.candidates.extend([
            orig_cand,
            self.candidate(name='demo', folder='pack/demo', source='other/repo', pin=dup_pin, blob_id='dup_blob'),
        ])
        with self.assertRaisesRegex(ValueError, 'conflicting vectors for identical embed text'):
            self.embedding_cache({'orig_blob': [1.0, 0.0], 'dup_blob': [0.0, 1.0]})

    def _concurrent_apply_fixtures(self):
        """Prepare two independent single-skill apply inputs sharing self.root."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        pin1 = self.source({'pack/first/SKILL.md': MD.format(name='first'), 'pack/first/LICENSE': LICENCE},
                           source='owner/first')
        pin2 = self.source({'pack/second/SKILL.md': MD.format(name='second'), 'pack/second/LICENSE': LICENCE},
                           source='other/second')
        groups = self.base / 'concurrent-groups.tsv'
        tsv(groups, ['members'], [])
        paths = {}
        for idx, (name, repo, pin) in enumerate((('first', 'owner/first', pin1), ('second', 'other/second', pin2)), 1):
            cand_path = self.base / f'cand-{idx}.tsv'
            cen_path = self.base / f'cen-{idx}.tsv'
            tsv(cand_path, CANDIDATE_FIELDS, [{
                'source': repo, 'path': f'pack/{name}/SKILL.md', 'skill_name': name,
                'verdict': 'candidate', 'licence': 'MIT', 'licence_evidence': 'folder', 'functions': 'testing',
            }])
            tsv(cen_path, ['source', 'path', 'pin'], [{'source': repo, 'path': f'pack/{name}/SKILL.md', 'pin': pin}])
            paths[name] = (cand_path, cen_path)
        return groups, paths

    def test_concurrent_applies_serialize_under_output_lock(self):
        """Serialize overlapping --apply runs so the second reads root only after the first publishes."""
        groups, paths = self._concurrent_apply_fixtures()
        first_in_gate, release_first, second_ready = threading.Event(), threading.Event(), threading.Event()
        real_reconcile, real_eval = adopt._reconcile_publish_journal, adopt._evaluate_and_publish
        events, errors = [], []
        @contextmanager
        def tracked_lock(path):
            events.append(f'{threading.current_thread().name}:lock-attempt')
            if threading.current_thread().name == 'second':
                second_ready.set()
            with input_identity.output_lock(path):
                yield
        def tracked_reconcile(root, *, apply):
            events.append(f'{threading.current_thread().name}:root-read')
            return real_reconcile(root, apply=apply)
        def tracked_eval(root, checkouts, parsed, opts):
            rows = real_eval(root, checkouts, parsed, opts)
            events.append(f'{threading.current_thread().name}:published')
            return rows
        def worker(name, gate_fn):
            try:
                adopt.run(paths[name][0], groups, paths[name][1], self.checkouts, self.root,
                          apply=True, gate=gate_fn, hiss_gate=passing_gate)
            except (OSError, ValueError, RuntimeError, AssertionError) as exc:
                errors.append(exc)
            finally:
                if name == 'second':
                    second_ready.set()
        first = threading.Thread(target=worker, name='first', args=(
            'first', lambda *_: (first_in_gate.set(), self.assertTrue(release_first.wait(timeout=5.0)))))
        second = threading.Thread(target=worker, args=('second', passing_gate), name='second')
        with (patch.object(adopt, 'output_lock', side_effect=tracked_lock),
              patch.object(adopt, '_reconcile_publish_journal', side_effect=tracked_reconcile),
              patch.object(adopt, '_evaluate_and_publish', side_effect=tracked_eval)):
            try:
                first.start()
                self.assertTrue(first_in_gate.wait(timeout=5.0))
                second.start()
                self.assertTrue(second_ready.wait(timeout=5.0))
                self.assertIn('second:lock-attempt', events)
                self.assertNotIn('second:root-read', events)
            finally:
                release_first.set()
                for thread in (first, second):
                    if thread.ident is not None:
                        thread.join(timeout=5.0)
        self.assertEqual((first.is_alive(), second.is_alive(), errors), (False, False, []))
        self.assertEqual(events, ['first:lock-attempt', 'first:root-read', 'second:lock-attempt',
                                  'first:published', 'second:root-read', 'second:published'])
        self.assertEqual(((self.root / '.adopt-skills.lock').is_file(),
                          (self.root / adopt.JOURNAL_NAME).exists(), check_skills.check(self.root)), (True, False, 0))
        for name in ('first', 'second'):
            self.assertTrue((self.root / 'skills' / name).is_dir())
            self.assertIn(f'skills/{name}/**', (self.root / 'REUSE.toml').read_text(encoding='utf-8'))
            self.assertIn(f'`{name}`', (self.root / 'CREDITS.md').read_text(encoding='utf-8'))
            self.assertIn(f'{name}\t', (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8'))

    def test_apply_lock_timeout_preserves_interrupted_journal(self):
        """Leave an interrupted publish journal and its staged backups untouched when --apply times out on the lock."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)
        journal_file = self.root / adopt.JOURNAL_NAME
        self.assertTrue(journal_file.is_file())
        staged = [p for p in self.root.iterdir() if p.is_dir() and p.name.startswith('.adopt-publish-')]
        self.assertEqual(len(staged), 1)
        self.assertTrue((staged[0] / 'backup/0-REUSE.toml').is_file())

        with (input_identity.output_lock(self.root / '.adopt-skills'),
              patch.object(input_identity, 'LOCK_WAIT_SECONDS', 0.05)):
            before = snapshot(self.root)
            with self.assertRaisesRegex(TimeoutError, r'\.adopt-skills\.lock.*timed out'):
                self.invoke(apply=True, runner=fake_runner)
            self.assertEqual(snapshot(self.root), before)
            self.assertTrue(journal_file.is_file())
            self.assertTrue((staged[0] / 'backup/0-REUSE.toml').is_file())

    def test_preview_completes_while_apply_lock_held(self):
        """Complete preview without waiting on a held apply lock when no publish journal exists."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        self.fixture()
        self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
        with (input_identity.output_lock(self.root / '.adopt-skills'),
              patch.object(input_identity, 'LOCK_WAIT_SECONDS', 0.05)):
            before = snapshot(self.root)
            rows, _ = self.invoke(apply=False)
            self.assertEqual([r['verdict'] for r in rows], ['adopted'])
            self.assertEqual(snapshot(self.root), before)

    def test_skill_directories_filters_dirs_before_max_skills_slice(self):
        """Keep all MAX_SKILLS directories in INDEX.tsv when skills/INDEX.tsv sits beside them."""
        (self.root / 'LICENSES').mkdir()
        (self.root / 'LICENSES/MIT.txt').write_text(LICENCE, encoding='utf-8')
        pin = self.source({
            'pack/mu/SKILL.md': MD.format(name='mu'), 'pack/mu/LICENSE': LICENCE,
            'pack/nu/SKILL.md': MD.format(name='nu'), 'pack/nu/LICENSE': LICENCE,
        })
        for name in ('mu', 'nu'):
            self.candidate(name=name, folder=f'pack/{name}', pin=pin)
        self.invoke(apply=True)
        self.assertEqual(check_skills.check(self.root), 0)
        self.candidates, self.census = [], []
        with patch.object(check_skills, 'MAX_SKILLS', 2):
            self.invoke(apply=True)
            index_text = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8')
            self.assertIn('mu\towner/repo\t', index_text)
            self.assertIn('nu\towner/repo\t', index_text)
            self.assertEqual(check_skills.check(self.root), 0)

    def test_restore_backup_file_stages_temp_under_backup_parent(self):
        """Create restore temporaries inside backup.parent so target.parent never holds *.restore.* files."""
        backup_dir = self.root / '.adopt-publish-stage' / 'backup'
        backup_dir.mkdir(parents=True)
        backup = backup_dir / '0-REUSE.toml'
        backup.write_text('version = 1\n', encoding='utf-8')
        target = self.root / 'REUSE.toml'
        target.write_text('modified\n', encoding='utf-8')
        recorded = {}

        def fail_replace(src, dst):
            """Capture temp file placement right before replacement and fail."""
            recorded['src'] = Path(src)
            recorded['dst'] = Path(dst)
            recorded['backup_tmp'] = list(backup.parent.glob('*.restore.*'))
            recorded['target_tmp'] = list(target.parent.glob('*.restore.*'))
            raise OSError('simulated restore replace failure')

        with (patch.object(adopt, 'os', _OsReplaceProxy(fail_replace)),
              self.assertRaisesRegex(OSError, 'simulated restore replace failure')):
            adopt._restore_backup_file(backup, target)
        self.assertEqual(recorded['dst'], target)
        self.assertEqual(recorded['src'].parent, backup.parent)
        self.assertEqual(recorded['backup_tmp'], [recorded['src']])
        self.assertEqual(recorded['target_tmp'], [])
        self.assertEqual(list(target.parent.glob('*.restore.*')), [])
        self.assertEqual(list(backup.parent.glob('*.restore.*')), [])
        self.assertEqual(target.read_text(encoding='utf-8'), 'modified\n')
        adopt._restore_backup_file(backup, target)
        self.assertEqual(target.read_text(encoding='utf-8'), 'version = 1\n')

    def test_journal_swapped_backup_paths_refused_without_modifying_root(self):
        """Refuse a journal whose backup slots were swapped before executing any recovery step."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)
        journal_file = self.root / adopt.JOURNAL_NAME
        payload = json.loads(journal_file.read_text(encoding='utf-8'))
        payload['backups']['REUSE.toml']['backup'], payload['backups']['CREDITS.md']['backup'] = (
            payload['backups']['CREDITS.md']['backup'], payload['backups']['REUSE.toml']['backup'],
        )
        journal_file.write_text(json.dumps(payload), encoding='utf-8')
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, r'invalid backup path'):
            self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(snapshot(self.root), before)

    def test_reconcile_continues_after_restore_failure_and_names_backup(self):
        """Run remaining recovery steps when REUSE.toml restore fails, keep journal/staging, then recover."""
        self._seed_existing_skill()
        pre_credits = (self.root / 'CREDITS.md').read_text(encoding='utf-8')
        before_seed = snapshot(self.root)
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)
        self.assertIn('`second`', (self.root / 'CREDITS.md').read_text(encoding='utf-8'))
        staging = next(p for p in self.root.iterdir() if p.is_dir() and p.name.startswith('.adopt-publish-'))
        backup_reuse = staging / 'backup/0-REUSE.toml'
        real_restore = adopt._restore_backup_file

        def fail_reuse_only(backup_path, target_path):
            """Fail restoring REUSE.toml while letting CREDITS.md and INDEX.tsv succeed."""
            if target_path == self.root / 'REUSE.toml':
                raise OSError('simulated reuse reconcile failure')
            return real_restore(backup_path, target_path)

        with (patch.object(adopt, '_restore_backup_file', side_effect=fail_reuse_only),
              self.assertRaises(adopt._PublishRollbackError) as ctx):
            self.invoke(apply=True, runner=fake_runner)
        self.assertIn('REUSE.toml', str(ctx.exception))
        self.assertIn(str(backup_reuse), str(ctx.exception))
        self.assertNotIn('.restore.', str(ctx.exception))
        self.assertEqual((self.root / 'CREDITS.md').read_text(encoding='utf-8'), pre_credits)
        self.assertTrue((self.root / adopt.JOURNAL_NAME).is_file())
        self.assertTrue(staging.is_dir() and backup_reuse.is_file())

        tsv(self.base / 'cand-empty.tsv', CANDIDATE_FIELDS, [])
        adopt.run(self.base / 'cand-empty.tsv', self.base / 'groups.tsv', self.base / 'census.tsv',
                  self.checkouts, self.root, apply=True, gate=passing_gate, hiss_gate=passing_gate,
                  runner=fake_runner)
        self.assertEqual(snapshot(self.root), before_seed)

    def _assert_interrupt_at_replace_rolls_back(self, target_dest: Path, *, after_replace: bool) -> None:
        """Raise KeyboardInterrupt right after or before replacing target_dest and assert clean rollback."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        before = snapshot(self.root)
        real_replace = os.replace
        fired = False

        def interrupt_replace(src, dst):
            """Trigger one KeyboardInterrupt at target_dest and delegate rollback calls."""
            nonlocal fired
            if not fired and Path(dst) == target_dest:
                fired = True
                if after_replace:
                    real_replace(src, dst)
                raise KeyboardInterrupt('simulated interrupt')
            return real_replace(src, dst)

        with (patch.object(adopt, 'os', _OsReplaceProxy(interrupt_replace)),
              self.assertRaises(KeyboardInterrupt)):
            self.invoke(apply=True, runner=fake_runner)
        self.assertTrue(fired)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish-')], [])

    def test_commit_interrupt_after_skill_replace_rolls_back_cleanly(self):
        """Roll back a moved skill directory when KeyboardInterrupt arrives right after os.replace."""
        self._assert_interrupt_at_replace_rolls_back(self.root / 'skills/first', after_replace=True)

    def test_commit_interrupt_before_skill_replace_rolls_back_cleanly(self):
        """Roll back cleanly when KeyboardInterrupt arrives after undo recording but before os.replace."""
        self._assert_interrupt_at_replace_rolls_back(self.root / 'skills/first', after_replace=False)

    def test_commit_interrupt_after_licence_replace_rolls_back_cleanly(self):
        """Unlink a moved licence file when KeyboardInterrupt arrives right after os.replace."""
        self._assert_interrupt_at_replace_rolls_back(self.root / 'LICENSES/MIT-0.txt', after_replace=True)

    def test_licence_destination_symlink_refused_before_journal(self):
        """Refuse dangling and live LICENSES/<id>.txt symlinks before writing a publish journal."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        lic_link = self.root / 'LICENSES/MIT-0.txt'
        live_target = self.base / 'outside-mit0.txt'
        live_target.write_text('Outside MIT-0\n', encoding='utf-8')
        for target in (self.base / 'dangling-mit0.txt', live_target):
            if os.path.lexists(lic_link):
                lic_link.unlink()
            lic_link.symlink_to(target)
            before = snapshot(self.root)
            with self.assertRaisesRegex(ValueError, r'LICENSES/MIT-0\.txt: output symlink refused'):
                self.invoke(apply=True, runner=fake_runner)
            self.assertTrue(os.path.lexists(lic_link) and lic_link.is_symlink())
            self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
            self.assertEqual(snapshot(self.root), before)

    def test_commit_journal_unlink_failure_rolls_back_and_keeps_staging(self):
        """Roll back root and preserve staging backups if unlinking the publish journal fails."""
        self._seed_existing_skill()
        before = snapshot(self.root)
        fake_runner, _ = self._two_mit0_candidates()
        journal_path = self.root / adopt.JOURNAL_NAME
        real_unlink = Path.unlink

        def fail_journal_unlink(path_self, missing_ok=False):
            """Raise OSError only when unlinking the publish journal."""
            if path_self == journal_path:
                raise OSError('simulated journal unlink failure')
            return real_unlink(path_self, missing_ok=missing_ok)

        with (patch.object(Path, 'unlink', fail_journal_unlink),
              self.assertRaises(adopt._PublishRollbackError) as ctx):
            self.invoke(apply=True, runner=fake_runner)
        self.assertIn(str(journal_path), str(ctx.exception))
        self.assertTrue(journal_path.is_file())
        staged_dirs = [p for p in self.root.iterdir() if p.is_dir() and p.name.startswith('.adopt-publish-')]
        self.assertEqual(len(staged_dirs), 1)
        staging = staged_dirs[0]
        self.assertIn(str(staging), str(ctx.exception))
        for slot in ('0-REUSE.toml', '1-CREDITS.md', '2-INDEX.tsv'):
            self.assertTrue((staging / 'backup' / slot).is_file(), slot)
        filtered = {
            rel: entry for rel, entry in snapshot(self.root).items()
            if rel != adopt.JOURNAL_NAME and rel != staging.name and not rel.startswith(f'{staging.name}/')
        }
        self.assertEqual(filtered, before)

        tsv(self.base / 'cand-empty.tsv', CANDIDATE_FIELDS, [])
        adopt.run(self.base / 'cand-empty.tsv', self.base / 'groups.tsv', self.base / 'census.tsv',
                  self.checkouts, self.root, apply=True, gate=passing_gate, hiss_gate=passing_gate,
                  runner=fake_runner)
        self.assertEqual(snapshot(self.root), before)

        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual([r['verdict'] for r in rows], ['adopted', 'adopted'])
        self.assertEqual(check_skills.check(self.root), 0)

    def test_reconcile_preserves_operator_skill_when_rename_did_not_run(self):
        """Keep operator-created skills/second when its staged rename never ran, while undoing completed moves."""
        self._seed_existing_skill()
        pre_reuse = (self.root / 'REUSE.toml').read_bytes()
        pre_credits = (self.root / 'CREDITS.md').read_bytes()
        pre_index = (self.root / 'skills/INDEX.tsv').read_bytes()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)

        self.assertTrue((self.root / 'skills/first').is_dir())
        self.assertFalse((self.root / 'skills/second').exists())
        operator_second = self.root / 'skills/second'
        operator_second.mkdir()
        marker = operator_second / 'operator-marker.txt'
        marker.write_text('handcrafted by operator\n', encoding='utf-8')

        adopt._reconcile_publish_journal(self.root, apply=True)

        self.assertTrue(operator_second.is_dir())
        self.assertEqual(marker.read_text(encoding='utf-8'), 'handcrafted by operator\n')
        self.assertFalse((self.root / 'skills/first').exists())
        self.assertFalse((self.root / 'LICENSES/MIT-0.txt').exists())
        self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])
        self.assertEqual((self.root / 'REUSE.toml').read_bytes(), pre_reuse)
        self.assertEqual((self.root / 'CREDITS.md').read_bytes(), pre_credits)
        self.assertEqual((self.root / 'skills/INDEX.tsv').read_bytes(), pre_index)

    def test_reconcile_preserves_operator_licence_when_rename_did_not_run(self):
        """Keep operator-created LICENSES/MIT-0.txt when hard-killed before its staged rename ran."""
        self._seed_existing_skill()
        pin = self.source({
            'pack/first/SKILL.md': MD.format(name='first'), 'pack/first/LICENSE': 'Apache-2.0\n',
            'pack/second/SKILL.md': MD.format(name='second'), 'pack/second/LICENSE': 'MIT-0\n',
        })
        for name, lic in (('first', 'Apache-2.0'), ('second', 'MIT-0')):
            self.candidates.append({
                'source': 'owner/repo', 'path': f'pack/{name}/SKILL.md',
                'skill_name': name, 'verdict': 'candidate', 'licence': lic,
                'licence_evidence': 'folder', 'functions': 'testing',
            })
            self.census.append({'source': 'owner/repo', 'path': f'pack/{name}/SKILL.md', 'pin': pin})

        def multi_runner(cmd, *, cwd=None):
            """Write deterministic staged licence files for Apache-2.0 and MIT-0."""
            self.assertIsNotNone(cwd)
            Path(cmd[cmd.index('-o') + 1]).write_text(f'Downloaded {cmd[-1]}\n', encoding='utf-8')
            return b'ok'

        self._fork_kill_before_replace(multi_runner, self.root / 'LICENSES/MIT-0.txt', exit_code=96)
        self.assertTrue((self.root / 'LICENSES/Apache-2.0.txt').is_file())
        self.assertFalse((self.root / 'LICENSES/MIT-0.txt').exists())
        operator_lic = self.root / 'LICENSES/MIT-0.txt'
        operator_lic.write_text('Operator custom MIT-0\n', encoding='utf-8')

        adopt._reconcile_publish_journal(self.root, apply=True)

        self.assertEqual(operator_lic.read_text(encoding='utf-8'), 'Operator custom MIT-0\n')
        self.assertFalse((self.root / 'LICENSES/Apache-2.0.txt').exists())
        self.assertTrue((self.root / 'LICENSES/MIT.txt').is_file())
        self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

    def test_reconcile_preserves_edited_reuse_when_killed_before_metadata_replace(self):
        """Keep operator edits to REUSE.toml when hard-killed before the REUSE.toml replacement ran."""
        self._seed_existing_skill()
        pre_credits = (self.root / 'CREDITS.md').read_bytes()
        pre_index = (self.root / 'skills/INDEX.tsv').read_bytes()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_before_replace(fake_runner, self.root / 'REUSE.toml', exit_code=97)

        self.assertTrue((self.root / adopt.JOURNAL_NAME).is_file())
        self.assertTrue((self.root / 'LICENSES/MIT-0.txt').is_file())
        edited_reuse = (self.root / 'REUSE.toml').read_text(encoding='utf-8') + '# operator comment\n'
        (self.root / 'REUSE.toml').write_text(edited_reuse, encoding='utf-8')

        adopt._reconcile_publish_journal(self.root, apply=True)

        self.assertEqual((self.root / 'REUSE.toml').read_bytes(), edited_reuse.encode('utf-8'))
        self.assertEqual((self.root / 'CREDITS.md').read_bytes(), pre_credits)
        self.assertEqual((self.root / 'skills/INDEX.tsv').read_bytes(), pre_index)
        self.assertFalse((self.root / 'LICENSES/MIT-0.txt').exists())
        self.assertFalse((self.root / adopt.JOURNAL_NAME).exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

    def test_reconcile_preserves_operator_index_when_index_not_staged(self):
        """Leave operator-created skills/INDEX.tsv untouched when publish staged no index."""
        pre_reuse = (self.root / 'REUSE.toml').read_bytes()
        self.assertFalse((self.root / 'skills').exists())
        self._fork_kill_before_replace(None, self.root / 'CREDITS.md', exit_code=97)

        journal_file = self.root / adopt.JOURNAL_NAME
        payload = json.loads(journal_file.read_text(encoding='utf-8'))
        self.assertEqual(payload['backups']['skills/INDEX.tsv'], {'backup': None, 'source': None})
        edited_credits = (self.root / 'CREDITS.md').read_text(encoding='utf-8') + '<!-- operator note -->\n'
        (self.root / 'CREDITS.md').write_text(edited_credits, encoding='utf-8')
        (self.root / 'skills').mkdir()
        index_file = self.root / 'skills/INDEX.tsv'
        index_file.write_text('skill\tsource\tfunctions\tdomains\toverlap_group\n', encoding='utf-8')

        adopt._reconcile_publish_journal(self.root, apply=True)

        self.assertEqual(index_file.read_text(encoding='utf-8'), 'skill\tsource\tfunctions\tdomains\toverlap_group\n')
        self.assertEqual((self.root / 'CREDITS.md').read_text(encoding='utf-8'), edited_credits)
        self.assertEqual((self.root / 'REUSE.toml').read_bytes(), pre_reuse)
        self.assertFalse(journal_file.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.adopt-publish')], [])

    def test_journal_invalid_or_null_backup_source_refused_without_modifying_root(self):
        """Refuse journals with mismatched or null metadata backup sources without modifying root."""
        self._seed_existing_skill()
        fake_runner, _ = self._two_mit0_candidates()
        self._fork_kill_on_second_skill(fake_runner)
        journal_file = self.root / adopt.JOURNAL_NAME
        valid = json.loads(journal_file.read_text(encoding='utf-8'))
        staging = valid['staging']
        bad_backups = [
            {**valid['backups'], 'REUSE.toml': {**valid['backups']['REUSE.toml'], 'source': f'{staging}/CREDITS.md'}},
            {**valid['backups'], 'skills/INDEX.tsv': {
                **valid['backups']['skills/INDEX.tsv'], 'source': f'{staging}/skills/INDEX.tsv',
            }},
            {**valid['backups'], 'REUSE.toml': {**valid['backups']['REUSE.toml'], 'source': 'REUSE.toml'}},
            {**valid['backups'], 'REUSE.toml': {**valid['backups']['REUSE.toml'], 'source': None}},
            {**valid['backups'], 'CREDITS.md': {**valid['backups']['CREDITS.md'], 'source': None}},
        ]
        for backups_entry in bad_backups[:8]:
            journal_file.write_text(json.dumps({**valid, 'backups': backups_entry}), encoding='utf-8')
            before = snapshot(self.root)
            with self.assertRaisesRegex(ValueError, r'invalid backup source'):
                self.invoke(apply=True, runner=fake_runner)
            self.assertEqual(snapshot(self.root), before)


class RewriteFencesTests(unittest.TestCase):
    def test_top_level_four_space_backticks_unchanged(self):
        text = "    ```\n    code\n    ```\n"
        new_text, changed = adopt.rewrite_fences(text)
        self.assertFalse(changed)
        self.assertEqual(new_text, text)

    def test_nested_list_four_space_fence_rewritten(self):
        text = "* item\n    ```\n    code\n    ```\n"
        new_text, changed = adopt.rewrite_fences(text)
        self.assertTrue(changed)
        self.assertEqual(new_text, "* item\n    ```text\n    code\n    ```\n")

    def test_tilde_fence_rewritten(self):
        text = "~~~\ncode\n~~~\n"
        new_text, changed = adopt.rewrite_fences(text)
        self.assertTrue(changed)
        self.assertEqual(new_text, "~~~text\ncode\n~~~\n")

    def test_unclosed_fence_rewritten(self):
        text = "```\nunclosed code\n"
        new_text, changed = adopt.rewrite_fences(text)
        self.assertTrue(changed)
        self.assertEqual(new_text, "```text\nunclosed code\n")

    def test_fence_with_info_unchanged(self):
        text = "```python\nprint('hello')\n```\n"
        new_text, changed = adopt.rewrite_fences(text)
        self.assertFalse(changed)
        self.assertEqual(new_text, text)


if __name__ == '__main__':
    unittest.main()
