#!/bin/sh
# Run a candidate Cadre tree as a full preview, next to live, without touching live.
#
# Isolation: a rootless user+mount+network namespace (pasta). Inside it the
# candidate code, a snapshot of Cadre's state and a snapshot of the page
# server's state are bind-mounted over their NORMAL paths, and the services
# bind their NORMAL ports — so nothing needs reconfiguring and every absolute
# path in state resolves to the snapshot. Live ports are unreachable from
# inside (-T none -U none --no-map-gw; pasta's default -T auto would forward
# them in). Outbound internet works, so real agent turns are possible.
#
# usage: preview.sh [candidate-tree] [seconds-to-stay-up]
#   host ports while up: fleet page 18181, page server 14387
set -e
CAND=${1:-$HOME/Projects/cadre/cadre-runner}
HOLD=${2:-0}
K=$(cd "$(dirname "$0")" && pwd)   # this folder: capture.mjs sits next to this script
P=/tmp/cadre-preview
REPO=$HOME/Projects/cadre/cadre-runner
S=$HOME/.local/state/cadre-local
rm -rf $P; mkdir -p $P/code $P/state $P/rs $P/bin $P/work $P/out

rsync -a --exclude .git --exclude __pycache__ --exclude .playwright-mcp "$CAND/" $P/code/   # frozen candidate
rsync -a --exclude runs --exclude logs --exclude 'board.db*' --exclude registry.lock $S/ $P/state/
sqlite3 $S/board.db ".backup '$P/state/board.db'"
python3 - "$P/state/registry.json" <<'EOF'
import json, sys
p = sys.argv[1]; r = json.load(open(p))
for sec in r.values():
    for rec in sec.values():
        if isinstance(rec, dict) and "active_runs" in rec:
            rec["active_runs"] = {}      # live turns belong to live
json.dump(r, open(p, "w"), indent=2)
EOF
rsync -a --exclude server.log $HOME/.review-surface/ $P/rs/

cat > $P/bin/claude <<'EOF'
#!/bin/sh
# stub agent (drop this file to run real turns: the namespace has internet)
[ -n "$CADRE_SURFACE_OUT" ] && printf '<!doctype html><html lang=en><head><meta charset=utf-8><link rel=stylesheet href=/surface-theme.css><title>Preview smoke</title></head><body><main class=wrap><h1>Preview smoke turn</h1><p>Written by the stub agent inside the preview.</p></main></body></html>' > "$CADRE_SURFACE_OUT"
echo '{"type":"result","result":"preview smoke ok"}'
EOF
chmod +x $P/bin/claude

cat > $P/inside.sh <<EOF
set -e
mount --bind $P/state $S
mount --bind $P/rs $HOME/.review-surface
mount --bind $P/code $REPO
cd $REPO
E="env -i HOME=$HOME PATH=$P/bin:$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin CADRE_CONFIG=$REPO/config.local.toml CADRE_ENGINE=only REVIEW_SURFACE_NO_OPEN=1 PYTHONDONTWRITEBYTECODE=1 CADRE_STATUS_BIND=0.0.0.0 REVIEW_SURFACE_HOST=0.0.0.0 REVIEW_SURFACE_ALLOWED_HOSTS=127.0.0.1:14387,localhost:14387"
# 0.0.0.0 is the namespace's private interface: pasta delivers forwarded ports there, not to its loopback
\$E node $HOME/Projects/review-surface/dist/cli.mjs server --port 4387 >$P/out/surface.log 2>&1 &
\$E python3 statusd.py >$P/out/statusd.log 2>&1 &
sleep 3
TASK=\$(\$E python3 pipeline.py task "preview smoke: write a page and stop" --cwd $P/work | head -1)
\$E python3 pipeline.py once 2>&1 | grep -E "spawned turn|Traceback|refusing" | tee -a $P/out/daemon.log
sleep 2
\$E python3 pipeline.py once 2>&1 | grep -E "done|opened|Traceback" | tee -a $P/out/daemon.log
echo "inside: live page server reachable? \$(curl -s -o /dev/null -w %{http_code} --max-time 2 http://127.0.0.1:4387/health) (this is the PREVIEW's own server)"
node $K/capture.mjs http://127.0.0.1:8181/ $P/out preview 2>&1 | grep -v "^ *-"
[ "$HOLD" -gt 0 ] && { echo "preview up: http://127.0.0.1:18181 for $HOLD s"; sleep $HOLD; }
kill 0 2>/dev/null || true
EOF

pasta --config-net -q -T none -U none --no-map-gw -t 127.0.0.1/18181:8181 -t 127.0.0.1/14387:4387 -- unshare -m sh $P/inside.sh 2>&1 \
  | grep --line-buffered -v -E "nameserver|IPv6 routes|Terminated"
echo "outputs in $P/out"; ls $P/out
