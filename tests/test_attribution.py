from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_app
from test_usage import line
from account_attribution import (OwnerIndex, atomic_json, handle_hook, install_hooks,
                                 launcher_environment, load_evidence, process_ticks, sync_live_evidence)
from usage_stats import UsageIndex

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ORG = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.ManagerTests()
        self.fixture.setUp()
        self.manager = self.fixture.manager
        self.index = UsageIndex(self.manager, start=False)
        self.a = self.fixture.account("A")
        self.b = self.fixture.account("B")
        self.profiles = {A: self.manager.profile(self.a), B: self.manager.profile(self.b)}
        for owner, profile in self.profiles.items():
            (profile / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": owner}}))
        self.project = self.manager.shared / "project"
        self.project.mkdir()
        self.file = self.project / "parent.jsonl"
        self.old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def tearDown(self):
        self.index.close()
        self.fixture.tearDown()

    def desktop(self, owner=A, suffix="", session="parent"):
        path = self.manager.home / (".config/Claude" + suffix) / "claude-code-sessions" / owner / ORG
        path.mkdir(parents=True, exist_ok=True)
        (path / "local_any.json").write_text(json.dumps({"cliSessionId": session}))

    def context(self, owner=A, run="run-a"):
        return {"owner": owner, "run": run, "pid": os.getpid(), "ticks": process_ticks(os.getpid()),
                "profile": str(self.profiles[owner])}

    def hook(self, event, context=None, **extra):
        handle_hook(self.manager.data, context or self.context(), {
            "hook_event_name": event, "session_id": "parent", "transcript_path": str(self.file), **extra})

    def test_desktop_index_inherits_parent_to_workflow_and_updates_without_reindex(self):
        self.file.write_bytes(line("old", at=self.old))
        child = self.project / "parent/subagents/workflows/wf_one/agent-child.jsonl"
        child.parent.mkdir(parents=True)
        child.write_bytes(line("child", at=self.old))
        self.index.scan()
        self.assertEqual(self.index.report()["accounts"][-1]["requests"], 2)
        self.desktop()
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["accounts"][0]["requests"], 2)
        self.assertEqual(report["accounts"][0]["historicalRequests"], 2)
        self.assertEqual(report["summary"]["requests"], 2)

    def test_scratch_and_legacy_fallbacks_and_conflicting_desktop_metadata(self):
        scratch = self.manager.shared / f"project-scratch-workspaces-{B}-{ORG}-scratch-x"
        scratch.mkdir()
        file = scratch / "scratch.jsonl"
        self.assertEqual(OwnerIndex(self.manager.home).for_file(file), {(B, "scratch-workspace")})
        self.file.write_bytes(json.dumps({"type": "bridge-session", "ownerAccountUuid": A}).encode() + b"\n" + line(at=self.old))
        self.assertEqual(OwnerIndex(self.manager.home).for_file(self.file), {(A, "legacy-session")})
        self.desktop(A)
        self.desktop(B, "-backup")
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["accounts"][-1]["requests"], 1)
        self.assertEqual(report["recent"][0]["attribution"], "conflict")

    def test_account_switch_in_same_session_only_assigns_new_messages_to_new_owner(self):
        self.desktop()
        self.file.write_bytes(line("historical", at=self.old))
        install_hooks(self.profiles[A], self.manager.data)
        self.hook("SessionStart")
        with self.file.open("ab") as stream:
            stream.write(line("new-a"))
        self.hook("Stop")
        self.hook("SessionEnd")
        second = self.context(B, "run-b")
        self.hook("SessionStart", second)
        with self.file.open("ab") as stream:
            stream.write(line("historical", at=self.old) + line("new-b"))
        self.hook("Stop", second)
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["summary"]["requests"], 3)
        self.assertEqual(report["accounts"][0]["requests"], 2)
        self.assertEqual(report["accounts"][0]["historicalRequests"], 1)
        self.assertEqual(report["accounts"][0]["verifiedRequests"], 1)
        self.assertEqual(report["accounts"][1]["requests"], 1)
        self.assertEqual(report["accounts"][1]["verifiedRequests"], 1)
        self.assertNotIn(b"PRIVATE_PROMPT", (self.manager.data / "account-attribution.sqlite3").read_bytes())

    def test_missing_start_and_post_cutoff_messages_do_not_fall_back_to_creator(self):
        self.desktop()
        install_hooks(self.profiles[A], self.manager.data)
        self.file.write_bytes(line())
        self.hook("Stop")
        self.index.scan()
        report = self.index.report()
        self.assertEqual(report["accounts"][0]["requests"], 0)
        self.assertEqual(report["accounts"][-1]["requests"], 1)

    def test_live_sync_collects_flushed_replies_only_with_matching_process_identity(self):
        self.file.write_bytes(b"")
        self.hook("SessionStart")
        with self.file.open("ab") as stream:
            stream.write(line("running-reply"))
        environment = b'CLAUDE_ACCOUNT_WEB_RUN=wrong\0CLAUDE_ACCOUNT_WEB_OWNER=' + A.encode() + b'\0CLAUDE_CONFIG_DIR=' + str(self.profiles[A]).encode()
        with patch.object(Path, "read_bytes", return_value=environment):
            sync_live_evidence(self.manager.data)
        self.assertEqual(load_evidence(self.manager.data)[1], {})
        with patch.object(Path, "read_bytes", return_value=environment.replace(b'RUN=wrong', b'RUN=run-a')):
            sync_live_evidence(self.manager.data)
        self.assertEqual(len(load_evidence(self.manager.data)[1]), 1)

    def test_overlapping_live_runs_invalidate_claims_instead_of_double_attributing(self):
        install_hooks(self.profiles[A], self.manager.data)
        self.file.write_bytes(b"")
        self.hook("SessionStart")
        with self.file.open("ab") as stream:
            stream.write(line("a-before-overlap"))
        self.hook("Stop")
        self.assertEqual(len(load_evidence(self.manager.data)[1]), 1)
        self.hook("SessionStart", self.context(B, "run-b"))
        with self.file.open("ab") as stream:
            stream.write(line("ambiguous"))
        self.hook("Stop", self.context(B, "run-b"))
        self.hook("Stop")
        self.assertEqual(load_evidence(self.manager.data)[1], {})
        self.index.scan()
        self.assertEqual(self.index.report()["accounts"][-1]["requests"], 2)

    def test_child_replay_and_partial_line_are_not_claimed_early(self):
        install_hooks(self.profiles[A], self.manager.data)
        self.file.write_bytes(b"")
        self.hook("SessionStart")
        child = self.project / "parent/subagents/agent-one.jsonl"
        child.parent.mkdir(parents=True)
        child.write_bytes(line("replayed", at=self.old) + line("fresh-child")[:-1])
        self.hook("SubagentStop", agent_transcript_path=str(child))
        self.assertEqual(load_evidence(self.manager.data)[1], {})
        with child.open("ab") as stream:
            stream.write(b"\n")
        self.hook("SubagentStop", agent_transcript_path=str(child))
        self.assertEqual(len(load_evidence(self.manager.data)[1]), 1)
        self.index.scan()
        self.assertEqual(self.index.report()["accounts"][0]["verifiedRequests"], 1)

    def test_hooks_preserve_settings_are_idempotent_and_launcher_pins_identity(self):
        profile = self.profiles[A]
        original = {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}]}}
        atomic_json(profile / "settings.json", original)
        env = launcher_environment(profile, self.manager.data, self.manager.home)
        self.assertEqual(env["CLAUDE_ACCOUNT_WEB_OWNER"], A)
        settings = json.loads((profile / "settings.json").read_text())
        self.assertEqual(settings["permissions"], original["permissions"])
        self.assertEqual(settings["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        install_hooks(profile, self.manager.data)
        self.assertEqual(settings, json.loads((profile / "settings.json").read_text()))
        self.assertEqual((profile / "settings.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list((self.manager.data / "attribution-backups").glob("*.json"))), 1)

    def test_transcript_outside_selected_history_is_rejected(self):
        install_hooks(self.profiles[A], self.manager.data)
        self.hook("SessionStart", transcript_path=str(self.manager.home / "outside.jsonl"))
        self.assertEqual(load_evidence(self.manager.data)[1], {})
