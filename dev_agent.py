# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Portable client for session-isolated, ephemeral GitHub development workers."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit
import zipfile

REPOSITORY = "jldkiro/dev-agent"
CONFIG_ROOT = Path(os.environ.get("DEV_AGENT_STATE_DIR", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/state"))) / "JLDIAOS/dev-agent")))
GH_CONFIG = Path(os.environ.get("DEV_AGENT_GH_CONFIG_DIR", str(CONFIG_ROOT / "gh")))
MAX_ARCHIVE = 512 * 1024 * 1024
FINISHED = {"completed", "failed", "cancelled", "lost"}
EXCLUDED = {".git", ".ssh", ".agents", ".codex", ".claude", ".config", ".aws", ".azure", ".gnupg", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "local"}


def trace(operation, begin):
    destination = os.environ.get("DEV_AGENT_TRACE")
    if destination:
        try:
            with Path(destination).open("a", encoding="utf-8") as out:
                out.write(json.dumps({"operation": operation, "seconds": round(time.perf_counter() - begin, 5)}) + "\n")
        except OSError:
            pass


class RemoteRejected(RuntimeError):
    """A complete negative RPC response proves that a request was rejected."""


class Channel:
    """One pinned SSH connection for upload, submit and incremental job output."""
    def __init__(self, session, state):
        self.session = session
        self.state = state

    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        config = self.session.root / self.state["request_id"] / "ssh_config"
        self.process = subprocess.Popen(["ssh", "-F", str(config), "dev-agent-worker", "rpc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        try:
            assert self.request({"op": "channel"})["channel"] == 1
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def send(self, payload):
        self.process.stdin.write((json.dumps({**payload, "session": self.session.id}) + "\n").encode())
        self.process.stdin.flush()

    def reply(self):
        line = self.process.stdout.readline(1024 * 1024)
        if not line:
            self.errors.seek(0)
            raise RuntimeError("SSH channel closed: " + self.errors.read().decode(errors="replace")[-1500:])
        result = json.loads(line)
        if not result.get("ok"):
            raise RemoteRejected(result.get("error", "Worker rejected request"))
        return result

    def request(self, payload, upload=None):
        begin = time.perf_counter()
        try:
            return self._request(payload, upload)
        finally:
            trace("ssh-channel:" + payload.get("op", "unknown"), begin)

    def _request(self, payload, upload=None):
        self.send(payload)
        if upload:
            try:
                with Path(upload).open("rb") as source:
                    shutil.copyfileobj(source, self.process.stdin, 1024 * 1024)
                self.process.stdin.flush()
            except BrokenPipeError:
                # Preserve a complete server rejection instead of hiding it
                # behind a local pipe error. No request is retried.
                self.reply()
                raise RuntimeError("SSH upload ended before its source was sent") from None
        return self.reply().get("result")

    def watch(self, job):
        self.send({"op": "watch", "job": job})
        while True:
            result = self.reply()["result"]
            yield result
            if result["status"] in FINISHED and result["stdout_eof"] and result["stderr_eof"]:
                return

    def __exit__(self, *_):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.errors.close()


def command(args, *, data=None, timeout=60, check=True):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:] or f"{args[0]} failed")
    return result


def gh(path, method="GET", payload=None):
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = str(GH_CONFIG)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(key, None)
    args = ["gh", "api", path, "--method", method]
    if payload is not None:
        args += ["--input", "-"]
    begin = time.perf_counter()
    try:
        result = subprocess.run(args, input=json.dumps(payload).encode() if payload is not None else None, capture_output=True, env=env, timeout=60)
    finally:
        trace("github:" + method + ":" + path.split("?", 1)[0], begin)
    if result.returncode:
        raise RuntimeError("GitHub request failed: " + result.stderr.decode(errors="replace")[-1500:])
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return result.stdout


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        identity = os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]
        command(["icacls", str(path), "/inheritance:r", "/grant:r", identity + ":(OI)(CI)F"])
    else:
        path.chmod(0o700)


def write_json(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes((json.dumps(data, indent=2) + "\n").encode())
    temporary.replace(path)


@contextmanager
def locked(path, seconds=360):
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        end = time.monotonic() + seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= end:
                    raise TimeoutError("Another call is allocating this session's worker")
                time.sleep(0.25)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cloudflared():
    configured = os.environ.get("DEV_AGENT_CLOUDFLARED")
    if configured:
        return Path(configured).resolve()
    if os.name == "nt":
        path = Path(os.environ["LOCALAPPDATA"]) / "JLDIAOS/remote-code/cloudflared/cloudflared.exe"
        if path.is_file():
            return path
    found = shutil.which("cloudflared")
    if not found:
        raise RuntimeError("cloudflared is required on the client")
    return Path(found).resolve()


class Session:
    def __init__(self):
        owner = os.environ.get("CODEX_THREAD_ID") or os.environ.get("T3_THREAD_ID")
        if not owner:
            raise RuntimeError("A CODEX_THREAD_ID or T3_THREAD_ID is required; workers must belong to a session")
        self.id = hashlib.sha256(("dev-agent:" + owner).encode()).hexdigest()[:32]
        self.root = CONFIG_ROOT / "sessions" / self.id
        if not self.root.exists():
            private_directory(self.root)
        (self.root / "jobs").mkdir(exist_ok=True)
        self.state_path = self.root / "session.json"

    def read(self):
        return json.loads(self.state_path.read_bytes()) if self.state_path.exists() else None

    def save(self, state):
        write_json(self.state_path, state)

    def configure_ssh(self, state, connection):
        if connection.get("repository") != REPOSITORY or connection.get("sha") != state["sha"] or connection.get("run_id") != state["run_id"] or connection.get("session_id") != self.id or connection.get("request_id") != state["request_id"]:
            raise RuntimeError("Connection metadata does not match the requested run/session/revision")
        if connection.get("protocol") != 1 or connection.get("idle_seconds") != 900 or not connection.get("ephemeral"):
            raise RuntimeError("Unexpected worker protocol or lifetime policy")
        hostname = urlsplit(connection["url"]).hostname
        if not hostname or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", hostname):
            raise RuntimeError("Unexpected tunnel hostname")
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", connection["host_key"]):
            raise RuntimeError("Invalid SSH host key")
        instance = self.root / state["request_id"]
        (instance / "known_hosts").write_bytes(("dev-agent-worker " + connection["host_key"] + "\n").encode())
        config = f'''Host dev-agent-worker
    HostName {hostname}
    HostKeyAlias dev-agent-worker
    User devagent
    IdentityFile "{(instance / 'identity').as_posix()}"
    UserKnownHostsFile "{(instance / 'known_hosts').as_posix()}"
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 25
    ServerAliveInterval 10
    ServerAliveCountMax 2
    ForwardAgent no
    ProxyCommand "{cloudflared().as_posix()}" access ssh --hostname %h
'''
        (instance / "ssh_config").write_bytes(config.encode())
        state["connection"] = connection
        state["phase"] = "ready"
        self.save(state)

    def ensure(self):
        cloudflared()
        with locked(self.root / "allocation.lock"):
            state = self.read()
            if state and state.get("run_id"):
                run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
                if run["status"] == "completed":
                    self._retire_locked(state)
                    state = None
            if state and state.get("phase") == "ready":
                result = self.rpc({"op": "status"}, state=state)
                if result["phase"] == "open":
                    return state
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
                    if run["status"] == "completed":
                        self._retire_locked(state)
                        state = None
                        break
                    time.sleep(2)
                if state:
                    raise RuntimeError("The previous worker is closing. Wait for its run to finish before allocating another.")
            if not state or state.get("phase") == "retired":
                owner, name = REPOSITORY.split("/")
                query = 'query { repository(owner: ' + json.dumps(owner) + ', name: ' + json.dumps(name) + ') { isPrivate defaultBranchRef { name target { oid } } } }'
                repo = gh("graphql", "POST", {"query": query}).get("data", {}).get("repository")
                if not repo or not repo.get("defaultBranchRef"):
                    raise RuntimeError("Controller repository is inaccessible or has no default branch")
                if repo["isPrivate"]:
                    raise RuntimeError("This installation expects the explicitly chosen public controller repository")
                branch = repo["defaultBranchRef"]["name"]
                commit = repo["defaultBranchRef"]["target"]["oid"]
                request_id = uuid.uuid4().hex
                instance = self.root / request_id
                private_directory(instance)
                command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dev-agent-session", "-f", str(instance / "identity")])
                state = {"session_id": self.id, "request_id": request_id, "sha": commit, "branch": branch, "phase": "allocating", "created_at": time.time(), "ephemeral": True}
                self.save(state)  # Persist before the non-idempotent allocation request.
                try:
                    gh(f"repos/{REPOSITORY}/actions/workflows/worker.yml/dispatches", "POST", {"ref": branch, "inputs": {"session_id": self.id, "request_id": request_id, "public_key": (instance / "identity.pub").read_text().strip()}})
                except Exception:
                    state["phase"] = "allocation-uncertain"
                    self.save(state)
                    raise RuntimeError("Allocation outcome uncertain. Run status/start to reconcile; do not send another dispatch manually.")
            print("Allocating ephemeral worker for this session…", file=sys.stderr, flush=True)
            deadline = time.monotonic() + 300
            poll_count = 0
            while time.monotonic() < deadline:
                if not state.get("run_id"):
                    runs = gh(f"repos/{REPOSITORY}/actions/workflows/worker.yml/runs?event=workflow_dispatch&per_page=100")["workflow_runs"]
                    title = "dev-agent " + self.id + " " + state["request_id"]
                    matches = [r for r in runs if r["display_title"] == title]
                    if len(matches) > 1:
                        raise RuntimeError("Duplicate allocations detected; inspect and stop the extra jobs")
                    if matches:
                        state["run_id"] = matches[0]["id"]
                        state["run_url"] = matches[0]["html_url"]
                        self.save(state)
                        if matches[0]["head_sha"] != state["sha"]:
                            if matches[0]["status"] != "completed":
                                gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}/cancel", "POST")
                            raise RuntimeError("Controller revision changed during allocation; this exact run was identified and cancelled. Reconcile it before starting another.")
                if state.get("run_id"):
                    artifacts = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}/artifacts")["artifacts"]
                    found = [a for a in artifacts if a["name"] == "connection-" + state["request_id"] and not a["expired"]]
                    if found:
                        data = gh(f"repos/{REPOSITORY}/actions/artifacts/{found[0]['id']}/zip")
                        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
                            connection = json.loads(archive.read("connection.json"))
                        self.configure_ssh(state, connection)
                        for attempt in range(3):
                            try:
                                if self.rpc({"op": "status"}, state=state)["phase"] == "open":
                                    return state
                            except (RuntimeError, OSError):
                                if attempt == 2:
                                    raise
                                time.sleep(2)
                        raise RuntimeError("Allocated worker did not become ready")
                    if poll_count % 3 == 0:
                        run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
                        if run["status"] == "completed":
                            raise RuntimeError("Worker allocation ended: " + str(run["conclusion"]) + "; " + state["run_url"])
                poll_count += 1
                time.sleep(1.5)
            raise TimeoutError("Worker not ready after five minutes. Its allocation is recorded; use status/start to continue, not a new manual dispatch.")

    def retire(self, state):
        with locked(self.root / "allocation.lock"):
            self._retire_locked(state)

    def _retire_locked(self, state):
        instance = self.root / state["request_id"]
        (instance / "identity").unlink(missing_ok=True)
        if state.get("run_id"):
            for artifact in gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}/artifacts")["artifacts"]:
                if artifact["name"] == "connection-" + state["request_id"]:
                    gh(f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}", "DELETE")
        state["phase"] = "retired"
        state["retired_at"] = time.time()
        write_json(instance / "retired.json", state)
        current = self.read()
        if current and current["request_id"] == state["request_id"]:
            self.save(state)

    def rpc(self, payload, *, state=None, upload=None, download=None):
        begin = time.perf_counter()
        try:
            return self._rpc(payload, state=state, upload=upload, download=download)
        finally:
            trace("ssh:" + payload.get("op", "unknown"), begin)

    def _rpc(self, payload, *, state=None, upload=None, download=None):
        state = state or self.read()
        if not state or state.get("phase") != "ready":
            raise RuntimeError("No ready worker in this session; start it first")
        payload = {**payload, "session": self.id}
        config = self.root / state["request_id"] / "ssh_config"
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["ssh", "-F", str(config), "dev-agent-worker", "rpc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            try:
                process.stdin.write((json.dumps(payload) + "\n").encode())
                if upload:
                    try:
                        with Path(upload).open("rb") as source:
                            shutil.copyfileobj(source, process.stdin, 1024 * 1024)
                    except BrokenPipeError:
                        header = process.stdout.readline(1024 * 1024)
                        if header:
                            response = json.loads(header)
                            if not response.get("ok"):
                                raise RemoteRejected(response.get("error", "Upload rejected")) from None
                        errors.seek(0)
                        raise RuntimeError("SSH upload closed: " + errors.read().decode(errors="replace")[-1500:]) from None
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                header = process.stdout.readline(1024 * 1024)
                if not header:
                    process.wait(timeout=35)
                    errors.seek(0)
                    raise RuntimeError("SSH RPC failed: " + errors.read().decode(errors="replace")[-1500:])
                response = json.loads(header)
                if not response.get("ok"):
                    raise RemoteRejected(response.get("error", "Worker rejected request"))
                if download:
                    remaining = response["bytes"]
                    digest = hashlib.sha256()
                    with Path(download).open("wb") as target:
                        while remaining:
                            data = process.stdout.read(min(1024 * 1024, remaining))
                            if not data:
                                raise EOFError("Incomplete download")
                            target.write(data)
                            digest.update(data)
                            remaining -= len(data)
                    if digest.hexdigest() != response["sha256"]:
                        raise ValueError("Download checksum mismatch")
                process.wait(timeout=35)
                if process.returncode:
                    raise RuntimeError("SSH terminated after the RPC response")
                return response.get("result", response)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()

    def job_record(self, job):
        if not re.fullmatch(r"[a-f0-9]{32}", job):
            raise ValueError("Invalid job identifier")
        return json.loads((self.root / "jobs" / (job + ".json")).read_bytes())

    def job_state(self, job):
        record = self.job_record(job)
        state = self.read()
        if not state or state["request_id"] != record["request_id"] or state.get("phase") != "ready":
            raise RuntimeError("This job belonged to a retired ephemeral worker. It must not be resubmitted automatically.")
        return record, state


def allowed(path):
    parts = path.parts
    name = path.name.lower()
    return not any(p in EXCLUDED for p in parts) and not (name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".p12", ".pfx", ".key")) or name in {"credentials.json", "auth.json", "id_rsa", "id_ed25519", "client_secret.json", ".mcp.json", "mcp.json", ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".envrc"})


