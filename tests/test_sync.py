import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import codex_sync as s


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        s.restrict(self.home)
        self.path = self.home / 'config.toml'
        self.cloud = {'mode': 'api', 'base_url': 'https://example.com/v1', 'api_key': 'fake-key'}
        self.write('# keep header\nmodel = "test" # keep model\n\n[mcp_servers.demo]\ncommand = "demo" # keep mcp\n')

    def write(self, text):
        self.path.write_text(text, encoding='utf8')
        s.restrict(self.path)

    def apply(self, cloud=None):
        return s.apply(self.home, cloud or self.cloud, idle=lambda: None)

    def test_roundtrip_keeps_unrelated_edits_and_auth(self):
        auth = self.home / 'auth.json'
        auth.write_bytes(b'FAKE-CREDENTIAL-SENTINEL')
        before = auth.stat().st_mtime_ns
        self.apply()
        doc = tomlkit.parse(self.path.read_text())
        self.assertEqual(doc['model_provider'], 'lq_sync')
        self.assertTrue(doc['model_providers']['lq_sync']['requires_openai_auth'])
        self.write(self.path.read_text().replace('model = "test"', 'model = "edited"'))
        self.apply({**self.cloud, 'mode': 'pro'})
        text = self.path.read_text()
        self.assertNotIn('model_provider =', text)
        self.assertNotIn('lq_sync', text)
        self.assertIn('model = "edited" # keep model', text)
        self.assertIn('command = "demo" # keep mcp', text)
        self.assertEqual(auth.read_bytes(), b'FAKE-CREDENTIAL-SENTINEL')
        self.assertEqual(auth.stat().st_mtime_ns, before)

    def test_explicit_original_provider(self):
        self.write('model_provider = "openai" # original\n[mcp_servers.a]\ncommand="a"\n')
        self.apply()
        self.apply({**self.cloud, 'mode': 'pro'})
        self.assertIn('model_provider = "openai" # original', self.path.read_text())

    def test_updates_and_noop(self):
        self.apply()
        timestamp = self.path.stat().st_mtime_ns
        self.assertEqual(self.apply(), 'unchanged')
        self.assertEqual(timestamp, self.path.stat().st_mtime_ns)
        self.apply({**self.cloud, 'base_url': 'https://new.example/v1', 'api_key': 'new-fake-key'})
        text = self.path.read_text()
        self.assertIn('new-fake-key', text)
        self.assertIn('https://new.example/v1', text)
        self.assertNotIn('"fake-key"', text)

    def test_invalid_cloud_never_changes_config(self):
        old = self.path.read_bytes()
        for cloud in [{**self.cloud, 'mode': 'bad'}, {**self.cloud, 'api_key': ''}, {**self.cloud, 'base_url': 'http://example.com'}, {**self.cloud, 'extra': 1}, {**self.cloud, 'api_key': 'secret\nheader'}]:
            with self.assertRaises(s.SafeError):
                self.apply(cloud)
            self.assertEqual(old, self.path.read_bytes())

    def test_conflicts(self):
        for text in ['model_provider="other"', '[model_providers.lq_sync]\nname="lq"', '[profiles.x]\nmodel_provider="other"', 'bad = [']:
            self.write(text)
            with self.assertRaises(s.SafeError):
                self.apply()
            self.assertEqual(text, self.path.read_text())

    def test_busy_no_state_created(self):
        old = self.path.read_bytes()
        def busy():
            raise s.SafeError('busy')
        with self.assertRaises(s.SafeError):
            s.apply(self.home, self.cloud, idle=busy)
        self.assertEqual(old, self.path.read_bytes())
        self.assertFalse((self.home / '.codex-sync-state.json').exists())

    def test_external_edit_detected(self):
        expected = s.snapshot(self.path)
        def edit():
            self.write('# external edit')
        with self.assertRaises(s.SafeError):
            s.atomic_write(self.path, b'# replacement', expected, edit)
        self.assertEqual(self.path.read_text(), '# external edit')
        self.assertEqual(list(self.home.glob('.codex-sync-*')), [])

    def test_failed_replace_preserves_config(self):
        old = self.path.read_bytes()
        with patch.object(s.os, 'replace', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(old, self.path.read_bytes())
        self.assertEqual(list(self.home.glob('.codex-sync-*')), [])

    def test_network_error_redacted(self):
        with patch.object(s.urllib.request.OpenerDirector, 'open', side_effect=OSError('SECRET')):
            with self.assertRaises(s.SafeError) as err:
                s.fetch({'url': 'https://example.com', 'username': 'reader', 'password': 'SECRET'})
        self.assertNotIn('SECRET', str(err.exception))

    def test_redirect_refused(self):
        with self.assertRaises(s.SafeError):
            s.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(s.SafeError):
            s.parse_json('{"mode":"api","mode":"pro"}')

    def test_mutex(self):
        with s.lock(self.home):
            with self.assertRaises(s.SafeError):
                with s.lock(self.home):
                    pass

    def test_unknown_process_status_deferred(self):
        with patch.object(s.psutil, 'process_iter', side_effect=RuntimeError()):
            with self.assertRaises(s.SafeError):
                s.ensure_idle()

    def test_state_saved_once(self):
        self.apply()
        path = self.home / '.codex-sync-state.json'
        before = s.snapshot(path)
        self.apply({**self.cloud, 'mode': 'pro'})
        self.apply()
        self.assertEqual(before, s.snapshot(path))


if __name__ == '__main__':
    unittest.main()
