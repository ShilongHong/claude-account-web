import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import AppError
from codex_accounts import CodexAccounts


class CodexAccountTests(unittest.TestCase):
    def test_isolated_login_switch_shared_transcripts_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            native = home / '.codex'
            native.mkdir(parents=True)
            auth = {'tokens': {'access_token': 'private-access', 'refresh_token': 'private-refresh', 'account_id': 'original'}}
            original = json.dumps(auth)
            (native / 'auth.json').write_text(original)
            manager = CodexAccounts(Path(root) / 'data', home)
            local = manager.state()['activeId']
            second = manager.create('工作号')
            p = manager.profile(manager.account(second))
            self.assertFalse((p / 'auth.json').exists())
            self.assertEqual((p / 'sessions').resolve(), native / 'sessions')
            with self.assertRaises(AppError):
                manager.activate(second)
            binary = Path(root) / 'fake-codex'
            binary.write_text('#!/usr/bin/env python3\nimport os,json\nfrom pathlib import Path\nassert os.environ.get("OPENAI_API_KEY") is None\nassert os.environ["CODEX_HOME"] != '+repr(str(native))+'\nPath(os.environ["CODEX_HOME"],"auth.json").write_text(json.dumps({"tokens":{"access_token":"new-private-access","refresh_token":"new-private-refresh"}}))\n')
            binary.chmod(0o700)
            with patch('codex_accounts.shutil.which', return_value=str(binary)), patch.dict(os.environ, {'OPENAI_API_KEY': 'inherited-secret'}):
                job = manager.start_login(second)
                for _ in range(100):
                    if manager.job(job['id']).status != 'running':
                        break
                    time.sleep(.01)
            self.assertEqual(manager.job(job['id']).status, 'completed')
            manager.activate(second)
            self.assertEqual(manager.state()['activeId'], second)
            self.assertNotIn('private-access', json.dumps(manager.state()))
            self.assertEqual((native / 'auth.json').read_text(), original)
            manager.activate(local)
            self.assertEqual(manager.state()['activeId'], local)
            with self.assertRaises(AppError):
                manager.account('../outside')
            with self.assertRaises(AppError):
                manager.create('工作号')
            manager.close()
