#!/bin/bash
# Start the AdvisingBot local server and open it in Chrome.
# Double-click in Finder, or run from Terminal.
#
#   AdvisingBot.command              start (or just open Chrome if already up)
#   AdvisingBot.command --restart    stop whatever holds the port, then start fresh
#   AdvisingBot.command --stop       stop the server and exit
#
# While it runs: press Ctrl-C, or type q (or :q) and Return, to stop the server.
# To pick up code changes: quit with q, then launch it again (a server that is
# already running keeps serving the code it started with).
#
# AdvisingBot reads Stellic through OpenTabs, so OpenTabs needs to be running
# and you need to be signed in to Stellic in Chrome.

set -euo pipefail

PROJECT="/Users/glang/Library/CloudStorage/Dropbox/Code/AdvisingBot/advisingbot"
PORT=8765
URL="http://127.0.0.1:${PORT}"
OPENTABS_PORT=9515

# Finder-launched scripts get a minimal PATH; Homebrew python lives here.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

RESTART=0
STOP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --restart) RESTART=1 ;;
    --stop) STOP_ONLY=1 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

cd "$PROJECT"

open_chrome() {
  open -a "Google Chrome" "$URL"
}

# Stop whatever is LISTENING on PORT -- the -sTCP:LISTEN filter matters, because a
# plain port lookup also returns clients, and Chrome (which has the page open) then
# shows up as a kill target. Identifying the server by port rather than by the pid
# we spawned also survives the server being started some other way.
stop_server() {
  local quiet="${1:-}" pids
  pids=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$pids" ]; then
    [ "$quiet" = quiet ] || echo "Nothing is listening on port ${PORT}."
    return 0
  fi

  [ "$quiet" = quiet ] || echo "Stopping the server on port ${PORT}..."
  kill $pids 2>/dev/null || true

  for _ in $(seq 1 10); do
    sleep 1
    if [ -z "$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)" ]; then
      [ "$quiet" = quiet ] || echo "Stopped."
      return 0
    fi
  done

  [ "$quiet" = quiet ] || echo "It did not exit; forcing."
  kill -9 $(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true) 2>/dev/null || true
  sleep 1
}

if [ "$STOP_ONLY" -eq 1 ]; then
  stop_server
  exit 0
fi

if [ "$RESTART" -eq 1 ]; then
  stop_server
elif curl -sfo /dev/null --max-time 2 "$URL"; then
  echo "AdvisingBot is already running at $URL"
  echo "(Changed the code? Re-run with --restart to pick it up.)"
  open_chrome
  exit 0
fi

# Not fatal -- the server still starts -- but every lookup fails without it, and
# the reason is far from obvious once you are staring at the page.
if [ -z "$(lsof -ti "tcp:${OPENTABS_PORT}" -sTCP:LISTEN 2>/dev/null || true)" ]; then
  echo "Warning: nothing is listening on port ${OPENTABS_PORT}, so OpenTabs"
  echo "         looks closed. Start it, or audits will not load."
  echo ""
fi

echo "Starting AdvisingBot..."
python3 app.py &
SERVER_PID=$!

SHUTTING_DOWN=0

# Idempotent: the watch loop below must not mistake our own teardown for a crash.
shutdown() {
  [ "$SHUTTING_DOWN" -eq 1 ] && return 0
  SHUTTING_DOWN=1
  trap - EXIT INT TERM
  echo ""
  echo "Shutting down..."
  # Reaping inside the redirect keeps bash's "Terminated" job notice off the screen.
  { kill "$SERVER_PID" && wait "$SERVER_PID"; } 2>/dev/null || true
  stop_server quiet
  echo "AdvisingBot stopped."
  exit 0
}
trap shutdown EXIT INT TERM

started=0
for _ in $(seq 1 60); do
  if curl -sfo /dev/null --max-time 2 "$URL"; then
    started=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    trap - EXIT INT TERM
    echo "Server exited before it started listening. See the output above." >&2
    exit 1
  fi
  sleep 1
done

if [ "$started" -eq 0 ]; then
  echo "Timed out waiting for $URL." >&2
  exit 1
fi

open_chrome
echo ""
echo "AdvisingBot is running at $URL"
echo "Press Ctrl-C, or type q and Return, to stop it."
echo ""

# With a terminal attached, watch for a quit keystroke while still noticing if the
# server falls over on its own. Without one (piped or detached), there is nobody to
# type, so just wait on the process.
if [ -t 0 ]; then
  while true; do
    if [ "$SHUTTING_DOWN" -eq 0 ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      trap - EXIT INT TERM
      echo "The server stopped on its own. See the output above."
      exit 1
    fi
    line=""
    read -r -t 2 line || true
    case "$(printf '%s' "$line" | tr -d '[:space:]')" in
      q|:q|Q|:Q|quit|:quit|exit) shutdown ;;
    esac
  done
else
  wait "$SERVER_PID"
fi
