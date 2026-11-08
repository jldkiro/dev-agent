# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Linux worker: authenticated SSH RPC, detached jobs, bounded idle lifetime."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid

IDLE_SECONDS = 15 * 60
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_HEADER = 1024 * 1024
FINISHED = {"completed", "cancelled", "failed", "lost"}


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ValueError("Invalid identifier")
    return value


def contained(base, relative):
    if not isinstance(relative, str) or "\\" in relative or "\0" in relative:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Path must stay inside workspace")
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError("Path escapes workspace")
    return result


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = self.root / "state.sqlite"

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self, session, idle=IDLE_SECONDS):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("jobs", "workspaces", "archives"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        with self.transaction() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, session TEXT,
                    phase TEXT, started REAL, activity REAL, idle REAL, reason TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request TEXT,
                    status TEXT, pid INTEGER, code INTEGER, started REAL, finished REAL,
                    cancel INTEGER DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY, pid INTEGER, beat REAL);
            """)
            old = c.execute("SELECT * FROM state WHERE id=1").fetchone()
            if old:
                raise RuntimeError("Refusing to reuse an existing worker state")
            now = time.monotonic()
            c.execute("INSERT INTO state VALUES(1,?,'open',?,?,?,NULL)", (identifier(session), now, now, idle))

    def check(self, c, session, *, writable=False):
        s = c.execute("SELECT * FROM state WHERE id=1").fetchone()
        if not s or s["session"] != session:
            raise PermissionError("Wrong session")
        if writable and s["phase"] != "open":
            raise RuntimeError("Worker is closing; no new work accepted")
        return dict(s)

    def activity(self, c):
        c.execute("UPDATE state SET activity=? WHERE id=1", (time.monotonic(),))

    def status(self, session):
        with self.transaction() as c:
            state = self.check(c, session)
            jobs = [dict(x) for x in c.execute("SELECT id,status,pid,code,started,finished,error FROM jobs ORDER BY rowid")]
            state["idle_remaining_seconds"] = max(0, state["idle"] - (time.monotonic() - state["activity"]))
            state["jobs"] = jobs
            state["ephemeral"] = True
            return state

    def job(self, session, job_id):
        with self.transaction() as c:
            self.check(c, session)
            row = c.execute("SELECT * FROM jobs WHERE id=?", (identifier(job_id),)).fetchone()
            if not row:
                raise KeyError("Unknown job")
            result = dict(row)
            result["request"] = json.loads(result["request"])
            return result

    @contextmanager
    def transfer(self, session):
        lease = uuid.uuid4().hex
        with self.transaction() as c:
            self.check(c, session, writable=True)
            self.activity(c)
            c.execute("INSERT INTO transfers VALUES(?,?,?)", (lease, os.getpid(), time.monotonic()))
        last_beat = time.monotonic()
        def beat():
            nonlocal last_beat
            now = time.monotonic()
            if now - last_beat < 1:
                return
            with self.transaction() as c:
                c.execute("UPDATE transfers SET beat=? WHERE id=?", (now, lease))
                self.activity(c)
            last_beat = now
        try:
            yield beat
        finally:
            with self.transaction() as c:
                c.execute("DELETE FROM transfers WHERE id=?", (lease,))
                self.activity(c)

    def sync(self, session, request, stream):
        size = request["bytes"]
        if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
            raise ValueError("Archive size outside allowed range")
        workspace = identifier(request["workspace"])
        destination = self.root / "workspaces" / workspace
        temporary = self.root / "workspaces" / ("upload-" + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        archive = temporary / "upload.tar"
        digest = hashlib.sha256()
        with self.transfer(session) as beat:
            try:
                remaining = size
                with archive.open("wb") as target:
                    while remaining:
                        data = stream.read(min(1024 * 1024, remaining))
                        if not data:
                            raise EOFError("Incomplete upload")
                        target.write(data)
                        digest.update(data)
                        remaining -= len(data)
                        beat()
                if digest.hexdigest() != request["sha256"]:
                    raise ValueError("Archive digest mismatch")
                result = self.extract(archive, temporary, destination, beat)
                cached = self.root / "archives" / digest.hexdigest()
                archive.replace(cached)
                cached.chmod(0o400)
                return {"workspace": workspace, **result, "reused": False, "ephemeral": True}
            finally:
                if temporary.resolve().parent == (self.root / "workspaces").resolve():
                    shutil.rmtree(temporary)

    def extract(self, archive, temporary, destination, beat):
        extracted = temporary / "files"
        extracted.mkdir()
        total = count = 0
        with tarfile.open(archive, "r:*") as tf:
            for member in tf:
                if not member.isfile():
                    raise ValueError("Only regular files can be uploaded")
                total += member.size
                count += 1
                if total > MAX_ARCHIVE or count > 50000:
                    raise ValueError("Archive expansion exceeds limit")
                target = contained(extracted, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                beat()
        if destination.exists():
            raise ValueError("Workspace identifier already exists")
        extracted.rename(destination)
        return {"files": count, "bytes": total}

    def prepare(self, session, request):
        digest = request["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid source digest")
        workspace = identifier(request["workspace"])
        archive = self.root / "archives" / digest
        with self.transfer(session) as beat:
            if not archive.is_file():
                return {"need_upload": True}
            with archive.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != digest:
                archive.unlink()
                return {"need_upload": True}
            temporary = self.root / "workspaces" / ("prepare-" + uuid.uuid4().hex)
            temporary.mkdir(mode=0o700)
            try:
                result = self.extract(archive, temporary, self.root / "workspaces" / workspace, beat)
            finally:
                if temporary.resolve().parent == (self.root / "workspaces").resolve():
                    shutil.rmtree(temporary)
        return {"workspace": workspace, **result, "reused": True, "ephemeral": True}

    def submit(self, session, request):
        job_id = identifier(request["job"])
        argv = request.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\0" in x for x in argv):
            raise ValueError("argv must be a non-empty string array")
        workspace = self.root / "workspaces" / identifier(request["workspace"])
        cwd = contained(workspace, request.get("cwd", "."))
        if not workspace.is_dir() or not cwd.is_dir():
            raise ValueError("Missing workspace or working directory")
        env = request.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) or "=" in k or "\0" in k + v for k, v in env.items()):
            raise ValueError("Invalid environment")
        normalized = json.dumps({k: request[k] for k in ("argv", "workspace", "cwd", "env") if k in request}, sort_keys=True)
        with self.transaction() as c:
            self.check(c, session, writable=True)
            old = c.execute("SELECT request FROM jobs WHERE id=?", (job_id,)).fetchone()
            if old:
                if old["request"] != normalized:
                    raise ValueError("Job identifier already used by another command")
            else:
                c.execute("INSERT INTO jobs(id,request,status) VALUES(?,?,'queued')", (job_id, normalized))
            self.activity(c)
        return {"job": job_id, "ephemeral": True}

    def poll(self, session, request):
        job = self.job(session, request["job"])
        result = {k: job[k] for k in ("id", "status", "code", "started", "finished", "error")}
        for name in ("stdout", "stderr"):
            offset = request.get(name + "_offset", 0)
            if not isinstance(offset, int) or offset < 0:
                raise ValueError("Invalid log offset")
            path = self.root / "jobs" / job["id"] / name
            data = b""
            if path.exists():
                with path.open("rb") as f:
                    f.seek(offset)
                    data = f.read(256 * 1024)
            result[name] = base64.b64encode(data).decode()
            result[name + "_offset"] = offset + len(data)
            result[name + "_eof"] = not path.exists() or offset + len(data) >= path.stat().st_size
        result["ephemeral"] = True
        return result

    def cancel(self, session, job_id):
        with self.transaction() as c:
            self.check(c, session, writable=True)
            c.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status NOT IN ('completed','cancelled','failed','lost')", (identifier(job_id),))
            self.activity(c)
        return self.job(session, job_id)

    def watch(self, session, request, output):
        offsets = {name + "_offset": request.get(name + "_offset", 0) for name in ("stdout", "stderr")}
        last_status = None
        last_sent = 0
        while True:
            result = self.poll(session, {"job": request["job"], **offsets})
            now = time.monotonic()
            finished = result["status"] in FINISHED and result["stdout_eof"] and result["stderr_eof"]
            if finished or result["stdout"] or result["stderr"] or result["status"] != last_status or now - last_sent >= 10:
                output.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                output.flush()
                last_sent = now
                last_status = result["status"]
                for name in ("stdout", "stderr"):
                    offsets[name + "_offset"] = result[name + "_offset"]
            if finished:
                return
            time.sleep(0.2)

    def stop(self, session, force=False):
        with self.transaction() as c:
            self.check(c, session)
            busy = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','starting','running')").fetchone()[0]
            if busy and not force:
                raise RuntimeError("Commands are active; cancel them or request an explicit force stop")
            c.execute("UPDATE state SET phase='draining',reason='requested' WHERE id=1")
            c.execute("UPDATE jobs SET cancel=1 WHERE status IN ('queued','starting','running')")
        return {"stopping": True, "ephemeral": True}


def group_alive(group):
    if os.name != "posix":
        return False
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    return True
            except (OSError, ValueError, IndexError):
                pass
    return False


def signal_job(process, sig):
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def serve(store, session, idle=IDLE_SECONDS, lifetime=21000, tick=0.5):
    if os.name != "posix" or not Path("/proc").is_dir():
        raise RuntimeError("The worker controller requires Linux; the client is portable")
    store.initialize(session, idle)
    (store.root / "controller.pid").write_text(str(os.getpid()))
    running = {}
    interrupted = False
    def interrupt(*_):
        nonlocal interrupted
        interrupted = True
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        while True:
            now = time.monotonic()
            with store.transaction() as c:
                state = store.check(c, session)
                if interrupted or now - state["started"] >= lifetime:
                    c.execute("UPDATE state SET phase='draining',reason=? WHERE id=1", ("signal" if interrupted else "hard-limit",))
                    c.execute("UPDATE jobs SET cancel=1 WHERE status IN ('queued','starting','running')")
                    state["phase"] = "draining"
                for row in c.execute("SELECT * FROM jobs WHERE status='queued'").fetchall():
                    if row["cancel"] or state["phase"] != "open":
                        c.execute("UPDATE jobs SET status='cancelled',code=130,finished=? WHERE id=?", (now, row["id"]))
                        store.activity(c)
                        continue
                    request = json.loads(row["request"])
                    directory = store.root / "jobs" / row["id"]
                    directory.mkdir(mode=0o700)
                    out = (directory / "stdout").open("wb")
                    err = (directory / "stderr").open("wb")
                    try:
                        # Only this small, explicit environment reaches user commands.
                        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()), "LANG": "C.UTF-8", "CI": "true", "UV_EXCLUDE_NEWER": cutoff}
                        env.update(request.get("env", {}))
                        workspace = store.root / "workspaces" / request["workspace"]
                        process = subprocess.Popen(request["argv"], cwd=contained(workspace, request.get("cwd", ".")), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=os.name == "posix")
                        running[row["id"]] = {"process": process, "out": out, "err": err, "cancel_at": None}
                        c.execute("UPDATE jobs SET status='running',pid=?,started=? WHERE id=?", (process.pid, now, row["id"]))
                    except Exception as e:
                        out.close()
                        err.close()
                        c.execute("UPDATE jobs SET status='failed',code=127,finished=?,error=? WHERE id=?", (now, str(e), row["id"]))
                        store.activity(c)
                for job_id, item in list(running.items()):
                    row = c.execute("SELECT cancel FROM jobs WHERE id=?", (job_id,)).fetchone()
                    process = item["process"]
                    if row["cancel"]:
                        if item["cancel_at"] is None:
                            signal_job(process, signal.SIGTERM)
                            item["cancel_at"] = now
                        elif now - item["cancel_at"] > 3:
                            signal_job(process, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
                    code = process.poll()
                    if code is not None and not group_alive(process.pid):
                        item["out"].close()
                        item["err"].close()
                        c.execute("UPDATE jobs SET status=?,code=?,finished=? WHERE id=?", ("cancelled" if row["cancel"] else "completed", 130 if row["cancel"] else code, now, job_id))
                        store.activity(c)
                        del running[job_id]
                for lease in c.execute("SELECT * FROM transfers").fetchall():
                    try:
                        os.kill(lease["pid"], 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                    if not alive or now - lease["beat"] > idle:
                        c.execute("DELETE FROM transfers WHERE id=?", (lease["id"],))
                busy = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','starting','running')").fetchone()[0]
                transfers = c.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
                latest = c.execute("SELECT * FROM state WHERE id=1").fetchone()
                expired = now - latest["activity"] >= idle
                if not busy and not transfers and (expired or latest["phase"] != "open"):
                    reason = latest["reason"] or "idle-timeout"
                    c.execute("UPDATE state SET phase='closed',reason=? WHERE id=1", (reason,))
                    print(json.dumps({"event": "worker-stopped", "reason": reason}), flush=True)
                    return
            time.sleep(tick)
    finally:
        for item in running.values():
            signal_job(item["process"], signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
            item["out"].close()
            item["err"].close()


def supervise(store, session):
    """Only fixed, allowlisted lifecycle messages may reach Actions stdout."""
    allowed = {"requested", "idle-timeout", "hard-limit", "signal"}
    while True:
        state = store.status(session)
        if state["phase"] == "closed":
            reason = state["reason"] if state["reason"] in allowed else "unknown"
            print("DEV_AGENT_STOPPED reason=" + reason, flush=True)
            return
        pid = int((store.root / "controller.pid").read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise RuntimeError("Controller exited without closing state") from None
        time.sleep(2)


def rpc(store, expected_session, source, output, allow_channel=True):
    line = source.readline(MAX_HEADER + 1)
    if not line:
        return False
    if len(line) > MAX_HEADER:
        raise ValueError("Request header too large")
    request = json.loads(line)
    if request.get("session") != expected_session:
        raise PermissionError("Wrong session")
    op = request.get("op")
    if op == "channel":
        if not allow_channel:
            raise ValueError("Nested channels are not supported")
        with store.transaction() as c:
            store.check(c, expected_session, writable=bool(request.get("writable", False)))
        output.write(b'{"ok":true,"result":{"channel":1,"ephemeral":true}}\n')
        output.flush()
        while rpc(store, expected_session, source, output, allow_channel=False):
            pass
        return True
    if op == "watch":
        store.watch(expected_session, request, output)
        return True
    if op == "status":
        result = store.status(expected_session)
    elif op == "sync":
        result = store.sync(expected_session, request, source)
    elif op == "prepare":
        result = store.prepare(expected_session, request)
    elif op == "submit":
        result = store.submit(expected_session, request)
    elif op == "poll":
        result = store.poll(expected_session, request)
    elif op == "cancel":
        result = store.cancel(expected_session, request["job"])
    elif op == "stop":
        result = store.stop(expected_session, request.get("force", False))
    elif op == "get":
        workspace = store.root / "workspaces" / identifier(request["workspace"])
        path = contained(workspace, request["path"])
        if not path.is_file():
            raise ValueError("Not a regular workspace file")
        with store.transfer(expected_session) as beat:
            with path.open("rb") as source_file:
                digest = hashlib.file_digest(source_file, "sha256").hexdigest()
            output.write((json.dumps({"ok": True, "bytes": path.stat().st_size, "sha256": digest}) + "\n").encode())
            output.flush()
            with path.open("rb") as f:
                while data := f.read(1024 * 1024):
                    output.write(data)
                    output.flush()
                    beat()
        return True
    else:
        raise ValueError("Unsupported operation")
    output.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
    output.flush()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "rpc", "supervise"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    parser.add_argument("--lifetime-seconds", type=float, default=21000)
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.root)
    if args.mode == "serve":
        serve(store, identifier(args.session), args.idle_seconds, args.lifetime_seconds)
    elif args.mode == "supervise":
        try:
            supervise(store, identifier(args.session))
        except Exception:
            print("DEV_AGENT_CONTROLLER_FAILED", flush=True)
            raise SystemExit(1) from None
    else:
        try:
            rpc(store, identifier(args.session), sys.stdin.buffer, sys.stdout.buffer)
        except Exception as e:
            sys.stdout.buffer.write((json.dumps({"ok": False, "error": str(e), "type": type(e).__name__}) + "\n").encode())
            sys.stdout.buffer.flush()
            raise SystemExit(1)


if __name__ == "__main__":
    main()
