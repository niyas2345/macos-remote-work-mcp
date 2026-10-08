#!/usr/bin/env bash
# One-command setup for the macOS Remote Work & Accessibility MCP server.
# Installs dependencies, initializes state, starts the MCP server (:8000)
# and the Grok adapter (:8010) in the background, and verifies connectivity.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR="$SCRIPT_DIR/.run"
LOG_DIR="$RUN_DIR/logs"
VENV="$SCRIPT_DIR/.venv"
STATE_DIR="$HOME/.macos-remote-work"
BACKUP_DIR="$STATE_DIR/backups"

info() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

info "macOS Remote Work & Accessibility MCP - setup"

# 1. Platform check (macOS Monterey 12 or later)
[[ "$(uname -s)" == "Darwin" ]] || fail "This project targets macOS (Monterey 12+)."
MIN_MAJOR=12
OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[[ "$OS_MAJOR" -ge "$MIN_MAJOR" ]] || fail "macOS ${MIN_MAJOR}+ required (found $(sw_vers -productVersion))."

# 2. Python 3.9+
PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || fail "python3 not found. Install it with: brew install python3"
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9+ required, found %s" % sys.version.split()[0])
print("[setup] %s %s" % (sys.executable, sys.version.split()[0]))
PY

# 3. Virtualenv + dependencies (fastmcp, fastapi, uvicorn, requests)
if [[ ! -x "$VENV/bin/python" ]]; then
  info "Creating virtualenv in .venv"
  "$PYTHON_BIN" -m venv "$VENV"
fi
info "Installing dependencies (fastmcp, fastapi, uvicorn, requests)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r requirements.txt

# 4. Config/state directories + JSON initialization
mkdir -p "$STATE_DIR" "$BACKUP_DIR" "$RUN_DIR" "$LOG_DIR"
info "Initializing cursor_themes.json & macos_profiles.json"
"$VENV/bin/python" -c "import mcp_server; mcp_server.ensure_state(); print('[setup] state files OK')"

# 5. Stop any previous instances
stop_pid() {
  local pidfile="$1" label="$2"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      info "Stopping previous $label (pid $pid)"
      kill "$pid" 2>/dev/null || true
      local _i=0
      while kill -0 "$pid" 2>/dev/null && [[ $_i -lt 20 ]]; do
        sleep 0.25
        _i=$((_i + 1))
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
}
stop_pid "$RUN_DIR/mcp.pid" "MCP server"
stop_pid "$RUN_DIR/adapter.pid" "Grok adapter"

# 6. Start both services in the background (localhost only)
info "Starting MCP server on 127.0.0.1:8000"
nohup "$VENV/bin/python" mcp_server.py >"$LOG_DIR/mcp_server.out" 2>&1 &
echo $! > "$RUN_DIR/mcp.pid"

info "Starting Grok adapter on 127.0.0.1:8010"
nohup "$VENV/bin/python" grok_adapter.py >"$LOG_DIR/grok_adapter.out" 2>&1 &
echo $! > "$RUN_DIR/adapter.pid"

# 7. Verify connectivity (bridge health + MCP tool list)
info "Verifying connectivity..."
if ! "$VENV/bin/python" - <<'PY'
import asyncio
import sys
import time

import requests
from fastmcp import Client

MCP_URL = "http://127.0.0.1:8000/mcp"
HEALTH_URL = "http://127.0.0.1:8010/health"


def wait_bridge(timeout=40.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            response = requests.get(HEALTH_URL, timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise SystemExit("bridge health check failed: %r" % (last,))


async def list_mcp_tools():
    async with Client(MCP_URL) as client:
        return [tool.name for tool in await client.list_tools()]


def wait_mcp(timeout=40.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return asyncio.run(list_mcp_tools())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise SystemExit("MCP server check failed: %r" % (last,))


health = wait_bridge()
names = wait_mcp()
print("[setup] bridge OK: %s" % health)
print("[setup] MCP OK: %d tools -> %s" % (len(names), ", ".join(sorted(names))))
if len(names) != 10 or int(health.get("tools", 0)) != 10:
    raise SystemExit(
        "expected 10 tools, got mcp=%d bridge=%s" % (len(names), health.get("tools"))
    )
print("[setup] verification passed")
PY
then
  warn "Verification failed. Last 30 log lines:"
  tail -n 30 "$LOG_DIR/mcp_server.out" 2>/dev/null || true
  tail -n 30 "$LOG_DIR/grok_adapter.out" 2>/dev/null || true
  fail "Setup failed - see logs above."
fi

# 8. Summary / configuration URLs
cat <<SUMMARY

============================================================
 Setup complete. Configuration for ChatGPT / Grok:
============================================================
 MCP server (streamable HTTP) : http://127.0.0.1:8000/mcp
 Grok adapter health          : http://127.0.0.1:8010/health
 Grok adapter tool list       : http://127.0.0.1:8010/tools
 Grok adapter tool endpoint   : http://127.0.0.1:8010/tool   (POST)
 ChatGPT function schemas     : $SCRIPT_DIR/chatgpt_tools.json
 ChatGPT GPT instructions     : $SCRIPT_DIR/examples/chatgpt_custom_gpt_instructions.md
 Grok tool config             : $SCRIPT_DIR/examples/grok_tool_config.json
 State files                  : $SCRIPT_DIR/cursor_themes.json
                                $SCRIPT_DIR/macos_profiles.json
 Logs                         : $STATE_DIR/logs.json
 Backups                      : $BACKUP_DIR
 Service logs                 : $LOG_DIR
 PIDs                         : $RUN_DIR/mcp.pid, $RUN_DIR/adapter.pid

 Stop services with:
   kill \$(cat $RUN_DIR/mcp.pid) \$(cat $RUN_DIR/adapter.pid)

 Permissions note: all settings are user-level (no sudo). The
 enable_voiceover tool uses Cmd+F5 via osascript; if it fails,
 grant Accessibility permission to your terminal app in
 System Preferences > Security & Privacy > Privacy > Accessibility.
============================================================
SUMMARY