def project_archive(project, destination, *, compressed=False):
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("Project directory does not exist")
    result = command(["git", "-C", str(project), "rev-parse", "--show-toplevel"], check=False)
    if result.returncode == 0:
        # Resolve paths from the explicitly selected directory, not an ambient parent repo.
        listing = command(["git", "-C", str(project), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).stdout
        files = [Path(os.fsdecode(x)) for x in listing.split(b"\0") if x]
        stage = command(["git", "-C", str(project), "ls-files", "--stage", "-z"]).stdout
        executable = {Path(os.fsdecode(row.split(b"\t", 1)[1])) for row in stage.split(b"\0") if row.startswith(b"100755 ")}
    else:
        files = []
        executable = set()
        for directory, children, names in os.walk(project):
            children[:] = [x for x in children if x not in EXCLUDED and not (Path(directory) / x).is_symlink()]
            files += [Path(directory).relative_to(project) / x for x in names]
    count = total = 0
    parents = {}
    with ExitStack() as stack:
        target = stack.enter_context(Path(destination).open("wb"))
        if compressed:
            target = stack.enter_context(gzip.GzipFile(filename="", mode="wb", compresslevel=1, mtime=0, fileobj=target))
        archive = stack.enter_context(tarfile.open(fileobj=target, mode="w|"))
        for relative in sorted(set(files)):
            if not allowed(relative):
                continue
            source = project / relative
            try:
                metadata = source.lstat()
            except FileNotFoundError:
                continue
            is_link = stat.S_ISLNK(metadata.st_mode)
            if is_link:
                metadata = source.stat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if source.parent not in parents:
                parents[source.parent] = source.parent.resolve()
            resolved = source.resolve() if is_link else parents[source.parent] / source.name
            if not resolved.is_relative_to(project):
                raise ValueError("Project symlink points outside the selected directory: " + str(relative))
            total += metadata.st_size
            count += 1
            if total > MAX_ARCHIVE:
                raise ValueError("Selected project exceeds 512 MiB; select a smaller project")
            info = tarfile.TarInfo(relative.as_posix())
            info.size = metadata.st_size
            info.mtime = metadata.st_mtime_ns / 1000000000
            info.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
            if relative in executable:
                info.mode |= 0o111
            with source.open("rb") as f:
                archive.addfile(info, f)
    return count, total


def synchronize(session, state, project, channel=None):
    workspace = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="dev-agent-upload-", dir=session.root) as temporary:
        archive = Path(temporary) / "source.tar"
        capabilities = state.get("connection", {}).get("capabilities", [])
        project_archive(project, archive, compressed="gzip" in capabilities)
        with archive.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        result = None
        if "source-cache" in capabilities:
            payload = {"op": "prepare", "workspace": workspace, "sha256": digest}
            result = channel.request(payload) if channel else session.rpc(payload, state=state)
            if result.get("need_upload"):
                result = None
        if result is None:
            payload = {"op": "sync", "workspace": workspace, "bytes": archive.stat().st_size, "sha256": digest}
            result = channel.request(payload, upload=archive) if channel else session.rpc(payload, state=state, upload=archive)
    with locked(session.root / "allocation.lock"):
        current = session.read()
        if not current or current["request_id"] != state["request_id"] or current["phase"] != "ready":
            raise RuntimeError("Upload belonged to a worker retired during the transfer; no command was submitted")
        current["workspace"] = workspace
        current["project"] = str(Path(project).resolve())
        session.save(current)
        state.update(current)
    return result


