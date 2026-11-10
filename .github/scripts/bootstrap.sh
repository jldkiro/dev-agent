set -euo pipefail
# This bootstrap is Linux-only. Detailed diagnostics stay on the disposable VM.
exec 3>&1
exec >>"$RUNNER_TEMP/dev-agent-bootstrap.log" 2>&1
stage=input-validation
finish_bootstrap() {
  code=$?
  if [ "$code" -ne 0 ]; then
    case "$stage" in input-validation|runtime|restore|ssh|selftest|tunnel|tunnel-probe|metadata) ;; *) stage=unknown ;; esac
    printf 'DEV_AGENT_START_FAILED step=%s\n' "$stage" >&3
  fi
}
trap finish_bootstrap EXIT
printf '%s\n' 'DEV_AGENT_STARTING' >&3
mapfile -t request_values < <(python3 - <<'PY'
import json,os
with open(os.environ['GITHUB_EVENT_PATH']) as source:
    request=json.load(source)['inputs']
for key in ('session_id','request_id','public_key'):
    print(request[key])
PY
)
export SESSION_ID="${request_values[0]}"
export REQUEST_ID="${request_values[1]}"
export CLIENT_PUBLIC_KEY="${request_values[2]}"
case "${DEV_AGENT_POLICY:-public}" in
  public) export IDLE_SECONDS=3600 CHECKPOINTS=false ;;
  private) export IDLE_SECONDS=300 CHECKPOINTS=true ;;
  *) exit 1 ;;
esac
export ENGINE_REPOSITORY="${DEV_AGENT_ENGINE_REPOSITORY:-$GITHUB_REPOSITORY}"
export ENGINE_SHA="${DEV_AGENT_ENGINE_SHA:-$GITHUB_SHA}"

python3 - <<'PY'
import os,re
assert re.fullmatch(r'[a-f0-9]{32}', os.environ['SESSION_ID'])
assert re.fullmatch(r'[a-f0-9]{32}', os.environ['REQUEST_ID'])
assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', os.environ['CLIENT_PUBLIC_KEY'])
PY
command -v /usr/sbin/sshd || { sudo apt-get update -qq; sudo apt-get install -y -qq openssh-server; }
stage=runtime
control=/opt/dev-agent
data=/tmp/dev-agent
mkdir -p "$RUNNER_TEMP/dev-agent-public"
sudo mkdir -p "$control" "$data/home" "$data/state" /run/sshd
sudo chown root:root "$control"
sudo chmod 755 "$control"
curl --fail --silent --show-error --location "https://raw.githubusercontent.com/$ENGINE_REPOSITORY/$ENGINE_SHA/worker.py" -o "$RUNNER_TEMP/dev-agent-worker.py" &
worker_download=$!
curl --fail --silent --show-error --location "https://raw.githubusercontent.com/$ENGINE_REPOSITORY/$ENGINE_SHA/checkpoints.py" -o "$RUNNER_TEMP/dev-agent-checkpoints.py" &
checkpoint_download=$!
curl --fail --silent --show-error --location https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-unknown-linux-gnu.tar.gz -o "$RUNNER_TEMP/uv.tar.gz" &
uv_download=$!
curl --fail --silent --show-error --location https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/cloudflared-linux-amd64 -o "$RUNNER_TEMP/dev-agent-cloudflared" &
cf_download=$!
for download_pid in "$worker_download" "$checkpoint_download" "$uv_download" "$cf_download"; do wait "$download_pid"; done
sudo install -m 644 "$RUNNER_TEMP/dev-agent-worker.py" "$control/worker.py"
sudo install -m 644 "$RUNNER_TEMP/dev-agent-checkpoints.py" "$control/checkpoints.py"
if [ "$CHECKPOINTS" = true ]; then
  stage=restore
  python3 - <<'PY' | sudo tee /opt/dev-agent/github-token >/dev/null
