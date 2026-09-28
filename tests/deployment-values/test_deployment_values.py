#!/usr/bin/env python3
"""
Tests for the deployment values the OS image carries for retina-gui.

This repository is public. A secret committed here cannot be rotated away,
because it stays in the history, so the committed defaults must be empty and
the real values must arrive from CI. These tests guard that arrangement, and
the path the values are written to, which is load-bearing for a different
reason: see the note in radar_data_bootstrap.
"""

import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULTS = os.path.join(REPO, 'configuration', 'retina-gui', 'retina-gui.yml')
TASKS = os.path.join(REPO, 'plugins', 'playbooks', 'os_setup', 'roles',
                     'radar_data_bootstrap', 'tasks', 'main.yml')
WORKFLOW = os.path.join(REPO, '.github', 'workflows', 'build_os.yml')
GITIGNORE = os.path.join(REPO, '.gitignore')


def read(path):
    with open(path) as handle:
        return handle.read()


class TestNoCommittedSecrets(unittest.TestCase):

    def test_committed_carto_key_is_empty(self):
        """The whole arrangement rests on this one line. Matched directly
        rather than parsed: owl-os's CI installs pytest and nothing else, and
        one assertion is not worth a dependency."""
        self.assertRegex(read(DEFAULTS), r'(?m)^carto_api_key:\s*""\s*$')

    def test_no_long_token_anywhere_in_the_committed_values(self):
        """An empty default is no use if a key is pasted in beside it."""
        for path in (DEFAULTS, TASKS, WORKFLOW):
            body = read(path)
            self.assertIsNone(
                re.search(r'carto_api_key\s*:\s*[\'"][A-Za-z0-9_-]{16,}', body),
                f'a CARTO key appears to be committed in {path}')

    def test_ci_override_is_ignored_by_git(self):
        """CI writes the real key beside the committed default. If that file
        were ever tracked, the first build would commit the key."""
        self.assertIn('/configuration/retina-gui/retina-gui_custom.yml',
                      read(GITIGNORE))


class TestKeyReachesTheFleet(unittest.TestCase):

    def test_ci_writes_the_override_from_a_secret(self):
        workflow = read(WORKFLOW)
        self.assertIn('secrets.CARTO_API_KEY', workflow)
        self.assertIn('configuration/retina-gui/retina-gui_custom.yml', workflow)

    def test_override_is_preferred_over_the_committed_default(self):
        """with_first_found takes the first that exists, so the CI-written
        file has to be listed above the committed one."""
        tasks = read(TASKS)
        custom = tasks.index('retina-gui_custom.yml')
        committed = tasks.index('retina-gui.yml"')
        self.assertLess(custom, committed)

    def test_key_is_written_to_the_rootfs_not_to_data(self):
        """/data survives an OS update, so a file the image writes there only
        ever reaches a freshly flashed node. /etc is replaced by every A/B
        update, which is what carries this to the existing fleet."""
        tasks = read(TASKS)
        self.assertIn('/etc/retina-gui/carto.env', tasks)
        self.assertNotIn('dest: /data/retina-gui/carto.env', tasks)

    def test_no_key_means_no_file_rather_than_an_empty_one(self):
        """An empty assignment would read as a key that had stopped working."""
        tasks = read(TASKS)
        block = tasks[tasks.index('- name: Write the CARTO basemap key'):]
        self.assertIn("when: carto_api_key | default('') | length > 0",
                      block[:block.index('\n\n')])

    def test_key_file_is_root_only(self):
        tasks = read(TASKS)
        block = tasks[tasks.index('- name: Write the CARTO basemap key'):]
        self.assertIn("mode: '0600'", block[:block.index('\n\n')])


if __name__ == '__main__':
    unittest.main()
