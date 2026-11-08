# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral tests for worker isolation, lifetime, process groups and file safety."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker
import dev_agent


def tar_bytes(name="hello.txt", data=b"hello", kind=None):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "../../escape"
            archive.addfile(info)
        else:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return result.getvalue()


def sync(store, session="test-session", workspace="snapshot", data=None):
    data = data or tar_bytes()
    return store.sync(session, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "workspace": workspace}, io.BytesIO(data))


class StorageSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-agent-test-")
        self.root = Path(self.temp.name)
        self.store = worker.Store(self.root / "worker")
        self.store.initialize("test-session")

    def tearDown(self):
        self.temp.cleanup()

    def test_wrong_session_cannot_inspect_or_submit(self):
        with self.assertRaises(PermissionError):
            self.store.status("other-session")
        with self.assertRaises(PermissionError):
            self.store.stop("other-session", True)
        self.assertEqual(self.store.status("test-session")["phase"], "open")

    def test_archive_traversal_and_links_are_rejected(self):
        for name, kind in [("../escape", None), ("/absolute-escape", None), ("link", "symlink")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                sync(self.store, workspace="bad", data=tar_bytes(name, kind=kind))
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.store.root / "workspaces/bad").exists())

    def test_upload_checksum_and_incomplete_stream(self):
        data = tar_bytes()
        with self.assertRaises(ValueError):
            self.store.sync("test-session", {"bytes": len(data), "workspace": "bad", "sha256": "0" * 64}, io.BytesIO(data))
        with self.assertRaises(EOFError):
            self.store.sync("test-session", {"bytes": len(data), "workspace": "bad", "sha256": hashlib.sha256(data).hexdigest()}, io.BytesIO(data[:100]))

    def test_cached_archive_creates_fresh_workspace_without_reusing_mutations(self):
        data = tar_bytes()
        sync(self.store, data=data)
        (self.store.root / "workspaces/snapshot/hello.txt").write_text("changed by previous job")
        request = {"workspace": "fresh", "sha256": hashlib.sha256(data).hexdigest()}
        result = self.store.prepare("test-session", request)
        self.assertTrue(result["reused"])
        self.assertEqual((self.store.root / "workspaces/fresh/hello.txt").read_bytes(), b"hello")
        archive = self.store.root / "archives" / request["sha256"]
        archive.chmod(0o600)
        archive.write_bytes(b"corrupt cached data")
        self.assertTrue(self.store.prepare("test-session", {**request, "workspace": "after-corruption"})["need_upload"])

    def test_gzip_snapshot_is_deterministic_and_extracts_safely(self):
        project = self.root / "gzip-project"
        project.mkdir()
        (project / "source.txt").write_text("compressible source\n" * 2000)
        first = self.root / "first.gz"
        second = self.root / "second.gz"
        dev_agent.project_archive(project, first, compressed=True)
        dev_agent.project_archive(project, second, compressed=True)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertLess(first.stat().st_size, (project / "source.txt").stat().st_size // 5)
        sync(self.store, data=first.read_bytes())
        self.assertEqual((self.store.root / "workspaces/snapshot/source.txt").read_text(), (project / "source.txt").read_text())

    def test_channel_frames_binary_upload_and_multiple_requests_without_renewing_reads(self):
        data = tar_bytes()
        messages = [
            {"session": "test-session", "op": "channel"},
            {"session": "test-session", "op": "sync", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "workspace": "channel-work"},
        ]
        framed = b"".join((json.dumps(x) + "\n").encode() for x in messages) + data
        framed += (json.dumps({"session": "test-session", "op": "status"}) + "\n").encode() * 2
        output = io.BytesIO()
        worker.rpc(self.store, "test-session", io.BytesIO(framed), output)
        replies = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertTrue(all(x["ok"] for x in replies))
        self.assertEqual(replies[-2]["result"]["activity"], replies[-1]["result"]["activity"])
        self.assertEqual((self.store.root / "workspaces/channel-work/hello.txt").read_bytes(), b"hello")

    def test_public_supervisor_never_prints_untrusted_reason(self):
        with self.store.transaction() as conn:
            conn.execute("UPDATE state SET phase='closed',reason='PRIVATE_REASON_CANARY' WHERE id=1")
        from contextlib import redirect_stdout
        output = io.StringIO()
        with redirect_stdout(output):
            worker.supervise(self.store, "test-session")
        self.assertEqual(output.getvalue(), "DEV_AGENT_STOPPED reason=unknown\n")

    def test_public_supervisor_exception_is_a_fixed_message_only(self):
        from contextlib import redirect_stdout
        output = io.StringIO()
        argv = ["worker.py", "supervise", "--root", str(self.store.root), "--session", "test-session"]
        with patch.object(sys, "argv", argv), patch.object(worker, "supervise", side_effect=RuntimeError("PRIVATE_EXCEPTION_CANARY")), redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                worker.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(output.getvalue(), "DEV_AGENT_CONTROLLER_FAILED\n")

    def test_project_symlink_cannot_copy_an_external_file(self):
        project = self.root / "symlink-project"
        project.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("must not leave the client")
        try:
            (project / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("File symlinks unavailable on this Windows client")
        with self.assertRaises(ValueError):
            dev_agent.project_archive(project, self.root / "unsafe.tar")

    def test_submission_is_idempotent_and_conflicting_reuse_is_rejected(self):
        sync(self.store)
        request = {"job": "same-job", "workspace": "snapshot", "argv": ["echo", "once"], "cwd": ".", "env": {}}
        self.store.submit("test-session", request)
        self.store.submit("test-session", request)
        self.assertEqual(len(self.store.status("test-session")["jobs"]), 1)
        with self.assertRaises(ValueError):
            self.store.submit("test-session", {**request, "argv": ["echo", "twice"]})

    def test_command_directory_and_download_cannot_escape_workspace(self):
        sync(self.store)
        with self.assertRaises(ValueError):
            self.store.submit("test-session", {"job": "bad", "workspace": "snapshot", "cwd": "../..", "argv": ["true"]})
        request = {"session": "test-session", "op": "get", "workspace": "snapshot", "path": "../../state.sqlite"}
        with self.assertRaises(ValueError):
            worker.rpc(self.store, "test-session", io.BytesIO(json.dumps(request).encode() + b"\n"), io.BytesIO())

    def test_status_polling_does_not_extend_idle_deadline(self):
        before = self.store.status("test-session")["activity"]
        for _ in range(4):
            self.store.status("test-session")
        self.assertEqual(self.store.status("test-session")["activity"], before)

    def test_rpc_rejects_another_session_before_any_write(self):
        before = self.store.status("test-session")["activity"]
        with self.assertRaises(PermissionError):
            worker.rpc(self.store, "test-session", io.BytesIO(b'{"session":"other-session","op":"stop","force":true}\n'), io.BytesIO())
        self.assertEqual(self.store.status("test-session")["activity"], before)

    def test_archive_filters_exclude_secrets_dependencies_and_outer_symlinks(self):
        project = self.root / "project"
        project.mkdir()
        (project / "source.py").write_text("print('source')")
        (project / ".env").write_text("PRIVATE=must-not-upload")
        (project / "credentials.json").write_text("must-not-upload")
        (project / ".mcp.json").write_text('{"token":"must-not-upload"}')
        (project / ".agents").mkdir()
        (project / ".agents/private.md").write_text("do not upload ambient agent context")
        (project / "node_modules").mkdir()
        (project / "node_modules/big.js").write_text("unnecessary")
        archive = self.root / "source.tar"
        dev_agent.project_archive(project, archive)
        with tarfile.open(archive) as tf:
            self.assertEqual(tf.getnames(), ["source.py"])

    def test_active_worker_refuses_non_forced_shutdown(self):
        sync(self.store)
        self.store.submit("test-session", {"job": "busy", "workspace": "snapshot", "argv": ["sleep", "5"]})
        with self.assertRaises(RuntimeError):
            self.store.stop("test-session")
        self.assertEqual(self.store.status("test-session")["phase"], "open")

    def test_git_snapshot_includes_dirty_and_untracked_files_with_executable_mode(self):
        project = self.root / "git-project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "tracked.sh").write_text("original")
        subprocess.run(["git", "-C", str(project), "add", "tracked.sh"], check=True)
        subprocess.run(["git", "-C", str(project), "update-index", "--chmod=+x", "tracked.sh"], check=True)
        (project / "tracked.sh").write_text("changed locally")
        (project / "untracked.py").write_text("uncommitted")
        (project / ".gitignore").write_text("ignored.bin\n")
        (project / "ignored.bin").write_bytes(b"do not upload")
        archive = self.root / "git.tar"
        dev_agent.project_archive(project, archive)
        with tarfile.open(archive) as tf:
            self.assertEqual(tf.extractfile("tracked.sh").read(), b"changed locally")
            self.assertTrue(tf.getmember("tracked.sh").mode & 0o111)
            self.assertIn("untracked.py", tf.getnames())
            self.assertNotIn("ignored.bin", tf.getnames())


class ClientIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-agent-client-")
        self.patches = [patch.object(dev_agent, "CONFIG_ROOT", Path(self.temp.name)), patch.dict(os.environ, {"CODEX_THREAD_ID": "client-isolation-test"})]
        for item in self.patches:
            item.start()
        self.session = dev_agent.Session()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_job_path_traversal_is_rejected_before_file_access(self):
        with self.assertRaises(ValueError):
            self.session.job_record("../../private")

    def test_connection_metadata_must_match_every_allocation_identity(self):
        state = {"request_id": "a" * 32, "run_id": 123, "sha": "b" * 40}
        instance = self.session.root / state["request_id"]
        instance.mkdir()
        connection = {"repository": dev_agent.REPOSITORY, "sha": state["sha"], "run_id": state["run_id"], "session_id": self.session.id, "request_id": state["request_id"], "protocol": 1, "idle_seconds": 900, "ephemeral": True, "url": "https://fixture.trycloudflare.com", "host_key": "ssh-ed25519 AAAA"}
        for key, wrong in [("repository", "other/repo"), ("sha", "c" * 40), ("run_id", 456), ("session_id", "wrong-session"), ("request_id", "wrong-request")]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.session.configure_ssh(state, {**connection, key: wrong})
            self.assertFalse((instance / "known_hosts").exists())

    def test_retiring_old_worker_cannot_overwrite_a_new_allocation(self):
        old = {"request_id": "a" * 32, "phase": "ready"}
        new = {"request_id": "b" * 32, "phase": "ready"}
        for state in (old, new):
            instance = self.session.root / state["request_id"]
            instance.mkdir()
            (instance / "identity").write_text("test fixture, not an SSH key")
        self.session.save(new)
        self.session.retire(old)
        self.assertEqual(self.session.read(), new)
        self.assertFalse((self.session.root / old["request_id"] / "identity").exists())
        self.assertTrue((self.session.root / new["request_id"] / "identity").exists())

    def test_stale_upload_cannot_restore_a_retired_worker_state(self):
        old = {"request_id": "a" * 32, "phase": "ready"}
        new = {"request_id": "b" * 32, "phase": "ready"}
        self.session.save(old)
        project = Path(self.temp.name) / "project"
        project.mkdir()
        (project / "source.py").write_text("pass")
        def replacement(*args, **kwargs):
            self.session.save(new)
            return {"workspace": "fixture"}
        with patch.object(self.session, "rpc", side_effect=replacement), self.assertRaises(RuntimeError):
            dev_agent.synchronize(self.session, old, project)
        self.assertEqual(self.session.read(), new)


@unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux process controller")
class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-agent-lifecycle-")
        self.root = Path(self.temp.name)
        self.store = worker.Store(self.root / "state")
        self.log = (self.root / "controller.log").open("wb")
        self.process = subprocess.Popen([sys.executable, str(ROOT / "worker.py"), "serve", "--root", str(self.store.root), "--session", "test-session", "--idle-seconds", "2", "--lifetime-seconds", "25"], stdout=self.log, stderr=self.log)
        end = time.monotonic() + 5
        while True:
            try:
                if self.store.status("test-session")["phase"] == "open":
                    break
            except (sqlite3.Error, FileNotFoundError):
                pass
            if time.monotonic() > end or self.process.poll() is not None:
                self.fail("Controller did not start")
            time.sleep(0.05)
        sync(self.store)

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        self.temp.cleanup()

    def until(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("Expected state was not reached")

    def submit(self, name, argv):
        self.store.submit("test-session", {"job": name, "workspace": "snapshot", "argv": argv, "cwd": ".", "env": {}})

    def test_command_survives_disconnect_and_exceeds_idle_then_gets_full_idle_period(self):
        self.submit("long", [sys.executable, "-c", "import time; print('begin',flush=True); time.sleep(3); print('end')"])
        self.until(lambda: self.store.job("test-session", "long")["status"] == "running")
        time.sleep(2.2)
        self.assertEqual(self.store.status("test-session")["phase"], "open")
        self.assertEqual(self.store.job("test-session", "long")["status"], "running")
        self.until(lambda: self.store.job("test-session", "long")["status"] == "completed")
        self.assertEqual((self.store.root / "jobs/long/stdout").read_text(), "begin\nend\n")
        self.assertGreater(self.store.status("test-session")["idle_remaining_seconds"], 1.5)
        self.until(lambda: self.store.status("test-session")["phase"] == "closed")
        self.assertEqual(self.store.status("test-session")["reason"], "idle-timeout")

    def test_cancel_terminates_the_process_group(self):
        self.submit("cancel", [sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"])
        self.until(lambda: self.store.job("test-session", "cancel")["status"] == "running")
        pid = self.store.job("test-session", "cancel")["pid"]
        self.store.cancel("test-session", "cancel")
        self.until(lambda: self.store.job("test-session", "cancel")["status"] == "cancelled")
        self.assertFalse(worker.group_alive(pid))

    def test_polling_empty_worker_does_not_keep_it_alive(self):
        self.until(lambda: self.store.status("test-session")["phase"] == "closed", timeout=5)
        self.assertEqual(self.store.status("test-session")["reason"], "idle-timeout")

    def test_background_descendant_counts_as_running_command(self):
        self.submit("descendant", ["bash", "-c", "sleep 3 &"])
        self.until(lambda: self.store.job("test-session", "descendant")["status"] == "running")
        time.sleep(2.2)
        self.assertEqual(self.store.status("test-session")["phase"], "open")
        self.assertEqual(self.store.job("test-session", "descendant")["status"], "running")
        self.until(lambda: self.store.job("test-session", "descendant")["status"] == "completed")

    def test_stream_watch_delivers_output_and_terminal_frame_without_idle_keepalive(self):
        self.submit("watch", [sys.executable, "-u", "-c", "import time; print('first',flush=True); time.sleep(0.7); print('last')"])
        output = io.BytesIO()
        worker.rpc(self.store, "test-session", io.BytesIO(b'{"session":"test-session","op":"watch","job":"watch"}\n'), output)
        frames = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
        self.assertEqual(frames[-1]["status"], "completed")
        self.assertEqual(frames[-1]["code"], 0)
        import base64
        self.assertEqual(b"".join(base64.b64decode(x["stdout"]) for x in frames), b"first\nlast\n")
        before = self.store.status("test-session")["activity"]
        worker.rpc(self.store, "test-session", io.BytesIO(b'{"session":"test-session","op":"watch","job":"watch"}\n'), io.BytesIO())
        self.assertEqual(self.store.status("test-session")["activity"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
