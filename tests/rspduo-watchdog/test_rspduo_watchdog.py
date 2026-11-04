#!/usr/bin/env python3
"""
Tests for the RSPduo watchdog (radar_packages/files/blah2_rspduo_restart.bash).

Cron runs it as root every 5 minutes on every node. Each test runs the real
script under bash against stub docker, curl, systemctl, usbreset, pkill, pgrep and sleep,
and a fake /sys/bus/usb/devices. The stubs log every call, with the fake
RSPduo's `authorized` value at that moment, so the tests can check the order of
the recovery steps. See docs/rspduo-watchdog.md.
"""

import datetime
import fcntl
import os
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHDOG = os.path.join(REPO, 'plugins', 'playbooks', 'os_setup', 'roles', 'radar_packages',
                        'files', 'blah2_rspduo_restart.bash')

# Every stub sources this for snap(): the authorized value of each fake device.
SNAP = r'''snap() { for f in "$USB"/*/authorized; do [ -f "$f" ] && printf '%s ' "$(cat "$f")"; done; }
'''

STUBS = {
    'docker': r'''
case "$1" in
    inspect)
        case "$3" in
            *RestartCount*)
                [ -f "$STUB/restart_count" ] || exit 1
                echo "$(cat "$STUB/container_id" 2>/dev/null || echo id1) $(cat "$STUB/restart_count")"
                exit 0 ;;
            *StartedAt*)
                [ -f "$STUB/started_at" ] || exit 1
                cat "$STUB/started_at"; exit 0 ;;
            *) exit 1 ;;
        esac ;;
    compose)
        for a; do last=$a; done
        [ "$last" = -d ] && last=up
        echo "compose $last | $(snap)" >> "$STUB/calls"; exit 0 ;;
esac
exit 0
''',
    'curl': r'''
[ -f "$STUB/map" ] && cat "$STUB/map"
exit 0
''',
    'systemctl': r'''
echo "systemctl $* | $(snap)" >> "$STUB/calls"
if [ "$1" = is-active ]; then
    # active_polls: how many polls still answer "active" before the stop job runs.
    n=$(cat "$STUB/active_polls" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n-1)) > "$STUB/active_polls"; echo active; exit 0; fi
    echo deactivating; exit 3
fi
[ "$1" = start ] && [ -f "$STUB/start_fails" ] && exit 1
exit 0
''',
    'usbreset': r'''
echo "usbreset $* | $(snap)" >> "$STUB/calls"
[ -f "$STUB/usbreset_fails" ] && exit 1
exit 0
''',
    'pkill': r'''
echo "pkill $* | $(snap)" >> "$STUB/calls"
[ -f "$STUB/stuck" ] || rm -f "$STUB/proc"
exit 0
''',
    'pgrep': r'''
[ -f "$STUB/proc" ]
''',
    'sleep': r'''
echo "sleep $1 | $(snap)" >> "$STUB/calls"
exit 0
''',
    'logger': r'''
shift 3
echo "$*" >> "$STUB/log"
exit 0
''',
}


