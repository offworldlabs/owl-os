#!/bin/bash
#
# RSPduo watchdog. Cron runs it every 5 minutes (/etc/cron.d/blah2-rspduo-watchdog).
#
# When blah2's map data goes stale it restarts the retina-node stack and the
# SDRplay service. If the previous check already did that and the data is still
# stale, it also resets the RSPduo on USB, the only software step that can clear
# a wedged device: a service restart does not touch USB state.
#
# owl-os's radar_packages role installs this file. blah2-arm keeps a mirror at
# script/blah2_rspduo_restart.bash that must stay identical.
# Design, state files and the incident behind the USB step: owl-os
# docs/rspduo-watchdog.md.
#
# mode.txt, restart.lock and calibrate.lock are contracts with retina-gui. See
# its docs/architecture.md#coupling-with-blah2-arm before changing any of them.

# The overrides exist for tests/rspduo-watchdog. Nodes use the defaults.
COMPOSE_FILE=${RSPDUO_WATCHDOG_COMPOSE_FILE:-/data/mender-docker-compose/current/manifests/docker-compose.yaml}
PROJECT=retina-node
GUI_DIR=${RSPDUO_WATCHDOG_GUI_DIR:-/data/retina-gui}
STATE_DIR=${RSPDUO_WATCHDOG_STATE_DIR:-/run}
USB_DEVICES=${RSPDUO_WATCHDOG_USB_DEVICES:-/sys/bus/usb/devices}
MAP_URL=${RSPDUO_WATCHDOG_MAP_URL:-127.0.0.1:3000/api/map}

MODE_FILE=$GUI_DIR/mode.txt
RESTART_LOCK=$GUI_DIR/restart.lock
CALIBRATE_LOCK=$GUI_DIR/calibrate.lock
# Mirrors CALIBRATE_LOCK_TIMEOUT in retina-gui's device_state.py. If they
# disagree, a crashed calibration silences this watchdog, or it restarts the
# stack under a live run.
CALIBRATE_LOCK_TIMEOUT_SECONDS=1200

# "<container id> <RestartCount>" from the last check.
RESTART_STATE=$STATE_DIR/blah2-rspduo-watchdog.restarts
# Consecutive recoveries since the map data was last fresh. Read by owl-os's
# mender-inventory-retina-stack.
RECOVERIES_STATE=$STATE_DIR/blah2-rspduo-watchdog.recoveries

GRACE_PERIOD_SECONDS=60
STALE_SECONDS=60
# The recovery that reaches this count, and every one after it, also resets the
# RSPduo on USB.
USB_RESET_FROM_RECOVERY=2
SDRPLAY_VENDOR=1df7
# Bracketed so it cannot match a shell whose command line merely contains it.
SDRPLAY_PROCESS='[s]drplay_apiService'

# Cron output goes nowhere on a node (no MTA), so log to the journal as well.
log() {
  echo "$*"
  logger -t blah2-rspduo-watchdog -- "$*" 2>/dev/null || true
}

read_count() {
  local n
  n=$(cat "$1" 2>/dev/null)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  echo "$n"
}

# blah2 is stopped on purpose in spectrum and sdrconnect modes. Earlier failures
# say nothing about the radar the user returns to, so forget them.
MODE=$(cat "$MODE_FILE" 2>/dev/null)
if [ "$MODE" = "spectrum" ] || [ "$MODE" = "sdrconnect" ]; then
  rm -f "$RECOVERIES_STATE"
  exit 0
fi

# Auto-Calibrate owns the SDR for the whole run. A restart reverts the gains it
# applied through blah2's live retune API and silently corrupts its result.
if [ -f "$CALIBRATE_LOCK" ]; then
  LOCK_MTIME=$(stat -c %Y "$CALIBRATE_LOCK" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - LOCK_MTIME )) -lt "$CALIBRATE_LOCK_TIMEOUT_SECONDS" ]; then
    exit 0
  fi
fi

