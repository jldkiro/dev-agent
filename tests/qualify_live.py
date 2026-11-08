# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Opt-in live qualification: two real workers, including the full 15-minute expiry."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dev_agent as client


def session_for(owner):
    before = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = owner
    try:
        return client.Session()
    finally:
        if before is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = before


def cli(owner, *args, timeout=180, expected=0):
    env = os.environ.copy()
    env["CODEX_THREAD_ID"] = owner
    result = subprocess.run([sys.executable, str(ROOT / "dev_agent.py"), *args], env=env, capture_output=True, timeout=timeout)
    if result.returncode != expected:
        raise AssertionError(f"CLI {args[0]} exit {result.returncode}; stderr: {result.stderr.decode(errors='replace')[-2500:]}; stdout: {result.stdout.decode(errors='replace')[-2000:]}")
    return result


def submit(owner, source):
    result = cli(owner, "run", "--project", str(ROOT), "--detach", "--", "python3", "-u", "-c", source)
    return json.loads(result.stdout)


def collect(session, record, expected=0, timeout=90):
    offsets = {"stdout_offset": 0, "stderr_offset": 0}
    content = {"stdout": b"", "stderr": b""}
    streaming = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = session.rpc({"op": "poll", "job": record["job"], **offsets})
        for name in content:
            data = base64.b64decode(status[name])
            if data and status["status"] == "running":
                streaming = True
            content[name] += data
            offsets[name + "_offset"] = status[name + "_offset"]
        if status["status"] in client.FINISHED and status["stdout_eof"] and status["stderr_eof"]:
            assert status["code"] == expected, {"status": status, "stderr": content["stderr"].decode(errors="replace")}
            return status, content, streaming
        time.sleep(0.5)
    raise TimeoutError("Job did not finish within qualification timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-owner", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    actual_owner = os.environ["CODEX_THREAD_ID"]
    main_session = session_for(actual_owner)
    idle_session = session_for(args.idle_owner)
    receipt_path = Path(args.receipt).resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    proof = {"date": "2026-11-08", "tests": [], "complete": False}
    def record(name, detail=True):
        proof["tests"].append({"test": name, "detail": detail, "at": time.time()})
        client.write_json(receipt_path, proof)
        print(json.dumps({"test": name, "detail": detail}), flush=True)
    try:
        main_state = main_session.ensure()
        idle_state = idle_session.ensure()
        assert main_state["run_id"] != idle_state["run_id"]
        assert main_state["connection"]["host_key"] != idle_state["connection"]["host_key"]
        proof["runs"] = [{k: s[k] for k in ("session_id", "request_id", "run_id", "run_url", "sha")} for s in (main_state, idle_state)]
        record("separate_workers_and_host_keys", [s["run_url"] for s in (main_state, idle_state)])

        source = "import sys,time; print('stream-first',flush=True); print('separate-error',file=sys.stderr,flush=True); time.sleep(3); print('stream-last',flush=True); sys.exit(42)"
        job = submit(actual_owner, source)
        _, output, streaming = collect(main_session, job, 42)
        assert output["stdout"] == b"stream-first\nstream-last\n" and output["stderr"] == b"separate-error\n"
        assert streaming
        record("stdout_stderr_streaming_exit_code")

        source = "import pathlib,hashlib,os,socket,subprocess,json; data=bytes(range(256))*1024; pathlib.Path('artifact.bin').write_bytes(data); assert not any(k.startswith(('GITHUB_','GH_','OPENAI_','ANTHROPIC_','CODEX_')) for k in os.environ); print(json.dumps({'hostname':socket.gethostname(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'sha256':hashlib.sha256(data).hexdigest(),'uv':subprocess.check_output(['uv','--version']).decode().strip(),'docker':subprocess.check_output(['docker','version','--format','{{.Server.Version}}']).decode().strip()}))"
        artifact_job = submit(actual_owner, source)
        _, output, _ = collect(main_session, artifact_job)
        environment = json.loads(output["stdout"])
        record("sanitized_environment_uv_and_docker", environment)
        with tempfile.TemporaryDirectory(prefix="dev-agent-download-") as temporary:
            target = Path(temporary) / "artifact.bin"
            cli(actual_owner, "get", "--job", artifact_job["job"], "artifact.bin", str(target))
            assert target.read_bytes() == bytes(range(256)) * 1024
            cli(actual_owner, "get", "--job", artifact_job["job"], "artifact.bin", str(target), expected=1)
        record("binary_download_hash_and_overwrite_guard", {"bytes": 262144, "sha256": environment["sha256"]})

        main_workspace = artifact_job["workspace"]
        isolated = submit(args.idle_owner, f"import pathlib,socket,json; assert not pathlib.Path('/tmp/dev-agent/state/workspaces/{main_workspace}').exists(); print(json.dumps({{'hostname':socket.gethostname(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}}))")
        _, output, _ = collect(idle_session, isolated)
        other = json.loads(output["stdout"])
        # Hostnames can be cloned from the runner image: they are not worker IDs.
        record("separate_runs_host_keys_and_filesystems", {"main": environment["hostname"], "other": other["hostname"], "main_boot_id": environment["boot_id"], "other_boot_id": other["boot_id"]})

        wrong_config = main_session.root / "wrong-session-test.conf"
        original = (main_session.root / main_state["request_id"] / "ssh_config").read_text()
        wrong = original.replace((main_session.root / main_state["request_id"] / "identity").as_posix(), (idle_session.root / idle_state["request_id"] / "identity").as_posix())
        wrong_config.write_bytes(wrong.encode())
        bad = client.command(["ssh", "-F", str(wrong_config), "dev-agent-worker", "rpc"], data=b'{"op":"status"}\n', check=False)
        assert bad.returncode == 255 and b"Permission denied" in bad.stderr
        wrong_config.unlink()
        record("cross_session_private_key_rejected")

        cancelled = submit(actual_owner, "import subprocess,time; subprocess.Popen(['sleep','120']); print('cancel-started',flush=True); time.sleep(120)")
        deadline = time.monotonic() + 20
        while main_session.rpc({"op": "poll", "job": cancelled["job"]})["status"] != "running":
            assert time.monotonic() < deadline
            time.sleep(0.5)
        cli(actual_owner, "cancel", cancelled["job"])
        cancelled_status, _, _ = collect(main_session, cancelled, 130)
        assert cancelled_status["status"] == "cancelled"
        record("detached_process_group_cancelled")

        # Real remote test suite, not just local/static validation.
        full_tests = json.loads(cli(actual_owner, "run", "--project", str(ROOT), "--detach", "--", "uv", "run", "-s", "tests/test_worker.py").stdout)
        _, suite_output, _ = collect(main_session, full_tests, timeout=120)
        assert b"OK" in suite_output["stderr"]
        record("remote_linux_test_suite", suite_output["stderr"].decode()[-1500:])

        # Refresh the idle session with one final short command; after that only
        # passive reads are allowed. A separate real command runs beyond 900s.
        idle_last = submit(args.idle_owner, "print('last-real-activity')")
        collect(idle_session, idle_last)
        idle_initial = idle_session.rpc({"op": "status"})
        assert idle_initial["idle"] == 900
        idle_local_start = time.monotonic()
        long_job = submit(actual_owner, "import time,pathlib; print('long-job-started',flush=True); time.sleep(925); pathlib.Path('long-result.txt').write_text('completed after the idle limit'); print('long-job-finished',flush=True)")
        proof["long_job"] = {"job": long_job["job"], "run_url": long_job["run_url"]}
        record("real_15_minute_test_started", {"idle_run": idle_state["run_url"], "busy_run": main_state["run_url"], "command_sleep_seconds": 925})
        busy_beyond_limit = False
        idle_expired = False
        end = time.monotonic() + 1030
        while time.monotonic() < end:
            elapsed = time.monotonic() - idle_local_start
            busy = main_session.rpc({"op": "poll", "job": long_job["job"]})
            idle_run = client.gh(f"repos/{client.REPOSITORY}/actions/runs/{idle_state['run_id']}")
            if elapsed >= 902 and busy["status"] == "running" and not busy_beyond_limit:
                busy_beyond_limit = True
                record("command_still_running_after_15_minutes", {"elapsed_seconds": round(elapsed, 1), "status": busy["status"]})
            if idle_run["status"] == "completed":
                assert idle_run["conclusion"] == "success"
                assert elapsed >= 880, elapsed
                if not idle_expired:
                    idle_expired = True
                    record("idle_worker_expired_with_passive_polling", {"elapsed_seconds": round(elapsed, 1), "conclusion": idle_run["conclusion"]})
            else:
                try:
                    current = idle_session.rpc({"op": "status"})
                    assert current["activity"] == idle_initial["activity"], "Passive polling renewed idle timer"
                except RuntimeError:
                    if elapsed < 890:
                        raise
            print(json.dumps({"progress_seconds": round(elapsed, 1), "busy": busy["status"], "idle_workflow": idle_run["status"]}), flush=True)
            if busy["status"] in client.FINISHED:
                assert busy["status"] == "completed" and busy["code"] == 0
                assert busy_beyond_limit and idle_expired
                fresh_idle = main_session.rpc({"op": "status"})
                assert fresh_idle["idle_remaining_seconds"] > 850
                record("busy_completion_resets_full_idle_period", {"idle_remaining_seconds": fresh_idle["idle_remaining_seconds"]})
                break
            time.sleep(10 if elapsed > 870 else 30)
        else:
            raise TimeoutError("Live lifetime qualification did not complete")
        proof["complete"] = True
    finally:
        cleanup = []
        for owner in (args.idle_owner, actual_owner):
            try:
                result = cli(owner, "stop", "--force", timeout=90)
                cleanup.append(json.loads(result.stdout))
            except Exception as e:
                cleanup.append({"error": str(e)})
        proof["cleanup"] = cleanup
        client.write_json(receipt_path, proof)
        print(json.dumps({"complete": proof["complete"], "cleanup": cleanup}), flush=True)


if __name__ == "__main__":
    main()
