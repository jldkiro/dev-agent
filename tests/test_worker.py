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


if __name__ == "__main__":
    unittest.main(verbosity=2)
