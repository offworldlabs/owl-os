#!/usr/bin/env python3
"""
Tests for owl-os's fork of the mender-docker-compose Update Module.

The module runs every retina-node install on every node. Each test runs the
real module under /bin/sh, pointed through its own config file at a temporary
store, a stub docker that records every call in order, and a stub jq. tar is
real, and the artifact payload is a real images.tar.gz and manifests.tar.

The fork's contract, which upstream does not meet:
  * nothing touches the running stack until the new images are loaded;
  * an install that stops before that point rolls back without stopping it;
  * the new composition starts with the node's own .env;
  * an image that is already gone does not fail cleanup.
"""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO, 'plugins', 'playbooks', 'board_support', 'roles', 'mender',
                      'files', 'update-modules', 'docker-compose')

STUB_DOCKER = r'''#!/bin/sh
echo "$(basename "$(pwd)")/$(basename "$(dirname "$(pwd)")") | $*" >> "$STUB/calls"
case "$1" in
    --version|version) exit 0 ;;
    image)
        case "$2" in
            load)
                # What the RSPduo watchdog's `flock -n` would see right now.
                { flock -n "$LOCK" true && echo free || echo busy; } >> "$STUB/lock_probe"
                [ -f "$STUB/load_fails" ] && exit 1; exit 0 ;;
            inspect) [ -f "$STUB/gone/$3" ] && exit 1; exit 0 ;;
        esac ;;
    images)
        for last; do :; done
        printf '"%s"\n' "$(echo "$last" | md5sum | cut -c1-12)"; exit 0 ;;
    rmi) [ -f "$STUB/rmi_fails" ] && exit 1; exit 0 ;;
    inspect) echo "running:no_check"; exit 0 ;;
    compose)
        case "$*" in
            *" down"*) [ -f "$STUB/down_fails" ] && exit 1; exit 0 ;;
            *" up "*) [ -f "$STUB/up_fails" ] && exit 1; exit 0 ;;
            *" ps "*) echo c1; exit 0 ;;
            *) exit 0 ;;
        esac ;;
esac
exit 0
'''

STUB_JQ = r'''#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ['--version']:
    sys.exit(0)
path = sys.argv[-1].lstrip('.').split('.')
value = json.load(sys.stdin)
for key in path:
    value = value.get(key) if isinstance(value, dict) else None
print('null' if value is None else value)
'''

MANIFEST = b'''services:
  blah2:
    image: ghcr.io/offworldlabs/blah2:v0.6.0
    container_name: blah2
'''