import os
token=os.environ['GH_CHECKPOINT_TOKEN']
assert token
print(token)
PY
  sudo chmod 600 "$control/github-token"
  sudo python3 "$control/checkpoints.py" restore --root "$data/state" --session "$SESSION_ID" --repository "$GITHUB_REPOSITORY" --token-file "$control/github-token" > "$RUNNER_TEMP/dev-agent-restore.json"
  printf '%s\n' '{"private":true}' | sudo tee "$data/state/private-policy.json" >/dev/null
fi
unset GH_CHECKPOINT_TOKEN
sudo useradd --no-create-home --home-dir "$data/home" --shell /bin/bash devagent
sudo usermod --password '*' devagent
if getent group docker >/dev/null; then sudo usermod --append --groups docker devagent; fi
sudo chown -R devagent:devagent "$data"
echo 'f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645  '"$RUNNER_TEMP/uv.tar.gz" | sha256sum --check
mkdir "$RUNNER_TEMP/uv-bin"
tar -xzf "$RUNNER_TEMP/uv.tar.gz" --strip-components=1 -C "$RUNNER_TEMP/uv-bin"
sudo install -m 755 "$RUNNER_TEMP/uv-bin/uv" /usr/local/bin/uv
stage=ssh
sudo ssh-keygen -q -t ed25519 -N '' -f "$control/probe_key"
sudo chmod 600 "$control/probe_key"
{ printf '%s\n' "$CLIENT_PUBLIC_KEY"; sudo cat "$control/probe_key.pub"; } | sudo tee /etc/ssh/dev-agent-authorized_keys >/dev/null
sudo chmod 644 /etc/ssh/dev-agent-authorized_keys
sudo ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/dev-agent-host_key
sudo chmod 600 /etc/ssh/dev-agent-host_key
cat > "$RUNNER_TEMP/dev-agent-sshd" <<EOF
Port 2222
ListenAddress 127.0.0.1
HostKey /etc/ssh/dev-agent-host_key
PidFile $control/sshd.pid
AuthorizedKeysFile /etc/ssh/dev-agent-authorized_keys
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
PermitRootLogin no
AllowUsers devagent
AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
PermitTunnel no
PermitTTY no
UsePAM no
StrictModes yes
LogLevel ERROR
ForceCommand /usr/bin/python3 $control/worker.py rpc --root $data/state --session $SESSION_ID
EOF
sudo install -m 600 "$RUNNER_TEMP/dev-agent-sshd" "$control/sshd_config"
sudo /usr/sbin/sshd -t -f "$control/sshd_config"
sudo -u devagent env -i HOME="$data/home" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 python3 "$control/worker.py" serve --root "$data/state" --session "$SESSION_ID" --idle-seconds "$IDLE_SECONDS" --lifetime-seconds 21000 > "$RUNNER_TEMP/dev-agent-controller.log" 2>&1 3>&- &
echo "$!" > "$RUNNER_TEMP/dev-agent-controller.pid"
sudo /usr/sbin/sshd -D -e -f "$control/sshd_config" > "$RUNNER_TEMP/dev-agent-sshd.log" 2>&1 3>&- &
{ printf '[127.0.0.1]:2222 '; sudo cat /etc/ssh/dev-agent-host_key.pub; } | sudo tee "$control/probe_known_hosts" >/dev/null
stage=selftest
sleep 1
if ! printf '{"op":"status","session":"%s"}\n' "$SESSION_ID" | sudo ssh -i "$control/probe_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$control/probe_known_hosts" -p 2222 devagent@127.0.0.1 rpc > "$RUNNER_TEMP/dev-agent-probe.json"; then
  sudo cat "$RUNNER_TEMP/dev-agent-sshd.log"
  exit 1
fi
python3 -c "import json,os,pathlib; d=json.loads((pathlib.Path(os.environ['RUNNER_TEMP'])/'dev-agent-probe.json').read_text()); assert d['ok'] and d['result']['phase']=='open'"
stage=tunnel
echo 'f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e  '"$RUNNER_TEMP/dev-agent-cloudflared" | sha256sum --check
chmod 755 "$RUNNER_TEMP/dev-agent-cloudflared"
"$RUNNER_TEMP/dev-agent-cloudflared" tunnel --no-autoupdate --url ssh://127.0.0.1:2222 > "$RUNNER_TEMP/dev-agent-tunnel.log" 2>&1 3>&- &
for attempt in $(seq 1 45); do
  grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' "$RUNNER_TEMP/dev-agent-tunnel.log" | head -1 > "$RUNNER_TEMP/dev-agent-url" || true
  if test -s "$RUNNER_TEMP/dev-agent-url" && grep -q 'Registered tunnel connection' "$RUNNER_TEMP/dev-agent-tunnel.log"; then break; fi
  sleep 2
