# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Opt-in measured cold allocations, warm commands, transfers and log canaries."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dev_agent as client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--project", help="Additional representative project upload")
    args = parser.parse_args()
    receipt = Path(args.receipt).resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    result = {"label": args.label, "date": "2026-11-08", "trials": [], "complete": False}
    for index in range(args.trials):
        owner = "dev-agent-benchmark-" + uuid.uuid4().hex
        os.environ["CODEX_THREAD_ID"] = owner
        session = client.Session()
        trace_file = session.root / "benchmark-trace.jsonl"
        os.environ["DEV_AGENT_TRACE"] = str(trace_file)
        trial = {"trial": index + 1, "warm_command_seconds": [], "rpc_seconds": []}
        result["trials"].append(trial)
        canaries = ["PRIVATE_" + kind + "_" + uuid.uuid4().hex for kind in ("OUT", "ERR", "FILE", "ARG")]
        with tempfile.TemporaryDirectory(prefix="dev-agent-benchmark-") as temporary:
            project = Path(temporary)
            (project / "input.txt").write_text(canaries[2])
            program = "import pathlib,sys,time; print(sys.argv[1]); print(sys.argv[2],file=sys.stderr); print(pathlib.Path('input.txt').read_text()); print(sys.argv[3]); pathlib.Path('result.txt').write_text('download-ok')"
            def cli(*arguments, expected=0):
                begin = time.perf_counter()
                done = subprocess.run([sys.executable, str(ROOT / "dev_agent.py"), *arguments], capture_output=True, timeout=420)
                elapsed = time.perf_counter() - begin
                if done.returncode != expected:
                    raise RuntimeError("Benchmark CLI failed: " + done.stderr.decode(errors="replace")[-1600:])
                return elapsed, done
            try:
                begin = time.perf_counter()
                state = session.ensure()
                if trace_file.exists():
                    trial["startup_events"] = [json.loads(x) for x in trace_file.read_text().splitlines()]
                trial["startup_seconds"] = round(time.perf_counter() - begin, 4)
                trial["run_url"] = state["run_url"]
                trial["run_id"] = state["run_id"]
                trial["sha"] = state["sha"]
                print(json.dumps({"label": args.label, "trial": index + 1, "startup_seconds": trial["startup_seconds"], "run_url": state["run_url"]}), flush=True)
                for _ in range(3):
                    begin = time.perf_counter()
                    session.rpc({"op": "status"})
                    trial["rpc_seconds"].append(round(time.perf_counter() - begin, 4))
                for _ in range(3):
                    elapsed, done = cli("run", "--project", str(project), "--", "python3", "-u", "-c", program, canaries[0], canaries[1], canaries[3])
                    assert canaries[0].encode() in done.stdout and canaries[1].encode() in done.stderr and canaries[2].encode() in done.stdout
                    trial["warm_command_seconds"].append(round(elapsed, 4))
                current = session.read()
                record_paths = list((session.root / "jobs").glob("*.json"))
                latest = max(record_paths, key=lambda p: p.stat().st_mtime)
                job = json.loads(latest.read_bytes())
                elapsed, _ = cli("get", "--job", job["job"], "result.txt", str(project / "downloaded.txt"))
                assert (project / "downloaded.txt").read_text() == "download-ok"
                trial["download_seconds"] = round(elapsed, 4)
                if args.project and index == 0:
                    selected = Path(args.project).resolve()
                    archive = project / "representative.tar"
                    begin = time.perf_counter()
                    files, size = client.project_archive(selected, archive, compressed="gzip" in state["connection"].get("capabilities", []))
                    trial["project_pack_seconds"] = round(time.perf_counter() - begin, 4)
                    trial["project_files"] = files
                    trial["project_bytes"] = size
                    trial["project_wire_bytes"] = archive.stat().st_size
                    with archive.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                    begin = time.perf_counter()
                    session.rpc({"op": "sync", "workspace": uuid.uuid4().hex, "bytes": archive.stat().st_size, "sha256": digest}, upload=archive)
                    trial["project_upload_seconds"] = round(time.perf_counter() - begin, 4)
                    if "source-cache" in state["connection"].get("capabilities", []):
                        begin = time.perf_counter()
                        reused = session.rpc({"op": "prepare", "workspace": uuid.uuid4().hex, "sha256": digest})
                        assert reused["reused"]
                        trial["project_cached_snapshot_seconds"] = round(time.perf_counter() - begin, 4)
            except Exception as error:
                trial["failed"] = True
                trial["error_type"] = type(error).__name__
                raise
            finally:
                elapsed, done = cli("stop", "--force")
                trial["stop_seconds"] = round(elapsed, 4)
                trial["cleanup"] = json.loads(done.stdout)
                if trace_file.exists():
                    trial["timing_events"] = [json.loads(x) for x in trace_file.read_text().splitlines()]
                client.write_json(receipt, result)
            if trial.get("run_id"):
                run_id = trial["run_id"]
                jobs = client.gh(f"repos/{client.REPOSITORY}/actions/runs/{run_id}/jobs")["jobs"]
                trial["github_steps"] = [{k: step.get(k) for k in ("name", "started_at", "completed_at", "conclusion")} for job_data in jobs for step in job_data["steps"]]
                logs = client.gh(f"repos/{client.REPOSITORY}/actions/runs/{run_id}/logs")
                import io, zipfile
                with zipfile.ZipFile(io.BytesIO(logs)) as archive:
                    raw = b"\n".join(archive.read(name) for name in archive.namelist() if not name.endswith("/"))
                assert not any(c.encode() in raw for c in canaries), "Private command canary leaked into public Actions logs"
                trial["log_bytes"] = len(raw)
                trial["log_lines"] = len(raw.splitlines())
                trial["private_canaries_absent"] = True
                trial["canary_hashes"] = [hashlib.sha256(c.encode()).hexdigest() for c in canaries]
                # Store only sizes and hashes, never raw logs or canary plaintext.
            client.write_json(receipt, result)
            print(json.dumps({"label": args.label, "trial": index + 1, "warm_seconds": trial["warm_command_seconds"], "private_canaries_absent": trial.get("private_canaries_absent"), "log_lines": trial.get("log_lines")}), flush=True)
    result["complete"] = True
    result["summary"] = {"startup_median_seconds": statistics.median(x["startup_seconds"] for x in result["trials"]), "warm_command_median_seconds": statistics.median(y for x in result["trials"] for y in x["warm_command_seconds"]), "rpc_median_seconds": statistics.median(y for x in result["trials"] for y in x["rpc_seconds"])}
    client.write_json(receipt, result)
    print(json.dumps(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
