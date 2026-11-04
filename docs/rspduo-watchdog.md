# RSPduo watchdog

`/opt/blah2/script/blah2_rspduo_restart.bash` runs from cron every 5 minutes
(`/etc/cron.d/blah2-rspduo-watchdog`). It restarts the radar when blah2 stops producing map
data, and resets the RSPduo on USB when a restart alone does not bring it back.

| File | Role |
| --- | --- |
| [radar_packages/files/blah2_rspduo_restart.bash](../plugins/playbooks/os_setup/roles/radar_packages/files/blah2_rspduo_restart.bash) | The script. This is the copy nodes run. |
| [radar_packages/tasks/main.yml](../plugins/playbooks/os_setup/roles/radar_packages/tasks/main.yml) | Installs the script and the cron entry. |
| [mender-inventory-retina-stack](../configuration/mender/inventory/mender-inventory-retina-stack) | Reports `retina_sdr_recoveries` to Mender. |
| [tests/rspduo-watchdog](../tests/rspduo-watchdog/test_rspduo_watchdog.py) | Runs the real script against stub commands and a fake sysfs. |

blah2-arm keeps a mirror at `script/blah2_rspduo_restart.bash`. Nodes never run it, but
retina-gui's docs point at it. Keep it identical: the two copies had already drifted once
before the script moved out of the playbook into its own file.

## What a check does

1. **Skips** when retina-gui's `mode.txt` is `spectrum` or `sdrconnect` (blah2 is stopped on
   purpose), or while Auto-Calibrate's `calibrate.lock` is younger than 20 minutes.
