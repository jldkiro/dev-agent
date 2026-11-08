# dev-agent

Run development commands on a temporary GitHub-hosted Linux worker while your
coding agent, conversation and model credentials stay on your own computer.
Transport: **OpenSSH over a public Cloudflare Quick Tunnel**, without T3 Connect,
a Cloudflare account, a VPN or a private key on the worker.

Each client session gets its **own VM and SSH keypair**. A worker closes after
**15 minutes without real activity and without active commands/transfers**.
Passive status and log polling never prolong its lifetime. All remote files are
ephemeral: explicitly download results before closing the worker.

## Use

Requirements on the client: Python 3.12+, uv, GitHub CLI, OpenSSH and cloudflared.
`CODEX_THREAD_ID` (or `T3_THREAD_ID`) identifies the caller's session. Workers are
never pooled across sessions. The Windows setup installs a `dev-agent` launcher;
the portable form is `uv run -s /path/to/dev-agent/dev_agent.py ...`.

Run `uv run -s install.py` from this checkout to install the thin launcher and a
Codex skill linked to `skills/dev-agent`. It refuses to replace unrelated files.

Authenticate GitHub CLI in a dedicated profile. On Windows this installation
uses `%LOCALAPPDATA%/JLDIAOS/dev-agent/gh`, separate from the ordinary gh profile.
The client honors `DEV_AGENT_GH_CONFIG_DIR` and `DEV_AGENT_STATE_DIR` when needed.
The worker workflow is restricted to its repository owner.

```text
dev-agent run --project C:\path\to\project -- uv run pytest
dev-agent run --project C:\path\to\project --detach -- python long_check.py
dev-agent status
dev-agent status --job JOB_ID
dev-agent wait JOB_ID
dev-agent logs JOB_ID --follow
dev-agent cancel JOB_ID
dev-agent get --job JOB_ID relative/result.json C:\path\to\result.json
dev-agent stop
```

`run` starts the session's worker when needed, uploads the selected project's
current files, and returns the remote command's exit code. `--detach` returns a
job identifier immediately. Each command receives a fresh uploaded workspace;
dependencies can reuse the worker's uv cache. Local uncommitted files are
included; there is no commit or source checkout requirement for the payload.

The selected project must be explicit. Git ignore rules and additional filters
exclude common credentials, `.env` files, private-key formats, `.venv`, caches and
`node_modules`. These filters are not a complete secret detector: send only the
project needed by the command, never your home/configuration/personal archive.
Commands and their stdout/stderr travel over SSH, not public Actions inputs or
logs. Results do not automatically modify your local checkout.

`get` refuses to overwrite an existing destination unless `--overwrite` is
explicit. A job's files belong to its original worker and cannot be recovered
from a replacement worker. Save useful outputs locally before retirement.

`stop` refuses to close a worker with active commands. Cancel them first, or use
`stop --force` when intentionally discarding that work. GitHub also imposes a
six-hour job limit; the controller starts shutdown before that hard ceiling.
The laptop must stay available for the agent to continue reasoning. An already
submitted remote command runs independently of its SSH client connection.

## Connection and lifecycle

1. A local allocation lock prevents duplicate workers for the same session.
2. A recorded request ID correlates `workflow_dispatch` with its exact run.
3. A public artifact supplies only the run/session IDs, revision, temporary URL
   and server public key. The client checks all of them and pins that SSH key.
4. The SSH server accepts only the session's public key and forces a small RPC
   handler. Password login, forwarding, TTY and agent forwarding are disabled.
5. A controller tracks queued/running process groups, separate stdout/stderr,
   cancellation and real activity. Transfers have expiring activity leases.
6. Idle shutdown ends the GitHub job and destroys the worker. Client retirement
   deletes the local private key and the connection artifact when it can.

Public connection artifacts expire after one day if the client never returns to
retire them; they contain no credentials or source files. A public endpoint is
not an unauthenticated shell: access still requires the private key kept on the
client. The public URL changes on every allocation.

The worker contains no model login or personal GitHub token. Commands receive a
small explicit environment. Docker access is intentionally enabled for isolated
development jobs; it allows root-equivalent access within that disposable VM.
This is isolation between sessions, not containment of an untrusted command from
its own worker. Do not use the service for production, durable storage, private
LAN access, or workloads outside the hosting provider's permitted development
usage. Source code, runtime and action revisions are pinned to the workflow run.

## Uncertain outcomes

Allocation and command identifiers are saved before sending a request. If a
connection fails after submission, the client looks up that exact job. It never
automatically repeats the command on another worker. Keep the reported job ID
and use `status --job` / `wait` to reconcile. A worker destroyed before its result
was retrieved means the result may be lost; the tool does not label that as
successful execution or silently reconstruct the work.

## Validation and maintenance

```text
uv run -s tests/test_worker.py
```

The portable storage/security tests run on Windows. The Linux controller tests
also verify expiry, active-command protection, process-group cancellation and
surviving background descendants. The Actions test workflow runs all of them.
Production idle timeout is fixed at 900 seconds in the worker workflow; the
shorter controller option exists for isolated lifecycle tests only.

The runtime is a bounded per-job process; no permanent client service, cron,
credential forwarding or heartbeat keepalive is installed. Inspect the GitHub
run if allocation or worker health fails. Disabling `worker.yml` stops new
allocations; cancel active runs to retire them. Uninstall the local launcher and
skill to remove client integration without deleting project files.

