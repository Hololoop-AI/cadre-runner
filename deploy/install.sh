#!/usr/bin/env bash
# Install Cadre on a fresh machine: clone what is missing, link the CLI, write
# a config, install the services, and refuse to finish quietly if any of it is
# wrong.
#
# Safe to re-run. Nothing here overwrites a config you have edited, and the
# clone steps skip a checkout that already exists.
#
#   bash deploy/install.sh              # install and start
#   bash deploy/install.sh --no-start   # install, do not start the services
#   bash deploy/install.sh --check      # verify an existing install only
set -euo pipefail

RUNNER_REPO=https://github.com/Hololoop-AI/cadre-runner.git
RUNNER_BRANCH=true-prototype
SURFACE_REPO=https://github.com/Hololoop-AI/review-surface.git
SURFACE_BRANCH=feat/feedback-journal
SKILLS_REPO=https://github.com/BrandonPerez-Dev/ai-dev-skills.git

RUNNER_DIR="${CADRE_RUNNER_DIR:-$HOME/Projects/cadre/cadre-runner}"
SURFACE_DIR="${CADRE_SURFACE_DIR:-$HOME/Projects/review-surface}"
SKILLS_DIR="${CADRE_SKILLS_DIR:-$HOME/dev-config/ai-workflow-config}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"

START=1; CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites
say "Checking prerequisites"
command -v git    >/dev/null || die "git is not installed"
command -v node   >/dev/null || die "node is not installed (need 22+)"
command -v npm    >/dev/null || die "npm is not installed"
command -v python3 >/dev/null || die "python3 is not installed (need 3.11+)"
command -v claude >/dev/null || die "the Claude Code CLI is not on PATH; every turn is a headless \`claude -p\` run"

py_ok=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
[ "$py_ok" = 1 ] || die "python3 is $(python3 -V), need 3.11+"
node_major=$(node -p 'process.versions.node.split(".")[0]')
[ "$node_major" -ge 22 ] || die "node is $(node -v), need 22+"
ok "git, node $(node -v), $(python3 -V), claude"

if [ "$CHECK_ONLY" = 0 ]; then
  # ------------------------------------------------------------------- clones
  say "Fetching repositories"
  clone() {  # repo branch dir
    if [ -d "$3/.git" ]; then ok "$(basename "$3") already present — leaving it alone"
    else mkdir -p "$(dirname "$3")" && git clone -q --branch "$2" "$1" "$3" && ok "cloned $(basename "$3") ($2)"; fi
  }
  clone "$RUNNER_REPO"  "$RUNNER_BRANCH"  "$RUNNER_DIR"
  clone "$SURFACE_REPO" "$SURFACE_BRANCH" "$SURFACE_DIR"
  if [ -d "$SKILLS_DIR/.git" ]; then ok "skills already present"
  else git clone -q "$SKILLS_REPO" "$SKILLS_DIR" && ok "cloned skills"; fi

  say "Installing the page server"
  (cd "$SURFACE_DIR" && npm install --omit=dev --silent) && ok "npm install"

  # The runner finds the CLI with which(); if this symlink is not on PATH the
  # entire surface bridge disables itself without a single log line. This is
  # the most common way the install looks finished and is not.
  mkdir -p "$BIN_DIR"
  ln -sf "$SURFACE_DIR/dist/cli.mjs" "$BIN_DIR/review-surface"
  chmod +x "$SURFACE_DIR/dist/cli.mjs"
  ok "linked review-surface into $BIN_DIR"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
    *) warn "$BIN_DIR is NOT on your PATH. The services set their own PATH so they will work, but your shell will not find \`review-surface\`. Add it to your shell profile." ;;
  esac

  if [ -x "$SKILLS_DIR/scripts/setup-claude.sh" ]; then
    bash "$SKILLS_DIR/scripts/setup-claude.sh" >/dev/null && ok "linked skills into ~/.claude"
  else
    warn "no setup-claude.sh in the skills clone; sessions will run without named skills"
  fi

  # ------------------------------------------------------------------- config
  say "Writing config"
  CONFIG="$RUNNER_DIR/config.local.toml"
  if [ -f "$CONFIG" ]; then
    ok "config.local.toml already exists — not overwriting it"
  else
    actor=$(git config --get user.name 2>/dev/null || echo "$USER")
    cat > "$CONFIG" <<EOF
# Written by deploy/install.sh. Edit freely; re-running the installer will not
# overwrite this file.
[runner]
poll_interval = 15
data_dir = "~/.local/state/cadre-local"
skills_source = "$SKILLS_DIR/skills"
require_skills = false
status_bind = "127.0.0.1"          # 0.0.0.0 only on a network you trust
status_port = 8181

[claude]
bin = "claude"
effort = "high"
permission_mode = "bypass"         # headless turns cannot answer a prompt
timeout_seconds = 7200
default_model = "opus"

[limits]
allowed_actors = ["$actor"]
EOF
    ok "wrote $CONFIG"
  fi

  # ----------------------------------------------------------------- services
  say "Installing services"
  mkdir -p "$UNIT_DIR"
  for u in "$RUNNER_DIR"/deploy/systemd/*.service; do
    install -m 644 "$u" "$UNIT_DIR/$(basename "$u")"
  done
  systemctl --user daemon-reload
  ok "installed $(ls "$RUNNER_DIR"/deploy/systemd/*.service | wc -l) units"
  # Without lingering, every service dies when the last session logs out —
  # which on a laptop means the daemon is gone every morning.
  loginctl enable-linger "$USER" 2>/dev/null && ok "lingering enabled (services survive logout)" \
    || warn "could not enable lingering; services will stop when you log out"

  if [ "$START" = 1 ]; then
    systemctl --user enable --now cadre-surface.service cadre-statusd.service cadre-daemon.service
    ok "started surface, fleet page and daemon"
    sleep 3
  else
    ok "services installed but not started (--no-start)"
  fi
fi

# ------------------------------------------------------------------- verify
say "Verifying"
cd "$RUNNER_DIR"
CONFIG="$RUNNER_DIR/config.local.toml"
if [ "$START" = 1 ] || [ "$CHECK_ONLY" = 1 ]; then
  for s in cadre-surface cadre-statusd cadre-daemon; do
    state=$(systemctl --user is-active "$s.service" 2>/dev/null || true)
    [ "$state" = active ] && ok "$s is running" || warn "$s is $state — \`journalctl --user -u $s -n 30\`"
  done
fi
# preflight is the real gate: it checks the node registry, the action set, the
# board, and — the part that used to be missing — that the surface CLI and
# server are actually reachable.
if CADRE_CONFIG="$CONFIG" PATH="$BIN_DIR:$PATH" python3 -m runnerlib.preflight; then
  say "Cadre is ready — open http://127.0.0.1:8181"
else
  die "preflight failed; the table above says which check and what to do"
fi
