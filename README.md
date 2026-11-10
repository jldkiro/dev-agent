# dev-agent

Run development commands on a temporary GitHub-hosted Linux worker while your
coding agent, conversation and model credentials stay on your own computer.
Transport: **OpenSSH over a public Cloudflare Quick Tunnel**, without T3 Connect,
a Cloudflare account, a VPN or transferring your private client key to the worker.
The worker generates its own temporary SSH host key.

Each client session/profile gets its **own VM and SSH keypair**. Public workers
close after **one hour** without real activity; private workers after **five
minutes**, only without active commands/transfers. Passive status and log polling
never prolong their lifetime. Private workers checkpoint admissible files before
normal retirement and restore them for the same session. VMs and processes remain
ephemeral; retrieve important results explicitly.

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
`--profile ha-mcp` uses the ordinary personal gh profile and the public
`julienld/ha-mcp-dev` administration branch `dev-agent-worker`. This branch is the
fork's default so GitHub can register/dispatch its workflow; `master` remains a
clean upstream mirror and the explicit base for contributions. `--profile public`
uses this engine repository; `--profile private` uses the independent private
copy `jldkiro/dev-agent-private`. Both latter profiles use the dedicated gh login.
There is no fallback between repositories or visibilities. Profile and repository
are part of the session identity. `DEV_AGENT_PROFILE` can select a profile too.
The worker workflow is restricted to its repository owner.

```text
dev-agent --profile ha-mcp run --project C:\path\to\project -- uv run pytest
dev-agent --profile private run --project C:\path\to\private-project --detach -- python long_check.py
dev-agent --profile private status
dev-agent --profile private status --job JOB_ID
dev-agent --profile private wait JOB_ID
dev-agent --profile private logs JOB_ID --follow
dev-agent --profile private cancel JOB_ID
dev-agent --profile private get --job JOB_ID relative/result.json C:\path\to\result.json
dev-agent --profile private stop
```

`run` starts the session's worker when needed, uploads the selected project's
current files, and returns the remote command's exit code. `--detach` returns a
job identifier immediately. Each command receives a fresh workspace;
dependencies can reuse the worker's uv cache. Local uncommitted files are
included; there is no commit or source checkout requirement for the payload.

Upload, submission and streamed output share one SSH connection per `run`.
Snapshots use deterministic gzip at level 1. An identical snapshot already on
this worker is verified by SHA-256 and extracted into a fresh workspace without
uploading it again. Local files are reread to detect changes. Public snapshots do
not reuse previously modified workspaces as source. Private reconciliation and
restored synchronization bases are described below.

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
   Before publishing it, the worker tests SSH through the actual public tunnel.
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

## Private checkpoints

The common engine implements persistence only for the private deployment. Its
supervisor uses the job's ephemeral `GITHUB_TOKEN`, restricted to that repository,
kept in a root-only file. It is absent from command environments and checkpoints.
Before each restore, save or prune, the GitHub API must confirm the exact repository
is private. A visibility or network error fails closed; public workers never save
or restore payloads. Docker/root commands can access their own VM; these are not
untrusted tenants contained from the supervisor.
All sessions using this private repository share its GitHub authorization
boundary: the job token can access other checkpoint branches in that repository.
Session namespacing prevents automatic mixing; it is not cryptographic access
control between mutually untrusted agents.

After the controller closes and all jobs/transfers are finished, the supervisor
saves `checkpoint.tar.gz` and `manifest.json`. The compressed archive is capped at
50 MiB, expanded contents at 512 MiB. It contains the latest workspace of each
explicit project plus its synchronization base, with SHA-256 verification. Keys,
common credential files, logs, `.git`, dependency trees and caches are excluded.
Filters do not detect every secret embedded in arbitrary source files: select
only the intended private project. Nothing from the agent's ambient context or
home directory is added. Dependencies must be reconstructed after allocation.

`state/<session>/current` and `state/<session>/previous` each point to an independent
root commit, with no parent. Publication rotates both references atomically, with
leases against concurrent updates. An uncertain push is reconciled by its exact
commit, never replayed. The previous good generation remains until the new one is
confirmed; restore can fall back to it if the current generation is corrupt.
References for other sessions older than seven days are pruned during later saves
(up to twenty deletions per pass). No permanent timer is installed. GitHub controls
physical collection of unreachable objects; this is not a disk-purge deadline.