class ModuleHarness(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stub = os.path.join(self.dir, 'stub')
        self.store = os.path.join(self.dir, 'mender-docker-compose')
        self.files = os.path.join(self.dir, 'tree')
        os.makedirs(os.path.join(self.stub, 'gone'))
        os.makedirs(self.store)
        for d in ('tmp', 'header', 'files'):
            os.makedirs(os.path.join(self.files, d))

        docker = self.executable('docker', STUB_DOCKER)
        jq = self.executable('jq', STUB_JQ)
        self.config = os.path.join(self.dir, 'mender-docker-compose.conf')
        self.write(self.config, f'PERSISTENT_STORE="{self.store}"\nDOCKER_CMD="{docker}"\n'
                                f'JQ_CMD="{jq}"\nWAIT_TIMEOUT=2\n'
                                f'RESTART_LOCK="{self.dir}/restart.lock"\nRESTART_LOCK_WAIT=1\n')

        self.write(os.path.join(self.files, 'header', 'meta-data'),
                   json.dumps({'project_name': 'retina-node', 'version': 1}))
        self.write(os.path.join(self.files, 'header', 'header-info'),
                   json.dumps({'artifact_provides': {'artifact_name': 'retina-node-vTEST'}}))
        self.payload()

    def tearDown(self):
        shutil.rmtree(self.dir)

    # --- fixtures -------------------------------------------------------------------

    def write(self, path, text, mode='w'):
        with open(path, mode) as f:
            f.write(text)

    def executable(self, name, text):
        path = os.path.join(self.dir, name)
        self.write(path, text)
        os.chmod(path, 0o755)
        return path

    def payload(self):
        def add(tar, name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with tarfile.open(os.path.join(self.files, 'files', 'images.tar.gz'), 'w:gz') as tar:
            add(tar, 'images/blah2.tar', b'image one')
            add(tar, 'images/tar1090.tar', b'image two')
        with tarfile.open(os.path.join(self.files, 'files', 'manifests.tar'), 'w') as tar:
            add(tar, 'manifests/docker-compose.yaml', MANIFEST)

    def composition(self, slot, env=None, image_ids=''):
        manifests = os.path.join(self.store, slot, 'manifests')
        os.makedirs(manifests)
        self.write(os.path.join(manifests, 'docker-compose.yaml'), MANIFEST.decode())
        self.write(os.path.join(self.store, slot, 'project_name'), 'retina-node\n')
        self.write(os.path.join(self.store, slot, 'image_ids'), image_ids)
        if env is not None:
            self.write(os.path.join(manifests, '.env'), env, mode='wb' if isinstance(env, bytes) else 'w')

    def flag(self, name):
        self.write(os.path.join(self.stub, name), '')

    def run_state(self, state):
        # parse_metadata calls `jq` by name rather than through $JQ_CMD.
        env = dict(os.environ, STUB=self.stub, MENDER_DOCKER_COMPOSE_CONFIG_FILE=self.config,
                   LOCK=os.path.join(self.dir, 'restart.lock'),
                   PATH=f"{self.dir}:{os.environ['PATH']}")
        return subprocess.run(['/bin/sh', MODULE, state, self.files],
                              env=env, capture_output=True, text=True)

    def calls(self):
        path = os.path.join(self.stub, 'calls')
        with open(path) as f:
            return f.read().splitlines()

    def slots(self):
        return sorted(os.listdir(self.store))

    def index(self, fragment):
        for i, call in enumerate(self.calls()):
            if fragment in call:
                return i
        self.fail(f'no call containing {fragment!r} in {self.calls()}')

    def has_call(self, fragment):
        return any(fragment in c for c in self.calls())


class TestInstallOrder(ModuleHarness):

    def test_images_are_loaded_before_the_running_stack_is_stopped(self):
        self.composition('current', env='RECEIVER_LAT=51.5\n')

        result = self.run_state('ArtifactInstall')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        last_load = max(i for i, c in enumerate(self.calls()) if 'image load' in c)
        self.assertLess(last_load, self.index(' down'))
        self.assertLess(self.index(' down'), self.index(' up --detach'))
        self.assertEqual(self.slots(), ['new', 'previous'])

    def test_down_runs_from_the_old_composition_and_up_from_the_new(self):
        self.composition('current', env='A=1\n')
        self.run_state('ArtifactInstall')
        self.assertIn('manifests/previous |', self.calls()[self.index(' down')])
        self.assertIn('manifests/new |', self.calls()[self.index(' up --detach')])


class TestSeedEnv(ModuleHarness):

    def test_the_new_composition_starts_with_the_current_env(self):
        self.composition('current', env='RECEIVER_LAT=42.2\nADSBLOL_ENABLED=true\n')
        self.run_state('ArtifactInstall')
        with open(os.path.join(self.store, 'new', 'manifests', '.env')) as f:
            self.assertEqual(f.read(), 'RECEIVER_LAT=42.2\nADSBLOL_ENABLED=true\n')

    def test_a_nul_filled_env_is_not_carried_forward(self):
        self.composition('current', env=b'\x00' * 208)
        self.run_state('ArtifactInstall')
        self.assertFalse(os.path.exists(os.path.join(self.store, 'new', 'manifests', '.env')))

    def test_a_first_install_needs_no_env(self):
        result = self.run_state('ArtifactInstall')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.slots(), ['new'])
        self.assertFalse(self.has_call(' down'))


class TestFailureBeforeTheStop(ModuleHarness):
    """A load failure, or a power cut during extract or load."""

    def test_a_load_failure_leaves_the_running_stack_alone(self):
        self.composition('current', env='A=1\n')
        self.flag('load_fails')

        result = self.run_state('ArtifactInstall')

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.has_call(' down'))
        self.assertEqual(self.slots(), ['current', 'new'])

    def test_rollback_does_not_stop_the_live_stack(self):
        # The state a failed load, or a power cut during load, leaves behind.
        self.composition('current', env='A=1\n', image_ids='oldid\n')
        self.composition('new', image_ids='newid\n')

        result = self.run_state('ArtifactRollback')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.stub, 'calls')) and self.has_call(' down'))
        self.assertEqual(self.slots(), ['cleanup', 'current'])
        self.assertIn('leaving it running', result.stdout)

    def test_cleanup_after_that_rollback_keeps_the_live_images(self):
        self.composition('current', image_ids='sharedid\noldid\n')
        self.composition('cleanup', image_ids='sharedid\nnewid\n')

        result = self.run_state('Cleanup')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.has_call('rmi newid'))
        self.assertFalse(self.has_call('rmi sharedid'))
        self.assertEqual(self.slots(), ['current'])


