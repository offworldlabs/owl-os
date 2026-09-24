#!/usr/bin/env python3
"""
Tests for retina-env-guard (retina-node.service ExecStartPre) and the
mender-inventory-retina-stack inventory script.

Both run as root on every node: the guard at every boot, the inventory script
every 600 s. Each test runs the real script under /bin/sh with a stub docker.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(REPO, 'plugins', 'playbooks', 'os_setup', 'roles', 'radar_data_bootstrap',
                     'files', 'retina-env-guard')
INVENTORY = os.path.join(REPO, 'configuration', 'mender', 'inventory', 'mender-inventory-retina-stack')

STUB_DOCKER = r'''#!/bin/sh
echo "$*" >> "$STUB/calls"
case "$1" in
    compose)
        if [ -f "$STUB/compose_error" ]; then cat "$STUB/compose_error" >&2; exit 1; fi
        exit 0 ;;
    ps)
        for a; do case "$a" in status=*) s=${a#status=} ;; esac; done
        n=$(cat "$STUB/$s" 2>/dev/null || echo 0)
        i=0; while [ "$i" -lt "$n" ]; do echo "c$i"; i=$((i+1)); done
        exit 0 ;;
    inspect)
        [ -f "$STUB/blah2" ] || exit 1
        cat "$STUB/blah2"; exit 0 ;;
esac
exit 0
'''


class Harness(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stub = os.path.join(self.dir, 'stub')
        self.bin = os.path.join(self.dir, 'bin')
        self.manifests = os.path.join(self.dir, 'current', 'manifests')
        for d in (self.stub, self.bin, self.manifests):
            os.makedirs(d)
        docker = os.path.join(self.bin, 'docker')
        self.write(docker, STUB_DOCKER)
        os.chmod(docker, 0o755)
        self.write(os.path.join(self.manifests, 'docker-compose.yaml'), 'services: {}\n')
        self.mode_file = os.path.join(self.dir, 'mode.txt')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, path, text, mode='w'):
        with open(path, mode) as f:
            f.write(text)

    def env(self):
        return dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", STUB=self.stub,
                    RETINA_ENV_GUARD_MANIFESTS=self.manifests,
                    RETINA_STACK_MANIFESTS=self.manifests,
                    RETINA_STACK_MODE_FILE=self.mode_file)

    def run_script(self, script):
        result = subprocess.run(['/bin/sh', script], env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def set_env(self, content):
        self.write(os.path.join(self.manifests, '.env'), content,
                   mode='wb' if isinstance(content, bytes) else 'w')

    def set_stub(self, name, value):
        self.write(os.path.join(self.stub, name), str(value))


class TestEnvGuard(Harness):

    def test_a_nul_filled_env_is_set_aside(self):
        # ret9573ecda: 294 NUL bytes since 2026-08-27, stack down on every boot.
        self.set_env(b'\x00' * 294)

        out = self.run_script(GUARD)

        names = os.listdir(self.manifests)
        self.assertNotIn('.env', names)
        self.assertEqual(len([n for n in names if n.startswith('.env.corrupt-')]), 1)
        self.assertIn('NUL bytes', out)

    def test_an_env_compose_cannot_parse_is_set_aside(self):
        env = os.path.join(self.manifests, '.env')
        self.set_env('BAD LINE\n')
        self.write(os.path.join(self.stub, 'compose_error'), f'failed to read {env}: line 1: unexpected character\n')

        self.assertIn('compose cannot parse it', self.run_script(GUARD))
        self.assertFalse(os.path.exists(env))

    def test_a_good_env_is_left_alone(self):
        self.set_env('RECEIVER_LAT=51.5\n')
        self.assertEqual(self.run_script(GUARD), '')
        self.assertTrue(os.path.exists(os.path.join(self.manifests, '.env')))

    def test_an_unrelated_compose_error_leaves_env_alone(self):
        self.set_env('RECEIVER_LAT=51.5\n')
        self.write(os.path.join(self.stub, 'compose_error'), 'service "x" has neither an image nor a build\n')
        self.run_script(GUARD)
        self.assertTrue(os.path.exists(os.path.join(self.manifests, '.env')))

    def test_no_env_and_no_composition_are_fine(self):
        self.run_script(GUARD)
        shutil.rmtree(os.path.join(self.dir, 'current'))
        self.run_script(GUARD)


class TestInventory(Harness):

    def attrs(self):
        out = self.run_script(INVENTORY)
        return dict(line.split('=', 1) for line in out.split())

    def healthy(self):
        self.set_env('A=1\n')
        self.set_stub('running', 8)
        self.set_stub('restarting', 0)
        self.set_stub('blah2', 'running')

    def test_a_healthy_stack_is_up(self):
        self.healthy()
        self.assertEqual(self.attrs(), {
            'retina_stack': 'up', 'retina_stack_running': '8', 'retina_stack_restarting': '0',
            'retina_blah2': 'running', 'retina_env_ok': 'true', 'retina_compose_ok': 'true',
            'retina_mode': 'radar'})

    def test_nothing_running_is_down(self):
        self.healthy()
        self.set_stub('running', 0)
        self.assertEqual(self.attrs()['retina_stack'], 'down')

    def test_the_ret9573ecda_state_is_reported_degraded(self):
        # Old containers kept alive by Docker's restart policy, blah2 aborting
        # on a zeroed config.yml, and a NUL .env that compose cannot load.
        self.set_env(b'\x00' * 294)
        self.write(os.path.join(self.stub, 'compose_error'), 'failed to read .env\n')
        self.set_stub('running', 4)
        self.set_stub('restarting', 3)
        self.set_stub('blah2', 'restarting')

        attrs = self.attrs()

        self.assertEqual(attrs['retina_stack'], 'degraded')
        self.assertEqual(attrs['retina_env_ok'], 'false')
        self.assertEqual(attrs['retina_compose_ok'], 'false')
        self.assertEqual(attrs['retina_blah2'], 'restarting')

    def test_blah2_stopped_in_spectrum_mode_is_not_degraded(self):
        self.healthy()
        self.set_stub('blah2', 'exited')
        self.write(self.mode_file, 'spectrum')
        attrs = self.attrs()
        self.assertEqual(attrs['retina_stack'], 'up')
        self.assertEqual(attrs['retina_mode'], 'spectrum')

    def test_blah2_stopped_in_radar_mode_is_degraded(self):
        self.healthy()
        self.set_stub('blah2', 'exited')
        self.assertEqual(self.attrs()['retina_stack'], 'degraded')

    def test_a_missing_blah2_is_reported(self):
        self.healthy()
        os.remove(os.path.join(self.stub, 'blah2'))
        self.assertEqual(self.attrs()['retina_blah2'], 'missing')

    def test_no_composition_is_absent(self):
        shutil.rmtree(os.path.join(self.dir, 'current'))
        self.assertEqual(self.attrs(), {'retina_stack': 'absent'})

    def test_every_line_is_key_equals_value(self):
        # mender-updated rejects the whole script's output on a malformed line.
        self.healthy()
        for line in self.run_script(INVENTORY).splitlines():
            key, _, value = line.partition('=')
            self.assertRegex(key, r'^[a-z0-9_]+$')
            self.assertTrue(value)


if __name__ == '__main__':
    unittest.main()
