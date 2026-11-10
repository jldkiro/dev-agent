# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Private-only, independent Git checkpoints and three-way file reconciliation."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

LIMIT = 50 * 1024 * 1024
EXPANDED_LIMIT = 512 * 1024 * 1024
EXCLUDED = {'.git', '.ssh', '.agents', '.codex', '.claude', '.config', '.aws', '.azure', '.gnupg', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'local'}


def eligible(path):
    p = PurePosixPath(path)
    name = p.name.lower()
    return not any(x.lower() in EXCLUDED for x in p.parts) and not (
        name == '.env' or name.startswith('.env.') or name.endswith(('.pem', '.p12', '.pfx', '.key', '.log'))
        or name in {'credentials.json', 'auth.json', 'id_rsa', 'id_ed25519', 'client_secret.json', '.mcp.json', 'mcp.json', '.npmrc', '.pypirc', '.netrc', '.git-credentials', '.envrc'})


def ident(value, length=32):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{' + str(length) + '}', value):
        raise ValueError('Invalid checkpoint identity')
    return value


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def files(root):
    root = Path(root)
    found = {}
    for directory, children, names in os.walk(root):
        children[:] = [n for n in children if eligible(n) and not (Path(directory) / n).is_symlink()]
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if eligible(relative) and path.is_file() and not path.is_symlink():
                found[relative] = path
    return found