class TestRestartLock(ModuleHarness):

    def test_the_watchdog_is_locked_out_while_images_load(self):
        # Josh Test Node 2, 2026-09-24 10:00:03: the watchdog restarted the
        # stack in the middle of the image load.
        self.composition('current', env='A=1\n')

        result = self.run_state('ArtifactInstall')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(self.stub, 'lock_probe')) as f:
            self.assertEqual(set(f.read().split()), {'busy'})

    def test_a_lock_held_elsewhere_fails_the_install_before_anything_is_touched(self):
        import fcntl
        self.composition('current', env='A=1\n')
        holder = open(os.path.join(self.dir, 'restart.lock'), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            install = self.run_state('ArtifactInstall')
            self.assertNotEqual(install.returncode, 0)
            self.assertIn('could not take', install.stderr)
            self.assertEqual(self.slots(), ['current', 'new'])

            rollback = self.run_state('ArtifactRollback')
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            self.assertEqual(self.slots(), ['cleanup', 'current'])
        finally:
            holder.close()
        self.assertFalse(os.path.exists(os.path.join(self.stub, 'calls')) and self.has_call(' down'))


class TestEarlyFailure(ModuleHarness):

    def test_a_corrupt_download_never_takes_the_live_stack_down(self):
        # With neither new/ nor previous/, rollback would read this as a
        # rollback after a commit and stop and move the live composition.
        self.composition('current', env='A=1\n', image_ids='oldid\n')
        self.write(os.path.join(self.files, 'files', 'images.tar.gz'), b'not a tarball', mode='wb')

        install = self.run_state('ArtifactInstall')
        self.assertNotEqual(install.returncode, 0)

        rollback = self.run_state('ArtifactRollback')

        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertNotIn('Rolling back after a Commit', rollback.stdout)
        self.assertEqual(self.slots(), ['cleanup', 'current'])
        self.assertFalse(os.path.exists(os.path.join(self.stub, 'calls')) and self.has_call(' down'))


class TestFailureAfterTheStop(ModuleHarness):
    """Unchanged from upstream: the new stack fails to start."""

    def test_install_that_fails_to_start_rolls_back_to_the_old_stack(self):
        self.composition('current', env='A=1\n')
        self.flag('up_fails')

        install = self.run_state('ArtifactInstall')
        self.assertNotEqual(install.returncode, 0)
        self.assertEqual(self.slots(), ['new', 'previous'])

        os.remove(os.path.join(self.stub, 'up_fails'))
        rollback = self.run_state('ArtifactRollback')

        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertEqual(self.slots(), ['cleanup', 'current'])
        self.assertIn('manifests/current |', self.calls()[-3] + self.calls()[-2] + self.calls()[-1])

    def test_rollback_after_a_commit_is_unchanged(self):
        self.composition('current', image_ids='newid\n')
        self.composition('cleanup', image_ids='oldid\n')

        result = self.run_state('ArtifactRollback')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Rolling back after a Commit', result.stdout)
        self.assertEqual(self.slots(), ['cleanup', 'current'])


class TestCleanup(ModuleHarness):

    def test_an_image_already_gone_does_not_fail_cleanup(self):
        # Fairforest B, retina-node v0.4.3.0: the same ID listed twice.
        self.composition('current', image_ids='keepid\n')
        self.composition('cleanup', image_ids='dupid\ndupid\n')
        open(os.path.join(self.stub, 'gone', 'dupid'), 'w').close()

        result = self.run_state('Cleanup')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.has_call('rmi dupid'))
        self.assertIn('already removed', result.stdout)

    def test_a_real_removal_failure_still_fails_cleanup(self):
        self.composition('current', image_ids='keepid\n')
        self.composition('cleanup', image_ids='stuckid\n')
        self.flag('rmi_fails')

        result = self.run_state('Cleanup')

        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