On a new VM for the same profile/session, files are restored into fresh controller
state. Old jobs, output streams, processes, SSH keys and monotonic timestamps are
not restored. The next command combines current local files with remote file
changes against their previous upload base. Conflicting edits fail explicitly;
the local checkout is never overwritten. Only one command per private project
can be active while another snapshot is prepared. A different conversation does
not automatically inherit another session's checkpoint.

A failed save is reported as a failed workflow with a fixed public message; its
detailed diagnostics remain on the VM. Abrupt VM loss or hard Actions cancellation
can prevent a final checkpoint. If a previous private run with uploaded work did
not finish successfully, the client requires `--accept-stale-checkpoint` before
allocating again. That flag explicitly accepts recovery from an older checkpoint;
agents must obtain a decision rather than silently using it. A `stop` phase of
`retired` describes VM/key cleanup, not a guarantee that a checkpoint succeeded.

The private copy is independent, not a GitHub fork of a public repository. Thin
deployment workflows pin the public common engine by full SHA. Run `deploy.py`
from the public engine checkout, whose workflow provides the template. To preview/update
only a deployment workflow (without pushing checkpoint branches):

```text
uv run -s deploy.py --profile private --engine FULL_COMMIT_SHA
uv run -s deploy.py --profile private --engine FULL_COMMIT_SHA --apply --t3-thread THREAD_ID
```

Use `--profile ha-mcp` to update the fork's administration workflow. The script
verifies visibility, the administration/default branch and the exact observed
file SHA before writing; no mirror push or automatic private-to-public sync occurs.

### Profile qualification, 2026-11-09

[Receipt](verification/profiles-2026-11-09.json). The public ha-mcp fork executed a
real Python 3.13 compilation. A private worker expired at its configured five
minutes (observed completed with its checkpoint after 315.285 s). New allocations
restored remote edits, preserved a new local file and rejected conflicting edits
before submission. Git references were inspected as independent roots. A final
round trip used common engine `690750f65a3578759e94eed6c42028d04c1ee31e`; the public
profile also verified the absence of checkpoint credentials and policy files.

Linux CI passes 43 tests; Windows passes 38 and skips the five Linux process
controller tests. The receipt retains resolved findings and cleanup results.
Private fixture contents and detailed private run identifiers stay out of this
public receipt. Job keys, connection artifacts and synthetic checkpoint references
are retired after qualification. No one-hour wall-clock expiry test is claimed:
the public 3600-second policy was checked in the live authenticated metadata,
with the same controller's expiry/active-job behavior covered by lifecycle tests.

## Uncertain outcomes

Allocation and command identifiers are saved before sending a request. If a
connection fails after submission, the client looks up that exact job. It never
automatically repeats the command on another worker. Keep the reported job ID
and use `status --job` / `wait` to reconcile. A worker destroyed before its result
was retrieved means the result may be lost; the tool does not label that as
successful execution or silently reconstruct the work.

Only read-only GitHub API requests can be retried once, with an eight-second
timeout per attempt. Allocation and command writes are never replayed as a
transport retry. Startup remains dependent on GitHub's queue, API and Cloudflare.

## Public logs

Project files, command arguments, stdout and stderr travel over SSH. They are
neither Actions workflow inputs nor uploaded artifacts. Command logs stay on the
disposable worker and are available through `dev-agent logs JOB` / `wait JOB`.
Detailed bootstrap, SSH and tunnel diagnostics are also kept on the worker;
failure handling never prints their contents to Actions.

Application messages in Actions are fixed lifecycle markers (`STARTING`,
`READY`, an allowlisted shutdown reason, or a fixed failure stage). GitHub still
adds its own runner, step and public-metadata artifact messages. This is a
minimal public operational log, not an empty run log. The provider operates the
VM; this mechanism does not conceal workloads from GitHub itself or constrain
a command with Docker/root access from intentionally disclosing its own data.

## Validation and maintenance

```text
uv run -s tests/test_worker.py
```

The portable storage/security tests run on Windows. The Linux controller tests
also verify expiry, active-command protection, process-group cancellation and
surviving background descendants. The Actions test workflow runs all of them.
Production idle timeouts are 3600 seconds public and 300 seconds private; the
shorter controller option exists for isolated lifecycle tests only.

The runtime is a bounded per-job process; no permanent client service, cron,
credential forwarding or heartbeat keepalive is installed. Inspect the GitHub
run if allocation or worker health fails. Disabling `worker.yml` stops new
allocations; cancel active runs to retire them. Uninstall the local launcher and
skill to remove client integration without deleting project files.

