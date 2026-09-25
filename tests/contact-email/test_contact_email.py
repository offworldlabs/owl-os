#!/usr/bin/env python3
"""
Tests for the mender-inventory-retina-contact inventory script.

It runs as root on every node every 600 s and puts text the owner typed into
Mender inventory, so each test runs the real script under /bin/sh against a
contact file written the way retina-gui writes it, or deliberately not.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INVENTORY = os.path.join(REPO, 'configuration', 'mender', 'inventory', 'mender-inventory-retina-contact')


class TestContactInventory(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.contact_file = os.path.join(self.dir, 'telemetry-contact.json')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, text):
        with open(self.contact_file, 'w') as f:
            f.write(text)

    def run_script(self):
        env = dict(os.environ, RETINA_CONTACT_FILE=self.contact_file)
        result = subprocess.run(['/bin/sh', INVENTORY], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout

    def test_an_entered_email_is_reported(self):
        self.write(json.dumps({'email': 'ann@example.com', 'first_name': 'Ann'}))
        self.assertEqual(self.run_script(), 'contact_email=ann@example.com\n')

    def test_a_non_ascii_email_is_reported(self):
        self.write(json.dumps({'email': 'josé@exämple.de'}, ensure_ascii=False))
        self.assertEqual(self.run_script(), 'contact_email=josé@exämple.de\n')

    def test_no_file_reports_nothing(self):
        # retina-gui removes the file once every contact box is empty.
        self.assertEqual(self.run_script(), '')

    def test_contact_details_without_an_email_report_nothing(self):
        self.write(json.dumps({'first_name': 'Ann', 'phone': '+44 20 7946 0000'}))
        self.assertEqual(self.run_script(), '')

    def test_a_newline_cannot_add_attributes(self):
        # retina-gui checks only the length, so this can be saved.
        self.write(json.dumps({'email': 'ann@example.com\nremote_access=true'}))
        self.assertEqual(self.run_script(), '')

    def test_values_that_are_not_one_address_are_dropped(self):
        for email in ('ann smith@example.com', 'a@b@example.com', 'ann@example.com\u0007',
                      'example.com', '@example.com', 'ann@', 42, None):
            with self.subTest(email=email):
                self.write(json.dumps({'email': email}))
                self.assertEqual(self.run_script(), '')

    def test_an_unreadable_file_reports_nothing(self):
        for text in ('{"email": "ann@exa', '["ann@example.com"]', ''):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(self.run_script(), '')


if __name__ == '__main__':
    unittest.main()
