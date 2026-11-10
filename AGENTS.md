# dev-agent

Ephemeral development compute. The agent and model credentials stay on the client.

- A session owns one GitHub job/VM and one SSH keypair. Never share workers
  automatically between sessions. Do not treat worker files as durable storage.
- Public workers expire after 3600 seconds, private after 300, without real activity AND without queued/running jobs
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
- Private checkpoints use two independent root commits per session, capped at
  50 MiB each. Verify repository privacy before every restore/save/prune. Never
  save credentials, logs, processes or dependency trees. Preserve remote edits
  across snapshots and fail explicitly on conflicting local edits.
- Commits created by Codex carry `Codex-Session`; include `T3-Thread` in T3.

