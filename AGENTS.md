# dev-agent

Ephemeral development compute. The agent and model credentials stay on the client.

- A session owns one GitHub job/VM and one SSH keypair. Never share workers
  automatically between sessions. Do not treat worker files as durable storage.
- Expire after 900 seconds without real activity AND without queued/running jobs
  or an active transfer. Status/log polling must not renew that timer.
- Keep payloads, commands, output and personal credentials out of public Actions
  logs and artifacts. Public connection metadata contains only public information.
- Pin the SSH host key to metadata from the exact expected repository/run/SHA.
  Never disable host verification or forward an SSH agent.
- An uncertain submission must be reconciled by its job identifier. Never start
  an equivalent command on another worker as an automatic transport retry.
- Validate process lifecycle, cross-session isolation, path containment and idle
  expiry with real behavior. Tests: `uv run -s tests/test_worker.py`.
- Standalone Python scripts have PEP 723 metadata and adjacent uv lockfiles.
- Commits created by Codex carry `Codex-Session`; include `T3-Thread` in T3.

