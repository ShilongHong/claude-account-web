from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import test_app
sys.path.insert(0, str(test_app.ROOT))
from codex_stats import parse_quota, quota_report
from source_archive import source_archive
from usage_stats import UsageIndex


def record(kind, payload, at=None):
    return json.dumps({"type": kind, "timestamp": at or datetime.now(timezone.utc).isoformat(),
                       "payload": payload}).encode() + b"\n"


def sample(inp, out, cached=0, at=None, limits=None, last=100, reasoning=0, written=0):
    return record("event_msg", {"type": "token_count", "rate_limits": limits,
                  "info": {"total_token_usage": {"input_tokens": inp, "output_tokens": out,
                            "cached_input_tokens": cached, "reasoning_output_tokens": reasoning, "cache_write_input_tokens": written},
                           "last_token_usage": {"input_tokens": last}}}, at)


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.ManagerTests()
        self.fixture.setUp()
        self.manager = self.fixture.manager
        self.home = self.manager.home / ".codex"
        self.environment = patch.dict("os.environ", {"CODEX_HOME": str(self.home)})
        self.environment.start()
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True)
        self.file = self.sessions / "rollout.jsonl"
        self.header = record("session_meta", {"id": "session-a", "base_instructions": "PRIVATE_PROMPT", "creator_account_id": "not-a-claude-account"}) + record("turn_context", {"model": "gpt-5.6-sol"})
        self.index = UsageIndex(self.manager, start=False)

    def tearDown(self):
        self.index.close()
        self.environment.stop()
        self.fixture.tearDown()

    def test_archive_and_rate_updates_are_deduped_reasoning_is_not_added_twice(self):
        one = sample(1000, 100, 800, reasoning=50)
        self.file.write_bytes(self.header + one + one + sample(1000, 100, 800, reasoning=50) + sample(1500, 200, 1100, reasoning=80))
        archive = self.home / "archived_sessions"
        archive.mkdir()
        (archive / "copy.jsonl").write_bytes(self.header + one)
        self.index.scan()
        report = self.index.report(provider="codex")
        self.assertEqual(report["summary"]["requests"], 2)
        self.assertEqual(report["summary"]["inputTokens"], 400)
        self.assertEqual(report["summary"]["cacheReadTokens"], 1100)
        self.assertEqual(report["summary"]["outputTokens"], 200)
        self.assertEqual(report["summary"]["totalTokens"], 1700)
        self.assertAlmostEqual(report["summary"]["estimatedUsd"], .00604)
        self.assertEqual(report["codexQuality"]["duplicates"], 3)
        self.assertEqual(report["accounts"][0]["id"], "codex-local")
        self.assertEqual(self.index.report(provider="claude")["summary"]["requests"], 0)
        self.assertNotIn(b"PRIVATE_PROMPT", self.index.path.read_bytes())
        self.assertNotIn("not-a-claude-account", json.dumps(report))

    def test_baseline_before_date_range_and_counter_regression(self):
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.file.write_bytes(self.header + sample(1000, 100, at=old) + sample(10, 5) + sample(1200, 150))
        self.index.scan()
        report = self.index.report(30, "codex")
        self.assertEqual(report["summary"]["totalTokens"], 250)
        self.assertEqual(report["summary"]["requests"], 1)
        self.assertEqual(report["codexQuality"]["incomplete"], 1)

    def test_incremental_state_partial_line_replacement_and_deletion(self):
        self.file.write_bytes(self.header + sample(100, 10))
        self.index.scan()
        next_line = record("turn_context", {"model": "gpt-future"}) + sample(150, 20)
        with self.file.open("ab") as stream:
            stream.write(next_line[:-1])
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 1)
        with self.file.open("ab") as stream:
            stream.write(b"\n")
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["summary"]["totalTokens"], 170)
        self.assertEqual(report["summary"]["unpricedRequests"], 1)
        self.file.write_bytes(self.header + sample(30, 10, last=300000))
        self.index.scan()
        self.assertIsNone(self.index.report()["summary"]["estimatedUsd"])
        self.file.unlink()
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 0)

    def test_latest_nonempty_quota_uses_actual_window_length_and_age(self):
        now = datetime.now(timezone.utc).timestamp()
        limits = {"primary": {"used_percent": 45, "window_minutes": 10080, "resets_at": now - 1}, "secondary": None}
        self.file.write_bytes(self.header + sample(100, 10, limits=limits) + sample(100, 10, limits={"primary": None, "secondary": None}))
        self.index.scan()
        windows = self.index.report(provider="codex")["quotas"][0]["windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["label"], "7 天")
        self.assertEqual(windows[0]["usedPercent"], 45)
        self.assertTrue(windows[0]["resetPassed"])
        self.assertTrue(windows[0]["stale"])
        self.assertIsNone(parse_quota({"primary": {}}, now))
        self.assertEqual(quota_report(None)["windows"], [])

    def test_official_gpt6_prices_keep_cache_write_separate_from_input(self):
        for model, expected in [("gpt-6.1-sol", .00196), ("gpt-6-astra", .0101), ("gpt-6-luna", .000101),
                                ("gpt-6.1-terra", .00098), ("gpt-5.6-terra", .00222), ("gpt-5.6-luna", .000222)]:
            with self.subTest(model=model):
                self.file.unlink(missing_ok=True)
                self.index.scan()
                self.file.write_bytes(record("session_meta", {"id": "prices"}) +
                    record("turn_context", {"model": model}) + sample(1000, 100, 600, written=200, last=1000))
                self.index.scan()
                summary = self.index.report(provider="codex")["summary"]
                self.assertEqual(summary["totalTokens"], 1100)
                self.assertEqual(summary["inputTokens"], 200)
                self.assertEqual(summary["cacheWriteTokens"], 200)
                self.assertAlmostEqual(summary["estimatedUsd"], expected)
                self.assertAlmostEqual(sum(summary["costBreakdown"].values()), expected)

    def test_context_threshold_changes_full_request_rates_and_ambiguous_gaps_stay_unpriced(self):
        header = record("session_meta", {"id": "context"}) + record("turn_context", {"model": "gpt-6.1-sol"})
        for inp, last, expected in [(272000, 272000, .545), (272001, 272001, 1.089504), (300000, 1000, None)]:
            with self.subTest(input=inp, last=last):
                self.file.write_bytes(header + sample(inp, 100, last=last))
                self.index.scan()
                result = self.index.report(provider="codex")["summary"]["estimatedUsd"]
                if expected is None:
                    self.assertIsNone(result)
                else:
                    self.assertAlmostEqual(result, expected)

    def test_old_numeric_cache_is_rebuilt_and_missing_write_counts_are_unpriced(self):
        import sqlite3
        with sqlite3.connect(self.index.path) as db:
            db.execute("CREATE TABLE codex_files(path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, offset INTEGER, size INTEGER, mtime INTEGER, session TEXT, model TEXT, invalid INTEGER)")
        self.file.write_bytes(self.header + sample(1000, 100, written=200, last=1000))
        self.index.scan()
        self.assertEqual(self.index.report(provider="codex")["summary"]["cacheWriteTokens"], 200)
        raw = json.loads(sample(1000, 100, last=1000))
        del raw["payload"]["info"]["total_token_usage"]["cache_write_input_tokens"]
        self.file.write_bytes(self.header + json.dumps(raw).encode() + b"\n")
        self.index.scan()
        self.assertIsNone(self.index.report(provider="codex")["summary"]["estimatedUsd"])

    def test_mixed_provider_totals_and_source_offer_excludes_runtime_files(self):
        self.file.write_bytes(self.header + sample(100, 10))
        from test_usage import line
        (self.manager.shared / "claude.jsonl").write_bytes(line())
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 2)
        self.assertEqual(self.index.report(provider="claude")["summary"]["requests"], 1)
        self.assertEqual(self.index.report(provider="codex")["summary"]["requests"], 1)
        with self.assertRaises(test_app.app.AppError):
            self.manager.usage_report(provider="invalid")
        with ZipFile(BytesIO(source_archive(test_app.ROOT))) as archive:
            names = archive.namelist()
            self.assertIn("claude-accounts-web/LICENSE", names)
            self.assertIn("claude-accounts-web/codex_stats.py", names)
            self.assertTrue(all(not any(part in n for part in ("artifacts/", "credentials", "access-password", "sqlite", "__pycache__")) for n in names))