### Live qualification, 2026-11-08

[Receipt](verification/live-2026-11-08.json): two independent GitHub allocations,
different SSH host keys and Linux boot IDs, cross-session key rejection and
isolated workspaces. The idle allocation finished successfully at the check
905.8 seconds after its last activity, while a 925-second command remained
running on the other allocation. Completion reset that worker's idle period.
Both allocations were retired and their client keys and public connection
artifacts removed. Other checks cover separate stdout/stderr, streaming, exit
code 42, binary checksum and overwrite protection, Docker/uv and process-group
cancellation. This qualifies the Windows client to GitHub Linux path; it does
not claim multi-hour reliability or compatibility with every compute provider.

### Performance and public-log qualification, 2026-11-08

[Measurements, revisions and cleanup](verification/performance-2026-11-08.json).
Each before/after series used two fresh workers and three tiny CLI commands per
worker. Cold startup ends at a verified ready worker and excludes project upload;
warm command time includes synchronization, command execution and streamed output.

| Measurement | Before | Final version |
| --- | --- | --- |
| Cold startup, median (2 workers) | 24.6 s | 22.0 s |
| Cold startup, observed range | 20.6–28.7 s | 21.9–22.1 s |
| Tiny command, median (6 commands) | 8.2 s | 3.8 s |
| Tiny command, observed range | 6.0–11.8 s | 3.5–4.6 s |
| Aggregate public job log lines per run | 169 | 72 |

The command median improved by about 53%. Startup varies with the queue, API,
DNS and tunnel: earlier optimized trials ranged from 17.7 to 48.0 seconds, so
two final samples do not establish a startup guarantee. A 31-second GitHub read
motivated the bounded read timeouts; a fresh-hostname DNS failure motivated the
public SSH readiness probe. Failed attempts remain recorded in the receipt.

For a representative 1,224-file project (38.7 MB of contents), gzip reduced
the transmitted archive to 21.1 MB. Optimized packaging took 3.7–5.4 seconds in
later runs, versus 30.6 seconds in the first baseline run; filesystem cache
warmth also affects that comparison. First uploads varied from 6.7 to 61.3
seconds; a verified cached snapshot took about 3.6 seconds to prepare through a
standalone SSH call. These are component timings, not a claim that a full-size
project command completes in 3.8 seconds.

An end-to-end check on the final revision compiled the actual `src/ha_mcp`
snapshot with Python 3.13: the first CLI call took 15.0 seconds (including the
initial project upload and Python installation), and the second took 6.6 seconds
with the same source cached. Compilation itself took about 0.4 seconds each
time. These two calls exclude worker startup and are a compilation check, not
the complete functional test suite.

Fresh random markers in arguments, stdout, stderr and a project file were
absent from all complete benchmark runs' public log archives. A separate live
check verified 700 KB of stdout, 300 KB of stderr, exit code 42 and concurrent
wait/cancel with exit code 130. Linux CI passes all 30 tests; Windows passes 25
and skips the five Linux controller tests.

## GitHub Free capacity

Checked on 2026-11-08: standard runners in public repositories have no monthly
compute-minute quota. The selected public Ubuntu 24.04 runner provides 4 vCPUs,
16 GB RAM and 14 GB SSD. Free allows up to 20 concurrent standard jobs across
the account, shared with its other Actions work. One session uses one job; this
does not combine their memory into one machine. GitHub's hard job limit is six
hours; this controller begins closing after 5 h 50 min, or at its profile's idle timeout.
See [runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[billing](https://docs.github.com/en/actions/concepts/billing-and-usage) and
[limits](https://docs.github.com/en/actions/reference/limits).

The Free artifact allowance is 500 MB and the repository cache allowance is
10 GB. This tool uploads only small public connection metadata and deletes it
on retirement; source/output caches live solely on the worker, not in public
Actions caches. The client also shares the authenticated user's 5,000 REST
requests/hour [API budget](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).
Capacity remains subject to [Actions usage terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
and availability. Those terms require hosted-runner activities to relate to
production, testing, deployment or publication of the software project associated
with the repository running Actions. Technical support for uploading another
project does not establish permission to use a public controller repository as
general-purpose free compute. For routine development of another project, prefer
hosting the workflow in that project's repository/fork and keeping this repository
as the execution tool. The live cross-project checks here qualify this tool's
snapshot transport and execution behavior.