done
test -s "$RUNNER_TEMP/dev-agent-url"
grep -q 'Registered tunnel connection' "$RUNNER_TEMP/dev-agent-tunnel.log"
stage=tunnel-probe
hostname=$(cat "$RUNNER_TEMP/dev-agent-url")
hostname=${hostname#https://}
{ printf 'dev-agent-public-probe '; sudo cat /etc/ssh/dev-agent-host_key.pub; } | sudo tee -a "$control/probe_known_hosts" >/dev/null
probe_ready=false
probe_deadline=$((SECONDS + 120))
while [ "$SECONDS" -lt "$probe_deadline" ]; do
  if printf '{"op":"status","session":"%s"}\n' "$SESSION_ID" | sudo ssh -i "$control/probe_key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 -o HostKeyAlias=dev-agent-public-probe -o UserKnownHostsFile="$control/probe_known_hosts" -o "ProxyCommand=$RUNNER_TEMP/dev-agent-cloudflared access ssh --hostname %h" "devagent@$hostname" rpc > "$RUNNER_TEMP/dev-agent-public-probe.json"; then
    probe_ready=true
    break
  fi
  sleep 1
done
test "$probe_ready" = true
python3 -c "import json,os,pathlib; d=json.loads((pathlib.Path(os.environ['RUNNER_TEMP'])/'dev-agent-public-probe.json').read_text()); assert d['ok'] and d['result']['phase']=='open'"
printf '%s\n' "$CLIENT_PUBLIC_KEY" | sudo tee /etc/ssh/dev-agent-authorized_keys >/dev/null
sudo rm -f "$control/probe_key" "$control/probe_key.pub" "$control/probe_known_hosts"
kill -0 "$(cat "$RUNNER_TEMP/dev-agent-controller.pid")"
sudo -u devagent python3 -c "import sqlite3; assert sqlite3.connect('/tmp/dev-agent/state/state.sqlite').execute('SELECT phase FROM state WHERE id=1').fetchone()[0]=='open'"
sudo -u devagent python3 -c "import sqlite3,time; c=sqlite3.connect('/tmp/dev-agent/state/state.sqlite'); c.execute('UPDATE state SET activity=? WHERE id=1',(time.monotonic(),)); c.commit()"
stage=metadata
sudo cat /etc/ssh/dev-agent-host_key.pub > "$RUNNER_TEMP/dev-agent-host.pub"
python3 - <<'PY'
import json,os,pathlib,time
root=pathlib.Path(os.environ['RUNNER_TEMP'])
data={'protocol':1,'ephemeral':True,'repository':os.environ['GITHUB_REPOSITORY'],'run_id':int(os.environ['GITHUB_RUN_ID']),'sha':os.environ['GITHUB_SHA'],'engine_sha':os.environ['ENGINE_SHA'],'session_id':os.environ['SESSION_ID'],'request_id':os.environ['REQUEST_ID'],'url':(root/'dev-agent-url').read_text().strip(),'host_key':(root/'dev-agent-host.pub').read_text().strip(),'capabilities':['channel','watch','gzip','source-cache'],'ready_probed':True,'idle_seconds':int(os.environ['IDLE_SECONDS']),'checkpoints':os.environ['CHECKPOINTS']=='true','created_at':time.time()}
if data['checkpoints']:
    restored=json.loads((root/'dev-agent-restore.json').read_text())
    data['checkpoint_restored']=restored['restored']
    data['checkpoint_fallback']=restored.get('fallback',False)
(root/'dev-agent-public'/'connection.json').write_text(json.dumps(data))
PY
printf '%s\n' 'DEV_AGENT_READY' >&3