def extract(archive, destination, maximum=EXPANDED_LIMIT):
    """Never restore links, special files, escaping names or unbounded output."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    seen, total = set(), 0
    with tarfile.open(archive, 'r:*') as source:
        for member in source:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not member.isfile() or member.name in seen:
                raise ValueError('Unsafe checkpoint member')
            total += member.size
            if total > maximum or len(seen) >= 100000:
                raise ValueError('Checkpoint expands beyond limit')
            seen.add(member.name)
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Checkpoint path escapes destination')
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o755 if member.mode & 0o111 else 0o600)


@contextmanager
def project_lock(root):
    # Workers are Linux. flock closes on RPC disconnect; abandoned writers cannot lock forever.
    import fcntl
    with (Path(root) / 'projects.lock').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def projects(root):
    path = Path(root) / 'projects.json'
    data = json.loads(path.read_bytes()) if path.exists() else {}
    for key, record in data.items():
        ident(key)
        ident(record['workspace'])
        ident(record['base'], 64)
    return data


def reconcile(root, project, workspace, base):
    """Preserve remote edits; conflicting local edits reject the new snapshot."""
    root = Path(root)
    ident(project)
    ident(workspace)
    ident(base, 64)
    records = projects(root)
    previous = records.get(project)
    destination = root / 'workspaces' / workspace
    if previous:
        old_archive = root / 'archives' / previous['base']
        if digest(old_archive) != previous['base']:
            raise ValueError('Previous synchronization base is corrupt')
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            baseline = Path(temporary)
            extract(old_archive, baseline)
            original = files(baseline)
            remote = files(root / 'workspaces' / previous['workspace'])
            local = files(destination)
            def signature(path):
                return (digest(path), bool(path.stat().st_mode & 0o111)) if path else None
            changes = []
            for name in sorted(original.keys() | remote.keys() | local.keys()):
                before, after, incoming = signature(original.get(name)), signature(remote.get(name)), signature(local.get(name))
                if after != before:
                    if incoming != before and incoming != after:
                        raise ValueError('Local/remote file conflict: ' + name)
                    changes.append((name, remote.get(name)))
            for name, source in changes:
                target = destination / name
                if target.is_dir() or any(p.is_file() for p in target.parents if p.is_relative_to(destination)):
                    raise ValueError('Local/remote directory conflict: ' + name)
                if source is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    records[project] = {'workspace': workspace, 'base': base}
    temporary = root / 'projects.json.tmp'
    temporary.write_text(json.dumps(records), encoding='utf-8')
    temporary.replace(root / 'projects.json')


def pack(root, output):
    root = Path(root)
    records = projects(root)
    selected = {}
    for record in records.values():
        base = root / 'archives' / record['base']
        if digest(base) != record['base']:
            raise ValueError('Synchronization base is corrupt')
        selected['archives/' + record['base']] = base
        workspace = root / 'workspaces' / record['workspace']
        selected.update({f"workspaces/{record['workspace']}/{name}": p for name, p in files(workspace).items()})
    total = 0
    with Path(output).open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=1, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w|') as archive:
        for name, path in sorted(selected.items()):
            size = path.stat().st_size
            total += size
            if total > EXPANDED_LIMIT:
                raise ValueError('Checkpoint content exceeds limit')
            info = tarfile.TarInfo(name)
            info.size = size
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o600
            with path.open('rb') as source:
                archive.addfile(info, source)
            if raw.tell() > LIMIT:
                raise ValueError('Compressed checkpoint exceeds 50 MiB')
    if Path(output).stat().st_size > LIMIT:
        raise ValueError('Compressed checkpoint exceeds 50 MiB')
    return records


class GitCheckpoint:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid checkpoint repository')
        self.repository, self.token = repository, token
        self.url = 'https://github.com/' + repository + '.git'

    def api(self, path):
        request = Request('https://api.github.com/repos/' + self.repository + path, headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'dev-agent-checkpoints'})
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def verify_private(self):
        data = self.api('')
        if data.get('private') is not True or data.get('full_name', '').lower() != self.repository.lower():
            raise PermissionError('Checkpoint storage requires the verified private repository')

    def git(self, cwd, *args, data=None, check=True):
        env = os.environ.copy()
        env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
            'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + self.token).encode()).decode(),
            'GIT_CONFIG_KEY_1': 'core.hooksPath', 'GIT_CONFIG_VALUE_1': os.devnull,
            'GIT_AUTHOR_NAME': 'dev-agent', 'GIT_AUTHOR_EMAIL': 'dev-agent@users.noreply.github.com',
            'GIT_COMMITTER_NAME': 'dev-agent', 'GIT_COMMITTER_EMAIL': 'dev-agent@users.noreply.github.com'})
        result = subprocess.run(['git', '-C', str(cwd), *args], input=data, capture_output=True, env=env, timeout=120)
        if check and result.returncode:
            raise RuntimeError('Checkpoint Git operation failed: ' + args[0])
        return result

    def refs(self, cwd):
        result = self.git(cwd, 'ls-remote', self.url, 'refs/heads/state/*').stdout.decode()
        return {ref: oid for oid, ref in (line.split() for line in result.splitlines())}

    def load(self, root, session):
        self.verify_private()
        ident(session)
        root = Path(root)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.git(tmp, 'init', '-q')
            refs = self.refs(tmp)
            prefix = 'refs/heads/state/' + session + '/'
            present = [prefix + n for n in ('current', 'previous') if prefix + n in refs]
            for ref in present:
                try:
                    oid = refs[ref]
                    self.git(tmp, 'fetch', '-q', '--depth=1', self.url, ref)
                    meta = json.loads(self.git(tmp, 'show', oid + ':manifest.json').stdout)
                    if meta.get('session') != session or meta.get('repository') != self.repository or meta.get('version') != 1 or not 0 < meta['bytes'] <= LIMIT:
                        raise ValueError('Checkpoint manifest does not match session')
                    archive = tmp / 'restore.tar.gz'
                    archive.write_bytes(self.git(tmp, 'show', oid + ':checkpoint.tar.gz').stdout)
                    if archive.stat().st_size != meta['bytes'] or digest(archive) != meta['sha256']:
                        raise ValueError('Checkpoint checksum mismatch')
                    stage = tmp / 'extracted'
                    if stage.exists():
                        shutil.rmtree(stage)
                    extract(archive, stage)
                    (stage / 'projects.json').write_text(json.dumps(meta['projects']))
                    records = projects(stage)
                    permitted = {'archives/' + r['base'] for r in records.values()}
                    prefixes = tuple('workspaces/' + r['workspace'] + '/' for r in records.values())
                    for p in stage.rglob('*'):
                        if p.is_file():
                            relative = p.relative_to(stage).as_posix()
                            if relative != 'projects.json' and relative not in permitted and not relative.startswith(prefixes):
                                raise ValueError('Unexpected restored state file')
                            if relative.startswith('workspaces/') and not eligible('/'.join(PurePosixPath(relative).parts[2:])):
                                raise ValueError('Excluded file in checkpoint')
                    for r in records.values():
                        if digest(stage / 'archives' / r['base']) != r['base']:
                            raise ValueError('Restored base checksum mismatch')
                    root.mkdir(parents=True, exist_ok=True)
                    for name in ('archives', 'workspaces'):
                        if (stage / name).exists():
                            shutil.copytree(stage / name, root / name, dirs_exist_ok=True)
                    shutil.copy2(stage / 'projects.json', root / 'projects.json')
                    (root / '.restored-commit').write_text(oid)
                    return {'restored': True, 'commit': oid, 'fallback': ref.endswith('/previous')}
                except Exception:
                    if ref == present[-1]:
                        raise RuntimeError('No valid checkpoint could be restored') from None
            return {'restored': False}

    def save(self, root, session):
        self.verify_private()
        ident(session)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / 'checkpoint.tar.gz'
            records = pack(root, archive)
            if not records:
                return {'saved': False, 'empty': True}
            meta = {'version': 1, 'repository': self.repository, 'session': session, 'created_at': time.time(),
                'projects': records, 'bytes': archive.stat().st_size, 'sha256': digest(archive)}
            (tmp / 'manifest.json').write_text(json.dumps(meta), encoding='utf-8')
            self.git(tmp, 'init', '-q')
            refs = self.refs(tmp)
            current, previous = ('refs/heads/state/' + session + '/' + n for n in ('current', 'previous'))
            promotion = current
            restored = Path(root) / '.restored-commit'
            if restored.exists() and refs.get(previous) == restored.read_text().strip():
                promotion = previous  # A corrupt current fell back to the valid previous generation.
            if promotion in refs:
                self.git(tmp, 'fetch', '-q', '--depth=1', self.url, promotion)
            self.git(tmp, 'add', '--', 'checkpoint.tar.gz', 'manifest.json')
            tree = self.git(tmp, 'write-tree').stdout.strip().decode()
            # No -p: every generation is an independent root, never an archive chain.
            oid = self.git(tmp, 'commit-tree', tree, data=('Ephemeral checkpoint\n\nDev-Agent-Session: ' + session + '\n').encode()).stdout.strip().decode()
            args = ['push', '--atomic', '--force-with-lease=' + current + ':' + refs.get(current, ''), '--force-with-lease=' + previous + ':' + refs.get(previous, ''), self.url, oid + ':' + current]
            if promotion in refs:
                args.append(refs[promotion] + ':' + previous)
            try:
                self.git(tmp, *args, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass  # Publication may have succeeded; reconcile only this exact root commit.
            # Reconcile an uncertain push by its exact commit; never replay it.
            if self.refs(tmp).get(current) != oid:
                raise RuntimeError('Checkpoint publication not confirmed')
            return {'saved': True, 'commit': oid, 'bytes': meta['bytes']}

    def prune(self, session, now=None):
        """Remove expired references on use; physical object collection is GitHub's."""
        self.verify_private()
        cutoff = (time.time() if now is None else now) - 7 * 86400
        removed = 0
        with tempfile.TemporaryDirectory() as tmp:
            self.git(tmp, 'init', '-q')
            for ref, oid in self.refs(tmp).items():
                match = re.fullmatch(r'refs/heads/state/([a-f0-9]{32})/(current|previous)', ref)
                if not match or match[1] == session:
                    continue
                from datetime import datetime
                date = self.api('/git/commits/' + oid)['committer']['date']
                if datetime.fromisoformat(date.replace('Z', '+00:00')).timestamp() < cutoff:
                    self.git(tmp, 'push', '--force-with-lease=' + ref + ':' + oid, self.url, ':' + ref)
                    removed += 1
                if removed >= 20:
                    break
        return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['restore', 'save'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--token-file', required=True)
    args = parser.parse_args()
    storage = GitCheckpoint(args.repository, Path(args.token_file).read_text().strip())
    result = storage.load(args.root, args.session) if args.operation == 'restore' else storage.save(args.root, args.session)
    if args.operation == 'save':
        result['expired_refs_removed'] = storage.prune(args.session)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
