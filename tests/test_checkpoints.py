# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Real bare-Git round trips, privacy, retention, filters and merge conflicts."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoints as cp
import dev_agent

SESSION, PROJECT, WORKSPACE = 'a' * 32, 'b' * 32, 'c' * 32


def archive(root, content):
    path = root / 'base.tar'
    with tarfile.open(path, 'w') as tar:
        for name, data in content.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    checksum = cp.digest(path)
    target = root / 'archives' / checksum
    target.parent.mkdir(exist_ok=True)
    path.replace(target)
    return checksum


class LocalStorage(cp.GitCheckpoint):
    def verify_private(self):
        if self.api('')['private'] is not True:
            raise PermissionError('Public storage')


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bare = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.bare)], check=True)
        self.storage = LocalStorage('owner/private', 'synthetic-token')
        self.storage.url = self.bare.as_uri()
        self.storage.api = lambda path: {'private': True}
        self.state = self.root / 'state'
        self.state.mkdir()
        base = archive(self.state, {'source.py': b'original'})
        workspace = self.state / 'workspaces' / WORKSPACE
        workspace.mkdir(parents=True)
        (workspace / 'source.py').write_bytes(b'remote edit')
        (workspace / 'result.txt').write_bytes(b'result')
        (self.state / 'projects.json').write_text(json.dumps({PROJECT: {'workspace': WORKSPACE, 'base': base}}))
        self.workspace = workspace

    def tearDown(self):
        # Git uses read-only object files on Windows.
        def unlock(function, name, _):
            Path(name).chmod(0o700)
            function(name)
        import shutil
        shutil.rmtree(self.root, onerror=unlock)
        self.temp.cleanup()

    def test_real_git_roundtrip_three_roots_only_two_references(self):
        commits = []
        for n in range(3):
            (self.workspace / 'result.txt').write_text(str(n))
            commits.append(self.storage.save(self.state, SESSION)['commit'])
        refs = self.storage.refs(self.root)
        self.assertEqual(set(refs.values()), set(commits[-2:]))
        for oid in refs.values():
            result = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-list', '--parents', oid], capture_output=True, check=True)
            self.assertEqual(result.stdout.decode().strip(), oid)
        restored = self.root / 'restored'
        self.assertTrue(self.storage.load(restored, SESSION)['restored'])
        self.assertEqual((restored / 'workspaces' / WORKSPACE / 'result.txt').read_text(), '2')
        self.assertEqual((restored / 'workspaces' / WORKSPACE / 'source.py').read_bytes(), b'remote edit')
        self.assertFalse((restored / 'state.sqlite').exists())
        self.assertFalse(self.storage.load(self.root / 'other', 'd' * 32)['restored'])

    def test_public_repository_cannot_save_restore_or_prune(self):
        self.storage.api = lambda path: {'private': False}
        for method, args in [(self.storage.save, (self.state, SESSION)), (self.storage.load, (self.root / 'restored', SESSION)), (self.storage.prune, (SESSION,))]:
            with self.assertRaises(PermissionError):
                method(*args)
        self.assertEqual(self.storage.refs(self.root), {})

    def test_excludes_credentials_logs_dependencies_and_caps_before_push(self):
        for name in ('.env', 'auth.json', 'id_ed25519', 'trace.log'):
            (self.workspace / name).write_text('synthetic-private-data')
        (self.workspace / '.venv').mkdir()
        (self.workspace / '.venv' / 'package').write_text('dependency')
        self.storage.save(self.state, SESSION)
        self.storage.load(self.root / 'restored', SESSION)
        self.assertEqual(set(cp.files(self.root / 'restored/workspaces' / WORKSPACE)), {'source.py', 'result.txt'})
        old = self.storage.refs(self.root)
        with patch.object(cp, 'LIMIT', 10), self.assertRaises(ValueError):
            self.storage.save(self.state, SESSION)
        self.assertEqual(old, self.storage.refs(self.root))

    def test_remote_changes_survive_local_updates_and_conflicts_are_explicit(self):
        base = archive(self.state, {'source.py': b'original', 'new.txt': b'local addition'})
        destination = self.state / 'workspaces' / ('d' * 32)
        cp.extract(self.state / 'archives' / base, destination)
        cp.reconcile(self.state, PROJECT, 'd' * 32, base)
        self.assertEqual((destination / 'source.py').read_bytes(), b'remote edit')
        self.assertEqual((destination / 'new.txt').read_bytes(), b'local addition')
        self.assertEqual((destination / 'result.txt').read_bytes(), b'result')
        conflict = archive(self.state, {'source.py': b'conflicting local change'})
        next_destination = self.state / 'workspaces' / ('e' * 32)
        cp.extract(self.state / 'archives' / conflict, next_destination)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            cp.reconcile(self.state, PROJECT, 'e' * 32, conflict)
        self.assertEqual(cp.projects(self.state)[PROJECT]['workspace'], 'd' * 32)

    def test_unsafe_archive_and_wrong_repository_are_rejected(self):
        path = self.root / 'evil.tar'
        with tarfile.open(path, 'w') as tar:
            entry = tarfile.TarInfo('../escape')
            tar.addfile(entry, io.BytesIO(b''))
        with self.assertRaises(ValueError):
            cp.extract(path, self.root / 'out')
        real = cp.GitCheckpoint('owner/private', 'synthetic-token')
        real.api = lambda path: {'private': True, 'full_name': 'other/repo'}
        with self.assertRaises(PermissionError):
            real.verify_private()

    def test_profiles_are_disjoint_and_timeouts_verified(self):
        with patch.dict('os.environ', {'CODEX_THREAD_ID': 'same-owner'}), patch.object(dev_agent, 'CONFIG_ROOT', self.root / 'clients'):
            ids = []
            for profile, idle in [('public', 3600), ('private', 300), ('ha-mcp', 3600)]:
                dev_agent.select_profile(profile)
                ids.append(dev_agent.Session().id)
                self.assertEqual(dev_agent.IDLE_SECONDS, idle)
            self.assertEqual(len(set(ids)), 3)
        dev_agent.select_profile('public')

    def test_retention_deletes_only_expired_other_session_references(self):
        self.storage.save(self.state, SESSION)
        other = 'd' * 32
        self.storage.save(self.state, other)
        self.storage.api = lambda path: {'private': True} if not path else {'committer': {'date': '2000-01-01T00:00:00Z'}}
        self.assertEqual(self.storage.prune(SESSION), 1)
        self.assertEqual(set(self.storage.refs(self.root)), {'refs/heads/state/' + SESSION + '/current'})

    def test_corrupt_current_falls_back_and_preserves_valid_previous(self):
        first = self.storage.save(self.state, SESSION)['commit']
        self.storage.save(self.state, SESSION)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            tmp = Path(tmp)
            self.storage.git(tmp, 'init', '-q')
            (tmp / 'manifest.json').write_text('{}')
            self.storage.git(tmp, 'add', 'manifest.json')
            tree = self.storage.git(tmp, 'write-tree').stdout.strip().decode()
            corrupt = self.storage.git(tmp, 'commit-tree', tree, data=b'corrupt fixture').stdout.strip().decode()
            self.storage.git(tmp, 'push', '--force', self.storage.url, corrupt + ':refs/heads/state/' + SESSION + '/current')
        restored = self.root / 'fallback'
        result = self.storage.load(restored, SESSION)
        self.assertTrue(result['fallback'])
        self.assertEqual(result['commit'], first)
        self.storage.save(restored, SESSION)
        self.assertEqual(self.storage.refs(self.root)['refs/heads/state/' + SESSION + '/previous'], first)

    def test_private_submissions_reject_superseded_and_active_snapshots(self):
        import worker
        store = worker.Store(self.root / 'rpc')
        store.initialize(SESSION)
        (store.root / 'private-policy.json').write_text('{}')
        base = archive(store.root, {'source.py': b'original'})
        request = {'project': PROJECT, 'sha256': base, 'workspace': '1' * 32}
        store.prepare(SESSION, request)
        store.prepare(SESSION, {**request, 'workspace': '2' * 32})
        job = {'job': '3' * 32, 'project': PROJECT, 'workspace': '1' * 32, 'argv': ['true']}
        with self.assertRaisesRegex(ValueError, 'superseded'):
            store.submit(SESSION, job)
        store.submit(SESSION, {**job, 'workspace': '2' * 32})
        with self.assertRaisesRegex(RuntimeError, 'active command'):
            store.prepare(SESSION, {**request, 'workspace': '4' * 32})


if __name__ == '__main__':
    unittest.main(verbosity=2)
