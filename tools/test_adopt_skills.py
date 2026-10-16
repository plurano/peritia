#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Temporary sparse repositories and a fake GitHub reader; no live adoption."""
import base64
import csv
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import adopt_skills as adopt
import census
import check_skills

REPO = Path(__file__).resolve().parent.parent
MD = '---\nname: {name}\ndescription: Use when testing adoption.\n---\n\n# Example\n\nCheck the result.\n'
LICENCE = 'MIT License\n\nCopyright (c) 2025 owner\n'
CANDIDATE_FIELDS = ['source', 'path', 'skill_name', 'verdict', 'licence', 'licence_evidence', 'functions']


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


class AdoptionTests(unittest.TestCase):
    def setUp(self):
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
        with patch.dict('os.environ', {'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.org',
                                      'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.org',
                                      'GIT_AUTHOR_DATE': '2025-01-01T00:00:00Z',
                                      'GIT_COMMITTER_DATE': '2025-01-01T00:00:00Z'}):
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
               report=None, runner=None, include_grouped=False, cache=None, sources=None):
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
                         include_grouped=include_grouped, cache=cache, sources=sources)

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
        self.fixture()
        self.invoke(apply=False)
        argv = [str(REPO / 'tools/adopt_skills.py'), '--candidates', str(self.base / 'candidates.tsv'),
                '--groups', str(self.base / 'groups.tsv'), '--census', str(self.base / 'census.tsv'),
                '--checkouts', str(self.checkouts), '--root', str(self.root)]
        before = snapshot(self.root)
        # The absent markdown gate refuses this fixture, but still produces a report without writes.
        output = adopt.command(['python3', '-B', *argv]).decode()
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
        files = {'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': 'MIT-0\n'}
        pin = self.source(files)
        row = dict(source='owner/repo', path='pack/demo/SKILL.md', skill_name='demo',
                   verdict='candidate', licence='MIT-0', licence_evidence='folder', functions='testing')
        self.candidates.append(row)
        self.census.append({'source': 'owner/repo', 'path': 'pack/demo/SKILL.md', 'pin': pin})

        commands = []
        def fake_runner(cmd, *, cwd=None):
            commands.append((list(cmd), cwd))
            (cwd / 'LICENSES' / f'{cmd[-1]}.txt').write_text('Downloaded MIT-0')
            return b'Downloaded'

        rows, _ = self.invoke(apply=True, runner=fake_runner)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertEqual(commands, [(['reuse', 'download', 'MIT-0'], self.root)])
        self.assertTrue((self.root / 'LICENSES/MIT-0.txt').is_file())

    def test_failed_licence_download_fails_run(self):
        files = {'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': 'MIT-0\n'}
        pin = self.source(files)
        row = dict(source='owner/repo', path='pack/demo/SKILL.md', skill_name='demo',
                   verdict='candidate', licence='MIT-0', licence_evidence='folder', functions='testing')
        self.candidates.append(row)
        self.census.append({'source': 'owner/repo', 'path': 'pack/demo/SKILL.md', 'pin': pin})

        def failing_runner(cmd, *, cwd=None):
            raise subprocess.CalledProcessError(1, cmd, stderr=b'network error')

        with self.assertRaises(ValueError) as ctx:
            self.invoke(apply=True, runner=failing_runner)
        self.assertIn('MIT-0', str(ctx.exception))

    def test_report_path_written_and_root_refused(self):
        self.fixture()
        # Default: no report at root
        rows, _ = self.invoke()
        self.assertFalse((self.root / 'adopt-report.tsv').exists())

        # Specific report path
        out_report = self.base / 'custom' / 'report.tsv'
        rows, _ = self.invoke(report=out_report)
        self.assertTrue(out_report.is_file())
        self.assertEqual(out_report.read_text(encoding='utf-8'), adopt.report_text(rows))
        self.assertFalse((self.root / 'adopt-report.tsv').exists())

        # Root refused as report location
        with self.assertRaises(ValueError) as ctx:
            self.invoke(report=self.root)
        self.assertIn('repository root refused', str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            self.invoke(report=self.root / 'adopt-report.tsv')
        self.assertIn('repository root refused', str(ctx.exception))

    def test_group_adopts_original_and_refuses_composio_copy(self):
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob', domains='coding;testing')
        copy_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob', domains='testing')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.base / 'cache.json'
        cache_file.write_text(json.dumps({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]}))
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

    def test_group_different_skills_adopts_both_with_group_id(self):
        pin_a = self.source({'pack/alpha/SKILL.md': MD.format(name='alpha'), 'pack/alpha/LICENSE': LICENCE},
                            source='anthropics/skills')
        cand_a = self.candidate(name='alpha', folder='pack/alpha', source='anthropics/skills', pin=pin_a,
                                blob_id='alpha_blob', domains='science')
        pin_b = self.source({'pack/beta/SKILL.md': MD.format(name='beta'), 'pack/beta/LICENSE': LICENCE},
                            source='obra/superpowers')
        cand_b = self.candidate(name='beta', folder='pack/beta', source='obra/superpowers', pin=pin_b,
                                blob_id='beta_blob', domains='dev')
        self.groups = [{
            'group_id': '42',
            'members': f'{cand_a["source"]}:{cand_a["path"]},{cand_b["source"]}:{cand_b["path"]}',
        }]
        cache_file = self.base / 'cache.json'
        cache_file.write_text(json.dumps({'alpha_blob': [1.0, 0.0], 'beta_blob': [0.0, 1.0]}))
        rows, _ = self.invoke(apply=True, include_grouped=True, cache=cache_file)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['verdict'], 'adopted')
        self.assertEqual(rows[1]['verdict'], 'adopted')
        self.assertTrue((self.root / 'skills/alpha').is_dir())
        self.assertTrue((self.root / 'skills/beta').is_dir())
        index_text = (self.root / 'skills/INDEX.tsv').read_text(encoding='utf-8')
        lines = index_text.splitlines()
        row_alpha = [l for l in lines[1:] if l.startswith('alpha\t')][0]
        row_beta = [l for l in lines[1:] if l.startswith('beta\t')][0]
        self.assertEqual(row_alpha.split('\t')[4], '42')
        self.assertEqual(row_beta.split('\t')[4], '42')
        self.assertEqual(check_skills.check(self.root), 0)

    def test_original_refused_by_gate_leaves_copy_refused(self):
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob')
        copy_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                               source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.base / 'cache.json'
        cache_file.write_text(json.dumps({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]}))
        def gate_reject_demo(root, staging, skill):
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
        orig_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                                source='anthropics/skills')
        orig_cand = self.candidate(name='demo', folder='pack/demo', source='anthropics/skills', pin=orig_pin,
                                   blob_id='orig_blob', domains='coding;testing')
        copy_pin = self.source({'pack/demo/SKILL.md': MD.format(name='demo'), 'pack/demo/LICENSE': LICENCE},
                                source='ComposioHQ/awesome-claude-skills')
        copy_cand = self.candidate(name='demo', folder='pack/demo', source='ComposioHQ/awesome-claude-skills', pin=copy_pin,
                                   blob_id='copy_blob', domains='testing')
        self.groups = [{
            'group_id': '101',
            'members': f'{orig_cand["source"]}:{orig_cand["path"]},{copy_cand["source"]}:{copy_cand["path"]}',
        }]
        cache_file = self.base / 'cache.json'
        cache_file.write_text(json.dumps({'orig_blob': [1.0, 0.0], 'copy_blob': [0.99, math.sqrt(1 - 0.99**2)]}))

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


if __name__ == '__main__':
    unittest.main()