2. **Skips** for 60 s after blah2 starts, unless blah2 is crash-looping, which keeps
   `StartedAt` fresh forever. It counts as crash-looping when Docker's `RestartCount` rose since
   the last check. The state file stores the container id with the count, and a new container
   counts from 0: Docker's backoff is regular, so a recreated container often climbs back to
   exactly the stored number by the next check. Comparing the counts alone skipped every other
   check during a crash loop. See [Known limitation](#known-limitation) for the case this
   does not cover.
3. **Reads the map.** Fresh means the answer starts with `{` and its timestamp is no more than
   60 s old. No answer, an error page or a missing timestamp all count as stale.
4. **Recovers** if stale. It takes retina-gui's `restart.lock` with `flock -n` first, and
   skips the check if another restart holds it.

The paths shared with retina-gui are contracts. See retina-gui's
`docs/architecture.md#coupling-with-blah2-arm`.

## The recovery ladder

Each recovery is numbered: the number of stale checks in a row since the map was last fresh.

| Recovery | Steps |
| --- | --- |
| 1 | `compose down`, stop `sdrplay.service`, start it, `compose up -d` |
| 2 and every one after | The same, plus resetting the RSPduo on USB between the stop and the start |

A fresh map resets the count to 0. A check skipped for the restart lock does not count.
Switching to spectrum or SDRconnect mode clears it. It lives in `/run`, so a reboot clears it
too.

If the reset does not help, it runs again at every check. From cron, a wedged RSPduo costs about
10 minutes of radar: measured at 10.3 minutes on owl-ded9, with the data back 25 s after the tick
that ran the USB reset.

### Stopping the SDRplay service

`sdrplay.service` has `Restart=on-failure` and `RestartSec=1`, and `sdrplay_apiService`
ignores SIGTERM. So the script asks systemd to stop the unit (`--no-block`, since the stop
would otherwise wait for a SIGTERM the process ignores), waits up to 5 s for the unit to leave
`active`, then SIGKILLs the process and waits up to 15 s for it to go. A kill alone would let
systemd relaunch the service onto the device a second later, in the middle of a USB reset.
systemd does not apply `Restart=` once a stop job is running, which is why the script waits for
the unit to leave `active` before the kill. It polls every 0.2 s to keep recovery as fast as the
old `kill` and `systemctl restart`.

The `pkill`/`pgrep` pattern is `[s]drplay_apiService`, bracketed so it never matches a shell
whose command line merely contains the name. retina-gui's `restart_sdrplay_service` does the
same (its `docs/features/sdr-mode.md` covers why).

### Resetting the RSPduo

For every device under `/sys/bus/usb/devices` with `idVendor` `1df7` (SDRplay), the script runs
two steps, because each clears something the other cannot:

1. **Toggle `authorized`:** write `0`, wait 2 s, write `1`. This tears down the device's
   interfaces, which drops any claim on them, and reconfigures it. It does not touch the bus: the
   kernel logs only `authorized to connect`, and the device keeps its number. On its own it
   clears a claimed interface or an unconfigured device.
2. **`usbreset BBB/DDD`** (`USBDEVFS_RESET`, from `usbutils`): a real port reset. The kernel logs
   `reset high-speed USB device number N`. It runs second because a port reset should restore
   the device's previous configuration, which would leave an unconfigured device unconfigured.
   That is from the kernel's behaviour: the port reset was not tested on its own against a wedge.

On owl-ded9 the RSPduo streamed again after every reset, with no firmware reload logged.

The script then waits up to 10 s for the device, and SIGKILLs any `sdrplay_apiService` still
running. It matches on vendor, not on the RSPduo's product id `3020`, so a device in a different
state with another product id still gets reset. A node has only the one SDRplay device.

## Logs

Cron output goes nowhere on a node, since there is no MTA. Everything the script reports also
goes to the journal:

```bash
journalctl -t blah2-rspduo-watchdog
cat /run/blah2-rspduo-watchdog.recoveries
```

The Mender inventory attribute `retina_sdr_recoveries` carries the same count. 2 or more means
a stack restart did not help and the watchdog is resetting the RSPduo on USB.

## The 2026-10-26 incident

A stock owl-os-pi5-v0.19.0 node lost its radar just before 10:45 EDT and stayed down for about
ten hours. The old watchdog ran about 120 recoveries, all of them identical:

- At first `sdrplay_api_GetDevices failed`, with the service logging
  `libusb_claim_interface() -6` (`LIBUSB_ERROR_BUSY`).
- After a host reboot the firmware transfer failed at offset 0 with
  `libusb_control_transfer() -1` (`LIBUSB_ERROR_IO`).
- After that, the interface was busy again.

The reporter put the failed reboot down to the Pi keeping USB powered across it. We have not
confirmed that. The node owner recovered it by stopping blah2 and `sdrplay.service`,
deauthorising and reauthorising the `1df7:3020` device, and starting the service and then blah2.
The watchdog does that from its second recovery, and adds a port reset.

The cause of the wedge is unknown. The report had no kernel log. The next time it happens,
collect these before recovering by hand:

- `journalctl -k` around the time the map went stale, for USB disconnects, xHCI errors or
  over-current messages.
- `vcgencmd get_throttled`, for under-voltage. The RSPduo is bus-powered.
- `journalctl -u sdrplay` and `docker logs blah2` from the first failure.
- `journalctl -t blah2-rspduo-watchdog`, to see whether the USB reset ran and what it found.

## Verification

Run on owl-ded9 (owl-os-pi5-v0.20.0-dev, kernel 6.12) on 2026-11-03, comparing the script on
`main` against this one, md5 `b01aa0839642f505c7800b6a9c016cbd`. If the script changes, this
evidence no longer describes it.

**Ordinary faults:** 12 runs per script, 4 per fault, in old/new/new/old order. All 24 recovered.
Time from the watchdog starting to fresh map data:

| Fault | Old mean | New mean |
| --- | --- | --- |
| blah2 stopped | 22.01 s | 21.92 s |
| SDRplay service killed, blah2 crash-looping | 22.05 s | 22.21 s |
| API client holding the RSPduo | 22.39 s | 21.84 s |
| All 12 runs | 22.15 s | 21.99 s |

The difference across all runs is -0.16 s, with an approximate 95% interval of -0.54 s to +0.21 s.
The sampler polls every 2 s, so differences that small are not meaningful.

**Skip conditions:** with stale data, the restart lock, a fresh calibration lock and spectrum mode
each stopped both scripts from acting. On a healthy node, neither acted. SDRconnect mode and a
stale calibration lock are covered by the unit tests only.

**Wedges:** two synthetic wedges, each surviving a stack and service restart:

| Wedge | Old | New |
| --- | --- | --- |
| Another process claims the RSPduo's interface | 0 of 2 recovered | 2 of 2, 24.2 s and 25.1 s after the second run started |
| Device left in USB configuration 0 | 0 of 2 recovered | 2 of 2, 24.1 s and 25.1 s after the second run started |

In all four, the new script's plain restart failed first, and the run with the USB reset fixed it.

**From cron, with no manual steps:**

| Run | Result |
| --- | --- |
| Old script, claim wedge | No fresh data in 19.6 minutes, service restarted at only every other tick |
| New script, device unconfigured | Recovered in 10.3 minutes, then 22 minutes stable with 0 restarts |
| New script, claim wedge | Recovered in 15.3 minutes, one tick lost to the limitation below |
| New script, no fault, 16 minutes | 3 ticks, no action, nothing logged |

**Not covered:**

- Neither wedge reproduces the 2026-10-26 firmware failure (`libusb_control_transfer() -1`), or any
  wedge that survives a reboot. The cause of that incident is still unknown, so recovery from it
  is unproven.
- The service does not log `-6` for the claim wedge at its normal log level, and blah2 reports
  `No devices found` rather than `GetDevices failed`.
- One node and one RSPduo, about 3.5 hours of testing.

## Known limitation

`docker start` resets `RestartCount` on the same container. If blah2 is started that way after
earlier crashes, and it then crash-loops, the count can stay at or below the stored number at the
next check. That check is then skipped by the grace period, and the one after it catches the crash
loop, so the cost is one tick (5 minutes). Recreating the container, as the watchdog and retina-gui
do, is not affected. retina-gui's plain `up -d` repair path could start a stopped blah2 this way.

## What it cannot fix

- **A device that has gone from the bus.** With no SDRplay device to reset, the script logs
  that and carries on restarting the stack. That points to the cable, the power or the device.
- **A wedge that survives both reset steps.** Only removing USB power would clear that, and it
  is not attempted. It would need testing first, since it may drop other USB devices too.
- **blah2 hung with fresh-looking data.** The check only reads the map's timestamp.
