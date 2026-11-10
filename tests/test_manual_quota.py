import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_app
from app import AppError
from manual_quota import refresh_quota, run_usage


class ManualQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.ManagerTests()
        self.fixture.setUp()
        self.manager = self.fixture.manager
        self.a = self.manager.create('A')
        self.b = self.manager.create('B')
        for ident in (self.a, self.b):
            profile = self.manager.profile(self.manager.account(ident))
            (profile / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': ident}}))
        self.manager._point_to(self.manager.account(self.a))
        self.cli = self.fixture.root / 'quota-cli'
        self.cli.write_text('''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
p=Path(os.environ['CLAUDE_CONFIG_DIR'])
(p/'invocation.json').write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),'apiKey':os.environ.get('ANTHROPIC_API_KEY')}))
f=p/'.claude.json';d=json.loads(f.read_text())
d['cachedUsageUtilization']={'accountUuid':d['oauthAccount']['accountUuid'],'fetchedAtMs':time.time()*1000,'utilization':{'five_hour':{'utilization':12,'resets_at':'2099-01-01T00:00:00Z'},'seven_day':{'utilization':34,'resets_at':'2099-01-02T00:00:00Z'}}}
f.write_text(json.dumps(d))
print('Current session 12% used',flush=True)
time.sleep(60)
''')
        self.cli.chmod(0o700)
        self.manager.claude = str(self.cli)

    def tearDown(self):
        self.fixture.tearDown()

    def test_only_requested_account_runs_in_home_without_switching_or_model_tools(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'not-for-cli'}):
            result = refresh_quota(self.manager, self.b)
        self.assertTrue(result['ok'])
        self.assertEqual(result['accountId'], self.b)
        self.assertEqual(result['cwd'], str(self.manager.home))
        self.assertEqual(result['quota']['fiveHour']['usedPercent'], 12)
        self.assertEqual(self.manager.active_id(), self.a)
        a = self.manager.profile(self.manager.account(self.a))
        b = self.manager.profile(self.manager.account(self.b))
        self.assertFalse((a/'invocation.json').exists())
        invocation = json.loads((b/'invocation.json').read_text())
        self.assertEqual(invocation['args'], ['/usage', '--safe-mode', '--tools', '', '--ax-screen-reader'])
        self.assertIsNone(invocation['apiKey'])
        self.assertEqual(invocation['cwd'], str(self.manager.home))
        with self.assertRaises(AppError) as limited:
            refresh_quota(self.manager, self.a)
        self.assertEqual(limited.exception.status, 429)

    def test_account_validation_and_inflight_guard(self):
        with self.assertRaises(test_app.app.AppError):
            refresh_quota(self.manager, '../outside')
        self.manager.quota_lock.acquire()
        try:
            with self.assertRaises(AppError) as pending:
                refresh_quota(self.manager, self.b)
            self.assertEqual(pending.exception.status, 409)
        finally:
            self.manager.quota_lock.release()

    def test_usage_is_allowed_even_when_service_disables_nonessential_traffic(self):
        original = self.cli.read_text()
        guard = "assert 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC' not in os.environ\nassert all(os.environ.get(k) == '1' for k in ('DISABLE_TELEMETRY', 'DISABLE_ERROR_REPORTING', 'DISABLE_AUTOUPDATER'))\n"
        self.cli.write_text(original.replace('import json,os,sys,time\n',
                                             'import json,os,sys,time\n' + guard))
        with patch.dict(os.environ, {'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}):
            result = refresh_quota(self.manager, self.b)
        self.assertTrue(result['ok'])
        self.assertEqual(result['accountId'], self.b)
        self.assertEqual(self.manager.active_id(), self.a)

    def test_unknown_trust_prompt_is_not_accepted(self):
        self.cli.write_text('#!/usr/bin/env python3\nimport time\nprint("Enter y/n: trust this folder",flush=True)\ntime.sleep(60)\n')
        with self.assertRaises(AppError) as trust:
            run_usage(self.manager, self.manager.account(self.b), self.manager.home, timeout=2)
        self.assertEqual(trust.exception.status, 409)
        self.assertIn('目录信任', str(trust.exception))

    def test_official_home_trust_prompt_is_confirmed_then_queries_same_account(self):
        original = self.cli.read_text()
        prompt = "import os,sys\nprint('Permission Required: Accessing workspace:',flush=True)\nprint(os.getcwd(),flush=True)\nprint('y. Yes, I trust this folder\\nEnter y/n:',flush=True)\nassert sys.stdin.readline().strip() == 'y'\n"
        self.cli.write_text(original.replace('import json,os,sys,time\n', 'import json,os,sys,time\n'+prompt))
        result = refresh_quota(self.manager, self.b)
        self.assertTrue(result['homeTrustConfirmed'])
        self.assertEqual(result['accountId'], self.b)
        self.assertEqual(self.manager.active_id(), self.a)

    def test_other_directory_cannot_be_automatically_trusted(self):
        other = self.fixture.root / 'other'
        other.mkdir()
        self.cli.write_text('#!/usr/bin/env python3\nimport os,time\nprint("Accessing workspace:\\n"+os.getcwd()+"\\nYes, I trust this folder\\nEnter y/n:",flush=True)\ntime.sleep(60)\n')
        with self.assertRaises(AppError) as trust:
            run_usage(self.manager, self.manager.account(self.b), other, timeout=2)
        self.assertEqual(trust.exception.status, 409)

    def test_first_run_theme_is_completed_before_home_trust(self):
        original = self.cli.read_text()
        prompt = "import sys\nprint('Choose the text style\\nSelect with numbers [1-7]. Then Enter to submit:',flush=True)\nassert sys.stdin.readline().strip() == '3'\n"
        self.cli.write_text(original.replace('import json,os,sys,time\n', 'import json,os,sys,time\n'+prompt))
        result = refresh_quota(self.manager, self.b)
        self.assertTrue(result['ok'])
        self.assertEqual(result['accountId'], self.b)

    def test_known_first_run_security_notes_are_acknowledged(self):
        original = self.cli.read_text()
        prompt = "import sys\nprint('Security notes:\\nPress Enter to continue…',flush=True)\nassert sys.stdin.readline().strip() == ''\n"
        self.cli.write_text(original.replace('import json,os,sys,time\n', 'import json,os,sys,time\n'+prompt))
        self.assertTrue(refresh_quota(self.manager, self.b)['ok'])

    def test_no_fresh_cache_is_failure_and_retains_old_data(self):
        self.cli.write_text('#!/usr/bin/env python3\nprint("No usage available",flush=True)\n')
        profile = self.manager.profile(self.manager.account(self.b))
        before = (profile/'.claude.json').read_text()
        with self.assertRaises(AppError) as missing:
            run_usage(self.manager, self.manager.account(self.b), self.manager.home, timeout=1)
        self.assertEqual(missing.exception.status, 502)
        self.assertEqual((profile/'.claude.json').read_text(), before)

    def test_official_usage_failure_is_reported_without_waiting_for_timeout(self):
        self.cli.write_text('#!/usr/bin/env python3\nimport time\nprint("Error: Failed to load usage data",flush=True)\ntime.sleep(60)\n')
        started = time.monotonic()
        with self.assertRaises(AppError) as failure:
            run_usage(self.manager, self.manager.account(self.b), self.manager.home, timeout=10)
        self.assertEqual(failure.exception.status, 502)
        self.assertIn('官方 CLI 无法加载', str(failure.exception))
        self.assertLess(time.monotonic() - started, 5)
