#!/usr/bin/env bash
#
# deploy.sh -- push code to the Pi and restart the service.
#
# Deliberately does NOT copy crop.json, settings.json, run_state.json or any
# temps_*.csv. Those are the Pi's own state: tuning belongs to the physical
# camera position, settings are whatever was last chosen in the UI, and the
# CSVs are run data. Copying them from a dev machine would silently destroy a
# tuning session or a run.
#
# Usage:
#   ./deploy.sh                    # copy code, restart the service
#   ./deploy.sh --check            # Python 3.5 syntax gate only, no deploy
#   ./deploy.sh --install-service  # install/refresh the systemd unit, then deploy
#
set -euo pipefail

PI="${STILLMON_PI:-pi@192.168.113.98}"
REMOTE_DIR="Documents/templog"
FILES=(capture.py chart.py config.py runner.py stillmon.py)
UNIT=stillmon.service

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

mode="${1:-}"

# The Pi runs Python 3.5. A modern Mac Python happily accepts f-strings and
# other syntax that dies there, so gate every deploy on the real interpreter.
echo "==> Python 3.5 syntax check on ${PI}"
scp -q "${FILES[@]}" "${PI}:/tmp/"
ssh -n "$PI" "cd /tmp && python3 -m py_compile ${FILES[*]}"
echo "    OK"

# The control panel's JS lives inside a Python string, so Python's escape
# handling can silently corrupt it. One bad escape is a single SyntaxError that
# stops the whole script parsing -- every button then does nothing, with no
# server-side symptom at all. There is no JS engine here to parse it properly,
# so check the two things that actually went wrong.
echo "==> control panel sanity checks"
python3 - <<'PY'
import re, sys
src = open("stillmon.py").read()
if 'PAGE = r"""' not in src:
    sys.exit("    FAIL: PAGE must be a raw string (r\"\"\") -- "
             "otherwise Python eats \\n and \\' meant for the browser")
page = re.search(r'PAGE = r"""(.*?)"""', src, re.S).group(1)
KEYWORDS = {"if", "for", "while", "switch", "return", "typeof", "catch"}
handlers = set(re.findall(r'\son\w+="(\w+)\(', page)) - KEYWORDS
defined = set(re.findall(r'function\s+(\w+)\s*\(', page))
missing = sorted(h for h in handlers if h not in defined)
if missing:
    sys.exit("    FAIL: inline handlers with no function: %s" % missing)
ids = set(re.findall(r'\bid="([\w\-]+)"', page))
used = set(re.findall(r'el\("([\w\-]+)"\)', page))
unknown = sorted(u for u in used if u not in ids)
if unknown:
    sys.exit("    FAIL: el() references missing elements: %s" % unknown)
print("    OK: %d handlers, %d element refs" % (len(handlers), len(used)))
PY

if [[ "$mode" == "--check" ]]; then
  exit 0
fi

# Refuse to interrupt a run in progress -- a still run is hours long.
echo "==> checking for a run in progress"
if ssh -n "$PI" "test -f ~/${REMOTE_DIR}/run_state.json"; then
  echo "    REFUSING: a logging run is active on the Pi." >&2
  echo "    Stop it from the control panel first." >&2
  exit 1
fi
echo "    idle"

echo "==> copying code"
scp -q "${FILES[@]}" "${PI}:${REMOTE_DIR}/"

if [[ "$mode" == "--install-service" ]]; then
  echo "==> installing systemd unit"
  scp -q "$UNIT" "${PI}:/tmp/${UNIT}"
  ssh -n "$PI" "sudo cp /tmp/${UNIT} /etc/systemd/system/${UNIT} && \
    sudo systemctl daemon-reload && sudo systemctl enable ${UNIT}"
  echo "    installed and enabled at boot"
fi

echo "==> restarting service"
# systemd is what makes this reliable. Launching by hand over ssh does not
# work well here: `nohup ... &` leaves the process in the ssh session's process
# group so it dies when the session tears down, `setsid` fixes that but then
# the ssh call itself never returns, and a `pkill -f stillmon.py` in the same
# command line matches the shell running it and kills itself mid-command.
# `systemctl restart` sidesteps the lot and returns immediately.
if ! ssh -n "$PI" "systemctl is-enabled ${UNIT}" >/dev/null 2>&1; then
  echo "    systemd unit not installed. Run: ./deploy.sh --install-service" >&2
  exit 1
fi
ssh -n "$PI" "sudo systemctl restart ${UNIT}"
sleep 3

host="${PI#*@}"
if curl -sf --max-time 10 -o /dev/null "http://${host}:8001/status"; then
  echo "    up: http://${host}:8001/"
else
  echo "    WARNING: service did not answer /status" >&2
  ssh -n "$PI" "sudo journalctl -u ${UNIT} -n 20 --no-pager" >&2
  exit 1
fi
