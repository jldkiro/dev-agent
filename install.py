# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install a thin local launcher and a discoverable Codex skill; no daemon."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parent


def main():
    if os.name == "nt":
        bin_dir = Path.home() / "code/bin"
        launcher = bin_dir / "dev-agent.cmd"
        contents = f'@echo off\nuv run -s "{ROOT / "dev_agent.py"}" %*\n'.encode()
    else:
        bin_dir = Path.home() / ".local/bin"
        launcher = bin_dir / "dev-agent"
        contents = ("#!/bin/sh\nexec uv run -s " + shlex.quote(str(ROOT / "dev_agent.py")) + ' "$@"\n').encode()
    bin_dir.mkdir(parents=True, exist_ok=True)
    if launcher.exists() and launcher.read_bytes().replace(b"\r\n", b"\n") != contents:
        raise RuntimeError("An unrelated launcher already exists: " + str(launcher))
    launcher.write_bytes(contents)
    if os.name != "nt":
        launcher.chmod(0o755)

    skill = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills/dev-agent"
    source = ROOT / "skills/dev-agent"
    skill.parent.mkdir(parents=True, exist_ok=True)
    if skill.exists():
        if skill.resolve() != source.resolve():
            raise RuntimeError("An unrelated skill already exists: " + str(skill))
    elif os.name == "nt":
        quoted = lambda p: "'" + str(p).replace("'", "''") + "'"
        script = "New-Item -ItemType Junction -Path " + quoted(skill) + " -Target " + quoted(source) + " | Out-Null"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True)
    else:
        skill.symlink_to(source, target_is_directory=True)
    print(json.dumps({"launcher": str(launcher), "skill": str(skill), "source": str(source), "permanent_service_installed": False}))


if __name__ == "__main__":
    main()