def stream_job(session, job, *, follow=True, return_code=True, channel=None):
    record, state = session.job_state(job)
    offsets = {"stdout_offset": 0, "stderr_offset": 0}
    def frames():
        if channel and follow:
            yield from channel.watch(job)
            return
        while True:
            payload = {"op": "poll", "job": job, **offsets}
            yield channel.request(payload) if channel else session.rpc(payload, state=state)
            if follow:
                time.sleep(1)
    for result in frames():
        for name in ("stdout", "stderr"):
            data = base64.b64decode(result[name])
            if data:
                target = getattr(sys, name)
                target.buffer.write(data)
                target.flush()
            offsets[name + "_offset"] = result[name + "_offset"]
        if result["status"] in FINISHED and result["stdout_eof"] and result["stderr_eof"]:
            record.update({"status": result["status"], "exit_code": result["code"], "finished_at": time.time()})
            write_json(session.root / "jobs" / (job + ".json"), record)
            code = result["code"] if result["code"] is not None else 1
            return max(0, min(255, code if code >= 0 else 128 - code)) if return_code else 0
        if not follow and result["stdout_eof"] and result["stderr_eof"]:
            return 0


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def execute(session, args, argv):
    state = session.ensure()
    with ExitStack() as stack:
        channel = stack.enter_context(Channel(session, state)) if "channel" in state.get("connection", {}).get("capabilities", []) else None
        synchronized = synchronize(session, state, args.project, channel=channel)
        job = uuid.uuid4().hex
        record = {"job": job, "request_id": state["request_id"], "run_id": state["run_id"], "run_url": state["run_url"], "workspace": synchronized["workspace"], "submitted_at": time.time(), "status": "submission-pending", "ephemeral": True}
        write_json(session.root / "jobs" / (job + ".json"), record)
        payload = {"op": "submit", "job": job, "workspace": record["workspace"], "cwd": args.cwd, "argv": argv, "env": {}}
        try:
            channel.request(payload) if channel else session.rpc(payload, state=state)
        except RemoteRejected as error:
            record.update({"status": "submission-rejected", "error": str(error)})
            write_json(session.root / "jobs" / (job + ".json"), record)
            raise
        except Exception:
            try:
                session.rpc({"op": "poll", "job": job}, state=state)
            except Exception:
                record["status"] = "submission-uncertain"
                write_json(session.root / "jobs" / (job + ".json"), record)
                raise RuntimeError(f"Submission outcome uncertain. Keep job {job}; use status --job {job}. Do not repeat the command on a new worker.")
        record["status"] = "submitted"
        write_json(session.root / "jobs" / (job + ".json"), record)
        if args.detach:
            show(record)
        else:
            print(json.dumps({"job": job, "run_url": state["run_url"], "ephemeral": True}), file=sys.stderr, flush=True)
            return stream_job(session, job, channel=channel)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("start")
    status = commands.add_parser("status")
    status.add_argument("--job")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--project", required=True, help="Explicit local directory to upload; never an implicit personal repo")
    run_parser.add_argument("--cwd", default=".", help="Relative working directory inside that uploaded project")
    run_parser.add_argument("--detach", action="store_true")
    run_parser.add_argument("argv", nargs=argparse.REMAINDER)
    for name in ("logs", "wait", "cancel"):
        item = commands.add_parser(name)
        item.add_argument("job")
        if name == "logs":
            item.add_argument("--follow", action="store_true")
    get = commands.add_parser("get")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("--job", required=True)
    get.add_argument("--overwrite", action="store_true")
    stop = commands.add_parser("stop")
    stop.add_argument("--force", action="store_true")
    args = parser.parse_args()
    session = Session()
    if args.operation == "start":
        state = session.ensure()
        show({"session": session.id, "run_url": state["run_url"], "ephemeral": True, "idle_seconds": 900})
    elif args.operation == "status":
        state = session.read()
        if not state or not state.get("run_id"):
            show({"phase": state["phase"] if state else "not-started", "session": session.id, "ephemeral": True})
        else:
            run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
            if run["status"] == "completed":
                session.retire(state)
                show({"phase": "retired", "conclusion": run["conclusion"], "run_url": state["run_url"], "ephemeral": True})
            elif state.get("phase") != "ready":
                show({"phase": state["phase"], "run_url": state["run_url"], "ephemeral": True})
            elif args.job:
                session.job_state(args.job)
                result = session.rpc({"op": "poll", "job": args.job})
                show({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
            else:
                show({"run_url": state["run_url"], **session.rpc({"op": "status"})})
    elif args.operation == "run":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv:
            parser.error("run requires a command after --")
        if not Path(args.project).is_dir():
            raise ValueError("The explicitly selected project directory does not exist")
        return execute(session, args, argv)
    elif args.operation in ("wait", "logs"):
        _, state = session.job_state(args.job)
        with ExitStack() as stack:
            channel = stack.enter_context(Channel(session, state)) if "channel" in state.get("connection", {}).get("capabilities", []) else None
            return stream_job(session, args.job, follow=args.operation == "wait" or args.follow, return_code=args.operation == "wait", channel=channel)
    elif args.operation == "cancel":
        record, state = session.job_state(args.job)
        result = session.rpc({"op": "cancel", "job": args.job}, state=state)
        show({"job": args.job, "status": result["status"], "cancel_requested": True, "ephemeral": True})
    elif args.operation == "get":
        record, state = session.job_state(args.job)
        destination = Path(args.local).resolve()
        if destination.exists() and not args.overwrite:
            raise ValueError("Destination exists; choose another path or explicitly pass --overwrite")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".download")
        try:
            result = session.rpc({"op": "get", "workspace": record["workspace"], "path": args.remote}, state=state, download=temporary)
            if args.overwrite:
                temporary.replace(destination)
            else:
                os.link(temporary, destination)  # Atomic no-clobber, even if another process created the destination.
                temporary.unlink()
        finally:
            temporary.unlink(missing_ok=True)
        show({"saved": str(destination), "bytes": result["bytes"], "sha256": result["sha256"]})
    elif args.operation == "stop":
        state = session.read()
        if not state:
            show({"phase": "not-started", "ephemeral": True})
            return 0
        if state.get("run_id"):
            run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
            if run["status"] != "completed":
                if state.get("phase") == "ready":
                    try:
                        session.rpc({"op": "stop", "force": args.force}, state=state)
                    except Exception:
                        if not args.force:
                            raise
                elif not args.force:
                    raise RuntimeError("Allocation is not ready; use stop --force to cancel it")
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    run = gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}")
                    if run["status"] == "completed":
                        break
                    time.sleep(2)
                if run["status"] != "completed":
                    gh(f"repos/{REPOSITORY}/actions/runs/{state['run_id']}/cancel", "POST")
                    show({"phase": "cancel-requested", "run_url": state["run_url"], "ephemeral": True})
                    return 0
            session.retire(state)
            show({"phase": "retired", "run_url": state["run_url"], "ephemeral": True})
        else:
            raise RuntimeError("Allocation outcome not yet reconciled. Run start to locate its workflow before retiring keys.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, TimeoutError, OSError, KeyError) as error:
        print("dev-agent: " + str(error), file=sys.stderr)
        raise SystemExit(1)
