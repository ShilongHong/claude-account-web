from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
import unittest

import test_app
ROOT = test_app.ROOT
sys.path.insert(0, str(ROOT))
from usage_stats import UsageIndex, parse_entry, price, quota_for, resolve_period


def line(ident="message-1", request="request-1", output=10, at=None, **changes):
    record = {"type": "assistant", "uuid": "uuid-" + ident, "requestId": request,
              "timestamp": at or datetime.now(timezone.utc).isoformat(),
              "message": {"id": ident, "model": "claude-opus-5-5", "stop_reason": "end_turn",
                          "content": [{"text": "PRIVATE_PROMPT_MUST_NOT_APPEAR"}],
                          "usage": {"input_tokens": 100, "output_tokens": output,
                                    "cache_creation_input_tokens": 20, "cache_read_input_tokens": 200}}}
    record.update(changes)
    return json.dumps(record).encode() + b"\n"


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.ManagerTests()
        self.fixture.setUp()
        self.manager = self.fixture.manager
        self.index = UsageIndex(self.manager, start=False)
        self.project = self.manager.shared / "project"
        self.project.mkdir()

    def tearDown(self):
        self.index.close()
        self.fixture.tearDown()

    def test_billing_boundaries_handle_short_month_leap_year_and_year_rollover(self):
        self.assertEqual(resolve_period(period='current', billing_day=31, today=date(2026, 3, 30)),
                         (date(2026, 2, 28), date(2026, 3, 30)))
        self.assertEqual(resolve_period(period='previous', billing_day=31, today=date(2024, 3, 31)),
                         (date(2024, 2, 29), date(2024, 3, 30)))
        self.assertEqual(resolve_period(period='previous', billing_day=15, today=date(2026, 1, 8)),
                         (date(2025, 11, 15), date(2025, 12, 14)))
        self.assertEqual(resolve_period(period='month', today=date(2026, 11, 8)),
                         (date(2026, 11, 1), date(2026, 11, 8)))
        for kwargs in [dict(start='bad', end='bad'), dict(billing_day=0), dict(billing_day=True),
                       dict(start='2026-11-09', end='2026-11-08'), dict(start='2026-11-08', end='2026-11-09'),
                       dict(start='2000-01-01', end='2026-11-08')]:
            with self.assertRaises(ValueError):
                resolve_period(today=date(2026, 11, 8), **kwargs)

    def test_custom_range_is_inclusive_and_all_details_match_selected_total(self):
        now = datetime.now().astimezone()
        day = now - timedelta(days=2)
        (self.project / 'range.jsonl').write_bytes(
            line('before', at=(day-timedelta(days=1)).isoformat()) +
            line('selected', at=day.isoformat()) + line('after', at=now.isoformat()))
        self.index.scan()
        report = self.index.report(start=day.date().isoformat(), end=day.date().isoformat())
        self.assertEqual(report['summary']['requests'], 1)
        self.assertEqual(sum(r['requests'] for r in report['hourly']), 1)
        self.assertEqual(sum(r['requests'] for r in report['models']), 1)
        self.assertEqual(sum(r['requests'] for r in report['accounts']), 1)
        self.assertEqual(len(report['recent']), 1)
        self.assertAlmostEqual(sum(report['summary']['costBreakdown'].values()), report['summary']['estimatedUsd'])
        self.assertEqual(report['overview']['7']['requests'], 3)
        self.manager.usage = self.index
        self.assertEqual(self.manager.usage_report(start=day.date().isoformat(), end=day.date().isoformat())['summary']['requests'], 1)

    def test_replay_dedupe_preserves_complete_usage_and_original_date(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        (self.project / "original.jsonl").write_bytes(line(output=1, at=yesterday))
        (self.project / "replay.jsonl").write_bytes(line(output=10, at=yesterday) + line(output=10))
        self.index.scan()
        report = self.index.report(7)
        self.assertEqual(report["summary"]["requests"], 1)
        self.assertEqual(report["summary"]["outputTokens"], 10)
        self.assertEqual(report["index"]["duplicates"], 2)
        self.assertEqual(report["overview"]["1"]["requests"], 0)
        self.assertEqual(report["accounts"][-1]["requests"], 1)
        self.assertAlmostEqual(report["summary"]["estimatedUsd"], .00074)

    def test_incremental_append_partial_line_truncate_and_delete(self):
        file = self.project / "session.jsonl"
        file.write_bytes(line("one"))
        self.index.scan()
        first = self.index.report()["summary"]["requests"]
        self.assertEqual(first, 1)
        with file.open("ab") as stream:
            stream.write(line("two")[:-1])
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 1)
        with file.open("ab") as stream:
            stream.write(b"\n")
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 2)
        file.write_bytes(line("three"))
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 1)
        file.unlink()
        self.index.scan()
        self.assertEqual(self.index.report()["summary"]["requests"], 0)

    def test_stats_never_assign_shared_history_to_current_account_or_store_content(self):
        item = self.fixture.account(signed=True)
        self.manager._point_to(item)
        (self.project / "session.jsonl").write_bytes(line())
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["accounts"][0]["requests"], 0)
        self.assertEqual(report["accounts"][-1]["requests"], 1)
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(report))
        self.assertNotIn(b"PRIVATE_PROMPT", self.index.path.read_bytes())
        self.assertEqual(self.index.path.stat().st_mode & 0o777, 0o600)

    def test_explicit_account_id_is_used_and_duplicate_request_ids_are_distinct(self):
        item = self.fixture.account()
        profile = self.manager.profile(item)
        (profile / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner-1"}}))
        (self.project / "session.jsonl").write_bytes(line(accountUuid="owner-1") + line(request="request-2"))
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["summary"]["requests"], 2)
        self.assertEqual(sum(h["requests"] for h in report["hourly"]), 2)
        self.assertEqual(report["accounts"][0]["requests"], 1)
        self.assertEqual(report["accounts"][-1]["requests"], 1)

    def test_unknown_models_do_not_get_a_fabricated_price_and_missing_dates_are_reported(self):
        raw = json.loads(line())
        raw["message"]["model"] = "claude-future-99"
        (self.project / "session.jsonl").write_bytes(json.dumps(raw).encode() + b"\n" + line("undated", timestamp="bad") + b'{"type":"assistant","usage":bad}\n')
        self.index.scan()
        report = self.index.report()
        self.assertIsNone(report["summary"]["estimatedUsd"])
        self.assertEqual(report["summary"]["unpricedRequests"], 1)
        self.assertEqual(report["index"]["undatedRecords"], 1)
        self.assertEqual(report["index"]["invalidLines"], 1)

    def test_quota_filters_identity_and_marks_passed_reset_and_missing_values(self):
        item = self.fixture.account()
        profile = self.manager.profile(item)
        now = time.time()
        config = {"oauthAccount": {"accountUuid": "owner-1"}, "cachedUsageUtilization": {
            "accountUuid": "owner-1", "fetchedAtMs": (now - 120) * 1000,
            "utilization": {"five_hour": {"utilization": 92, "resets_at": datetime.fromtimestamp(now - 60, timezone.utc).isoformat()}}}}
        path = profile / ".claude.json"
        path.write_text(json.dumps(config))
        quota = quota_for(item, profile, self.manager.home, now)
        self.assertTrue(quota["fiveHour"]["stale"])
        self.assertTrue(quota["fiveHour"]["resetPassed"])
        self.assertIsNone(quota["sevenDay"])
        config["cachedUsageUtilization"]["accountUuid"] = "another-owner"
        path.write_text(json.dumps(config))
        self.assertIsNone(quota_for(item, profile, self.manager.home, now)["fiveHour"])

    def test_quota_matches_desktop_organization_and_prefers_new_sample(self):
        item = self.fixture.account()
        profile = self.manager.profile(item)
        now = time.time()
        (profile / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner", "organizationUuid": "my-org"}}))
        desktop = self.manager.home / ".config/Claude"
        desktop.mkdir(parents=True)
        (desktop / "plan-usage-history.json").write_text(json.dumps({"samples": [
            {"t": (now - 30) * 1000, "org": "other-org", "u": {"fh": 99, "sd": 99}},
            {"t": (now - 60) * 1000, "org": "my-org", "u": {"fh": 12.5}}]}))
        quota = quota_for(item, profile, self.manager.home, now)
        self.assertEqual(quota["fiveHour"]["usedPercent"], 12.5)
        self.assertIsNone(quota["sevenDay"])
        self.assertEqual(quota["fiveHour"]["source"], "desktop")

    def test_hour_cache_fast_and_long_haiku_pricing(self):
        entry = {"model": "claude-opus-5-5", "inputTokens": 1000000,
                 "outputTokens": 1000000, "cacheWriteTokens": 1000000,
                 "cacheWrite1hTokens": 1000000, "cacheReadTokens": 1000000,
                 "speed": "fast", "tier": "standard", "geo": "us"}
        self.assertAlmostEqual(price(entry), (4 + 20 + 8 + .2) * 2 * 1.1)
        entry.update(model="claude-haiku-5-5", speed="standard", geo="global")
        self.assertAlmostEqual(price(entry), .5 + 2.5 + 1 + .05)


if __name__ == "__main__":
    unittest.main()