# Docker's RestartCount rising between checks means blah2 is crash-looping
# under its own restart policy, so StartedAt is always fresh and the grace
# period below would otherwise skip every check. Recreating the container resets
# the count, and Docker's backoff is regular enough for it to climb back to
# exactly the stored number by the next check, so a new container counts from 0.
read -r CONTAINER_ID RESTART_COUNT <<< "$(docker inspect -f '{{.Id}} {{.RestartCount}}' blah2 2>/dev/null)"
read -r PREV_CONTAINER_ID PREV_RESTART_COUNT <<< "$(cat "$RESTART_STATE" 2>/dev/null)"
[ -n "$RESTART_COUNT" ] && echo "$CONTAINER_ID $RESTART_COUNT" > "$RESTART_STATE"
if [ -n "$PREV_CONTAINER_ID" ] && [ "$CONTAINER_ID" != "$PREV_CONTAINER_ID" ]; then
  PREV_RESTART_COUNT=0
fi

CRASH_LOOPING=false
if [ -n "$RESTART_COUNT" ] && [ -n "$PREV_RESTART_COUNT" ] && [ "$RESTART_COUNT" -gt "$PREV_RESTART_COUNT" ]; then
  CRASH_LOOPING=true
  log "blah2 is crash-looping (RestartCount $PREV_RESTART_COUNT -> $RESTART_COUNT since last check), bypassing grace period"
fi

# Give a freshly (re)started blah2 time to acquire the RSPduo.
if [ "$CRASH_LOOPING" = false ]; then
  CONTAINER_STARTED=$(docker inspect -f '{{.State.StartedAt}}' blah2 2>/dev/null)
  if [ -n "$CONTAINER_STARTED" ]; then
    STARTED_EPOCH=$(date -d "$CONTAINER_STARTED" +%s 2>/dev/null)
    if [ -n "$STARTED_EPOCH" ] && [ $(( $(date +%s) - STARTED_EPOCH )) -lt $GRACE_PERIOD_SECONDS ]; then
      exit 0
    fi
  fi
fi

# The map starts {"timestamp":<13-digit ms>. --max-time so a hung API cannot
# leave instances piling up under cron. No answer, or not a number, is stale.
MAP_HEAD=$(curl -s --max-time 10 "$MAP_URL" | head -c23)
FIRST_CHAR=${MAP_HEAD:0:1}
TIMESTAMP=${MAP_HEAD:13:10}
case "$TIMESTAMP" in ''|*[!0-9]*) TIMESTAMP=0 ;; esac
AGE=$(( $(date +%s) - TIMESTAMP ))

PREV_RECOVERIES=$(read_count "$RECOVERIES_STATE")

if [ "$FIRST_CHAR" = "{" ] && [ "$AGE" -le "$STALE_SECONDS" ]; then
  if [ "$PREV_RECOVERIES" -gt 0 ]; then
    log "map data is fresh again after $PREV_RECOVERIES recovery attempt(s)"
  fi
  echo 0 > "$RECOVERIES_STATE"
  exit 0
fi

# retina-gui holds this lock for every operation that drives the compose
# project. -n: a restart already in flight will likely fix what this check saw,
# and cron is back in 5 minutes. A skipped check is not a recovery.
mkdir -p "$(dirname "$RESTART_LOCK")" 2>/dev/null || true
exec 9>"$RESTART_LOCK" || exit 0
if ! flock -n 9; then
  log "map data is stale but a stack restart is already in flight, skipping"
  exit 0
fi

RECOVERY=$((PREV_RECOVERIES + 1))
echo "$RECOVERY" > "$RECOVERIES_STATE"

sdrplay_running() {
  pgrep -f "$SDRPLAY_PROCESS" > /dev/null 2>&1
}

# Polls every 0.2 s, up to 15 s. A 1 s poll cost a whole second on most
# recoveries, because the service is usually still exiting at the first check.
wait_for_sdrplay_exit() {
  local i
  for ((i = 0; i < 75; i++)); do
    sdrplay_running || return 0
    sleep 0.2
  done
  return 1
}