class Watchdog(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stub = os.path.join(self.dir, 'stub')
        self.bin = os.path.join(self.dir, 'bin')
        self.gui = os.path.join(self.dir, 'gui')
        self.run_dir = os.path.join(self.dir, 'run')
        self.usb = os.path.join(self.dir, 'usb')
        for d in (self.stub, self.bin, self.gui, self.run_dir, self.usb):
            os.makedirs(d)
        for name, body in STUBS.items():
            path = os.path.join(self.bin, name)
            self.write(path, '#!/bin/sh\n' + SNAP + body)
            os.chmod(path, 0o755)
        self.recoveries = os.path.join(self.run_dir, 'blah2-rspduo-watchdog.recoveries')
        # The service is running before the watchdog stops it.
        self.write(os.path.join(self.stub, 'proc'), '')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, path, text):
        with open(path, 'w') as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read().strip()

    def add_usb_device(self, port, vendor, product):
        dev = os.path.join(self.usb, port)
        os.makedirs(dev)
        self.write(os.path.join(dev, 'idVendor'), vendor + '\n')
        self.write(os.path.join(dev, 'idProduct'), product + '\n')
        self.write(os.path.join(dev, 'busnum'), '3\n')
        self.write(os.path.join(dev, 'devnum'), '7\n')
        # Not a real sysfs value: it shows which writes the watchdog made.
        self.write(os.path.join(dev, 'authorized'), 'untouched\n')
        return dev

    def map_aged(self, seconds):
        ms = int((time.time() - seconds) * 1000)
        self.write(os.path.join(self.stub, 'map'), '{"timestamp":%d,"delay":[0,1,2]}' % ms)

    def set_recoveries(self, n):
        self.write(self.recoveries, f'{n}\n')

    def run_watchdog(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", STUB=self.stub, USB=self.usb,
                   RSPDUO_WATCHDOG_COMPOSE_FILE=os.path.join(self.dir, 'docker-compose.yaml'),
                   RSPDUO_WATCHDOG_GUI_DIR=self.gui,
                   RSPDUO_WATCHDOG_STATE_DIR=self.run_dir,
                   RSPDUO_WATCHDOG_USB_DEVICES=self.usb,
                   RSPDUO_WATCHDOG_MAP_URL='stub')
        result = subprocess.run(['/bin/bash', WATCHDOG], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout

    def calls(self):
        path = os.path.join(self.stub, 'calls')
        return open(path).read().splitlines() if os.path.exists(path) else []

    def log(self):
        path = os.path.join(self.stub, 'log')
        return open(path).read() if os.path.exists(path) else ''

    def index(self, prefix):
        for i, line in enumerate(self.calls()):
            if line.startswith(prefix):
                return i
        self.fail(f'no call starting {prefix!r} in {self.calls()}')

    def restarted(self):
        return any(c.startswith('compose up') for c in self.calls())


class TestHealthy(Watchdog):

    def test_fresh_data_does_nothing(self):
        self.map_aged(5)
        self.run_watchdog()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.read(self.recoveries), '0')

    def test_fresh_data_clears_earlier_recoveries(self):
        self.map_aged(5)
        self.set_recoveries(3)
        self.run_watchdog()
        self.assertEqual(self.read(self.recoveries), '0')
        self.assertIn('fresh again after 3', self.log())


class TestPlainRecovery(Watchdog):

    def test_first_stale_check_restarts_without_touching_usb(self):
        dev = self.add_usb_device('3-1', '1df7', '3020')
        self.map_aged(600)
        self.run_watchdog()

        self.assertEqual(self.read(os.path.join(dev, 'authorized')), 'untouched')
        self.assertLess(self.index('compose down'), self.index('systemctl stop --no-block sdrplay.service'))
        self.assertLess(self.index('systemctl start sdrplay.service'), self.index('compose up'))
        self.assertEqual(self.read(self.recoveries), '1')

    def test_the_unit_is_stopped_before_the_kill(self):
        # Restart=on-failure would otherwise respawn the service onto the
        # device within a second of the kill.
        self.map_aged(600)
        self.run_watchdog()
        self.assertLess(self.index('systemctl stop --no-block sdrplay.service'),
                        self.index('pkill -9 -f [s]drplay_apiService'))
        self.assertLess(self.index('pkill'), self.index('systemctl reset-failed sdrplay.service'))

    def test_the_kill_waits_for_the_stop_job_to_run(self):
        # Until the unit leaves "active", systemd would still apply Restart=.
        self.write(os.path.join(self.stub, 'active_polls'), '2')
        self.map_aged(600)
        self.run_watchdog()
        calls = self.calls()
        kill = self.index('pkill')
        polls = [i for i, c in enumerate(calls) if c.startswith('systemctl is-active')]
        self.assertEqual(len(polls), 3)
        self.assertEqual(len([c for c in calls[polls[0]:kill] if c.startswith("sleep 0.2")]), 2)
        self.assertLess(polls[-1], kill)

    def test_blah2_api_down_reads_as_stale(self):
        self.run_watchdog()  # no map at all; run_watchdog asserts stderr is empty
        self.assertTrue(self.restarted())
        self.assertIn('no map data: restarting the stack', self.log())

    def test_a_non_json_answer_reads_as_stale(self):
        self.write(os.path.join(self.stub, 'map'), '<html>502 Bad Gateway</html>')
        self.run_watchdog()
        self.assertTrue(self.restarted())

    def test_a_service_that_will_not_die_is_logged_and_recovery_continues(self):
        self.write(os.path.join(self.stub, 'stuck'), '')
        self.map_aged(600)
        self.run_watchdog()
        self.assertIn('survived SIGKILL', self.log())
        self.assertTrue(self.restarted())

    def test_a_failed_service_start_is_logged_and_recovery_continues(self):
        self.write(os.path.join(self.stub, 'start_fails'), '')
        self.map_aged(600)
        self.run_watchdog()
        self.assertIn('systemctl start sdrplay.service failed', self.log())
        self.assertTrue(self.restarted())


