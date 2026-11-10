# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Render and explicitly deploy pinned thin workflows; never mirror state branches."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re

import dev_agent as client


def workflow(engine, private):
    if not re.fullmatch('[a-f0-9]{40}', engine):
        raise ValueError('Pin a full engine commit SHA')
    source = (Path(__file__).parent / '.github/workflows/worker.yml').read_text(encoding='utf-8')
    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError('Workflow template drift; review before deploying')
        source = source.replace(old, new)
    replace('${{ vars.DEV_AGENT_ENGINE_SHA || github.sha }}', engine)
    if private:
        replace('permissions: {}', 'permissions:\n  contents: write')
        replace('DEV_AGENT_POLICY: public', 'DEV_AGENT_POLICY: private')
        replace('      - name: Bootstrap isolated development worker\n        shell: bash', '      - name: Bootstrap isolated development worker\n        shell: bash\n        env:\n          GH_CHECKPOINT_TOKEN: ${{ github.token }}')
        replace('sudo -u devagent python3 /opt/dev-agent/worker.py supervise --root /tmp/dev-agent/state --session "$session"', 'sudo python3 /opt/dev-agent/worker.py supervise --root /tmp/dev-agent/state --session "$session" --checkpoint-repository "$GITHUB_REPOSITORY" --token-file /opt/dev-agent/github-token')
    return source.encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['ha-mcp', 'private'], required=True)
    parser.add_argument('--engine', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--codex-session', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--t3-thread', default=os.environ.get('T3_THREAD_ID'))
    args = parser.parse_args()
    client.select_profile(args.profile)
    repo = client.gh(f'repos/{client.REPOSITORY}')
    if repo['private'] != client.PRIVATE:
        raise ValueError('Deployment visibility mismatch')
    branch = client.WORKFLOW_BRANCH or repo['default_branch']
    if repo['default_branch'] != branch:
        raise ValueError('The dispatch workflow must be hosted on the default administration branch; preserve master as the separate clean mirror')
    path = f'repos/{client.REPOSITORY}/contents/.github/workflows/worker.yml'
    previous = client.gh(path + '?ref=' + branch)
    desired = workflow(args.engine, client.PRIVATE)
    changed = base64.b64decode(previous['content']) != desired
    if args.apply and changed:
        message = 'chore: pin common development worker engine\n'
        if args.codex_session:
            message += '\nCodex-Session: ' + args.codex_session + '\n'
        if args.t3_thread:
            message += 'T3-Thread: ' + args.t3_thread + '\n'
        client.gh(path, 'PUT', {'message': message, 'sha': previous['sha'], 'branch': branch, 'content': base64.b64encode(desired).decode()})
        observed = client.gh(path + '?ref=' + branch)
        if base64.b64decode(observed['content']) != desired:
            raise RuntimeError('Workflow publication not confirmed; reconcile before another write')
    result = {'repository': client.REPOSITORY, 'branch': branch, 'engine_sha': args.engine,
              'changed': changed, 'applied': args.apply, 'workflow_sha256': hashlib.sha256(desired).hexdigest()}
    if args.receipt:
        client.write_json(args.receipt, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