# Stop the unit before killing the process: sdrplay.service has
# Restart=on-failure, RestartSec=1, so a bare kill gets it respawned straight
# back onto the device. systemd only suppresses that once the stop job is
# running (the unit leaves "active"), so wait for that. SIGKILL because the
# service ignores SIGTERM.
stop_sdrplay() {
  local i
  timeout 15 systemctl stop --no-block sdrplay.service > /dev/null 2>&1
  for ((i = 0; i < 25; i++)); do
    [ "$(timeout 5 systemctl is-active sdrplay.service 2>/dev/null)" = active ] || break
    sleep 0.2
  done
  pkill -9 -f "$SDRPLAY_PROCESS" > /dev/null 2>&1
  if ! wait_for_sdrplay_exit; then
    log "sdrplay_apiService survived SIGKILL"
  fi
}

start_sdrplay() {
  timeout 15 systemctl reset-failed sdrplay.service > /dev/null 2>&1
  if ! timeout 30 systemctl start sdrplay.service > /dev/null 2>&1; then
    log "systemctl start sdrplay.service failed or timed out"
  fi
}

sdrplay_devices() {
  local dev
  for dev in "$USB_DEVICES"/*; do
    [ "$(cat "$dev/idVendor" 2>/dev/null)" = "$SDRPLAY_VENDOR" ] && [ -f "$dev/authorized" ] && echo "$dev"
  done
}

# Two steps, because each clears what the other cannot. Toggling `authorized`
# tears down the interfaces, which drops any claim on them, and reconfigures the
# device. It does not touch the bus. usbreset then issues a real port reset.
reset_rspduo() {
  local devs dev i
  devs=$(sdrplay_devices)
  if [ -z "$devs" ]; then
    log "no SDRplay device on the USB bus, nothing to reset: check the RSPduo's cable and power"
    return
  fi
  for dev in $devs; do
    log "resetting SDRplay device $SDRPLAY_VENDOR:$(cat "$dev/idProduct" 2>/dev/null) at USB ${dev##*/}"
    echo 0 > "$dev/authorized" || log "could not deauthorise USB ${dev##*/}"
  done
  sleep 2
  for dev in $devs; do
    echo 1 > "$dev/authorized" || log "could not reauthorise USB ${dev##*/}"
    timeout 15 usbreset "$(printf '%03d/%03d' "$(cat "$dev/busnum" 2>/dev/null)" "$(cat "$dev/devnum" 2>/dev/null)")" > /dev/null 2>&1 \
      || log "usbreset failed for USB ${dev##*/}"
  done
  for ((i = 0; i < 10; i++)); do
    [ -n "$(sdrplay_devices)" ] && break
    sleep 1
  done
  [ -n "$(sdrplay_devices)" ] || log "the SDRplay device did not come back after the USB reset"
  if sdrplay_running; then
    pkill -9 -f "$SDRPLAY_PROCESS" > /dev/null 2>&1
    wait_for_sdrplay_exit || log "sdrplay_apiService is still running after the USB reset"
  fi
}

if [ "$TIMESTAMP" -eq 0 ]; then
  PROBLEM="no map data"
else
  PROBLEM="map data is stale (age ${AGE}s)"
fi
if [ "$RECOVERY" -ge "$USB_RESET_FROM_RECOVERY" ]; then
  log "$PROBLEM, recovery $RECOVERY in a row: restarting the stack and resetting the RSPduo on USB"
else
  log "$PROBLEM: restarting the stack"
fi

docker compose -p $PROJECT -f "$COMPOSE_FILE" down
stop_sdrplay
[ "$RECOVERY" -ge "$USB_RESET_FROM_RECOVERY" ] && reset_rspduo
start_sdrplay
docker compose -p $PROJECT -f "$COMPOSE_FILE" up -d
log "restarted the stack (recovery $RECOVERY)"
exit 0