class TestUsbReset(Watchdog):

    def test_second_stale_check_reenumerates_the_rspduo(self):
        dev = self.add_usb_device('3-1', '1df7', '3020')
        self.map_aged(600)
        self.set_recoveries(1)
        self.run_watchdog()

        calls = self.calls()
        stop = self.index('systemctl stop --no-block sdrplay.service')
        deauthorised = next(i for i, c in enumerate(calls) if c.startswith('sleep 2 | 0'))
        start = self.index('systemctl start sdrplay.service')

        self.assertLess(self.index('compose down'), stop)
        self.assertTrue(calls[stop].endswith('| untouched '), calls[stop])
        self.assertLess(stop, deauthorised)
        self.assertLess(deauthorised, start)
        self.assertTrue(calls[start].endswith('| 1 '), calls[start])
        self.assertLess(start, self.index('compose up'))
        self.assertEqual(self.read(os.path.join(dev, 'authorized')), '1')
        self.assertEqual(self.read(self.recoveries), '2')
        self.assertIn('resetting SDRplay device 1df7:3020 at USB 3-1', self.log())
        # The bus reset comes after reauthorising, and before the service starts.
        self.assertTrue(calls[self.index('usbreset 003/007')].endswith('| 1 '))
        self.assertLess(self.index('usbreset 003/007'), start)

    def test_a_failed_usbreset_is_logged_and_recovery_continues(self):
        self.add_usb_device('3-1', '1df7', '3020')
        self.write(os.path.join(self.stub, 'usbreset_fails'), '')
        self.map_aged(600)
        self.set_recoveries(1)
        self.run_watchdog()
        self.assertIn('usbreset failed for USB 3-1', self.log())
        self.assertTrue(self.restarted())

    def test_the_first_recovery_never_resets_the_bus(self):
        self.add_usb_device('3-1', '1df7', '3020')
        self.map_aged(600)
        self.run_watchdog()
        self.assertFalse([c for c in self.calls() if c.startswith('usbreset')])

    def test_the_reset_repeats_while_the_data_stays_stale(self):
        dev = self.add_usb_device('3-1', '1df7', '3020')
        self.map_aged(600)
        self.set_recoveries(5)
        self.run_watchdog()
        self.assertEqual(self.read(os.path.join(dev, 'authorized')), '1')
        self.assertEqual(self.read(self.recoveries), '6')

    def test_other_usb_devices_are_left_alone(self):
        self.add_usb_device('3-1', '1df7', '3020')
        hub = self.add_usb_device('usb3', '1d6b', '0002')
        self.map_aged(600)
        self.set_recoveries(1)
        self.run_watchdog()
        self.assertEqual(self.read(os.path.join(hub, 'authorized')), 'untouched')

    def test_no_rspduo_on_the_bus_is_logged_and_recovery_continues(self):
        self.map_aged(600)
        self.set_recoveries(1)
        self.run_watchdog()
        self.assertIn('no SDRplay device on the USB bus', self.log())
        self.assertTrue(self.restarted())

    def test_a_service_stuck_on_the_old_device_is_killed_after_the_reset(self):
        self.add_usb_device('3-1', '1df7', '3020')
        self.write(os.path.join(self.stub, 'stuck'), '')
        self.map_aged(600)
        self.set_recoveries(1)
        self.run_watchdog()
        kills = [c for c in self.calls() if c.startswith('pkill')]
        self.assertEqual(len(kills), 2)
        self.assertTrue(kills[1].endswith('| 1 '), kills[1])
        self.assertIn('still running after the USB reset', self.log())
        self.assertTrue(self.restarted())


class TestSkips(Watchdog):

    def test_a_held_restart_lock_skips_without_counting(self):
        self.map_aged(600)
        self.set_recoveries(1)
        with open(os.path.join(self.gui, 'restart.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_watchdog()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.read(self.recoveries), '1')

    def test_spectrum_mode_skips_and_forgets_earlier_recoveries(self):
        self.map_aged(600)
        self.set_recoveries(3)
        self.write(os.path.join(self.gui, 'mode.txt'), 'spectrum')
        self.run_watchdog()
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.recoveries))

    def test_sdrconnect_mode_skips(self):
        self.map_aged(600)
        self.write(os.path.join(self.gui, 'mode.txt'), 'sdrconnect')
        self.run_watchdog()
        self.assertEqual(self.calls(), [])

    def test_a_fresh_calibration_lock_skips(self):
        self.map_aged(600)
        self.set_recoveries(1)
        self.write(os.path.join(self.gui, 'calibrate.lock'), '{"started_at": 0}')
        self.run_watchdog()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.read(self.recoveries), '1')

    def test_a_stale_calibration_lock_does_not_skip(self):
        self.map_aged(600)
        lock = os.path.join(self.gui, 'calibrate.lock')
        self.write(lock, '{"started_at": 0}')
        old = time.time() - 1300
        os.utime(lock, (old, old))
        self.run_watchdog()
        self.assertTrue(self.restarted())

    def test_a_just_started_blah2_gets_a_grace_period(self):
        self.map_aged(600)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write(os.path.join(self.stub, 'started_at'), now)
        self.run_watchdog()
        self.assertEqual(self.calls(), [])

    def test_a_crash_loop_bypasses_the_grace_period(self):
        self.map_aged(600)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write(os.path.join(self.stub, 'started_at'), now)
        self.write(os.path.join(self.stub, 'restart_count'), '5')
        self.write(os.path.join(self.run_dir, 'blah2-rspduo-watchdog.restarts'), 'id1 3\n')
        self.run_watchdog()
        self.assertTrue(self.restarted())

    def test_a_recreated_container_back_at_the_stored_count_is_a_crash_loop(self):
        # Seen on owl-ded9: the stack was recreated, and Docker's backoff took
        # the new container back to exactly the stored count by the next check.
        self.map_aged(600)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write(os.path.join(self.stub, 'started_at'), now)
        self.write(os.path.join(self.stub, 'restart_count'), '11')
        self.write(os.path.join(self.stub, 'container_id'), 'id2')
        self.write(os.path.join(self.run_dir, 'blah2-rspduo-watchdog.restarts'), 'id1 11\n')
        self.run_watchdog()
        self.assertTrue(self.restarted())

    def test_a_healthy_recreated_container_is_not_a_crash_loop(self):
        self.map_aged(600)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write(os.path.join(self.stub, 'started_at'), now)
        self.write(os.path.join(self.stub, 'restart_count'), '0')
        self.write(os.path.join(self.stub, 'container_id'), 'id2')
        self.write(os.path.join(self.run_dir, 'blah2-rspduo-watchdog.restarts'), 'id1 11\n')
        self.run_watchdog()
        self.assertEqual(self.calls(), [])

    def test_the_same_container_with_an_unchanged_count_gets_the_grace_period(self):
        self.map_aged(600)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write(os.path.join(self.stub, 'started_at'), now)
        self.write(os.path.join(self.stub, 'restart_count'), '3')
        self.write(os.path.join(self.run_dir, 'blah2-rspduo-watchdog.restarts'), 'id1 3\n')
        self.run_watchdog()
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
