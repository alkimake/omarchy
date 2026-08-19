#!/usr/bin/python3

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = APP_DIR / "lib"
COLLECTOR = APP_DIR / "bin" / "omarchy-agent-usage-kimi"
sys.path.insert(0, str(LIB_DIR))

from kimi_collector import build_record
from kimi_quota import QuotaResult


class CollectorTest(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    self.kimi_home = self.root / "kimi"
    self.cache_home = self.root / "cache"
    self.env = {
      "HOME": str(self.root / "home"),
      "KIMI_CODE_HOME": str(self.kimi_home),
      "XDG_CACHE_HOME": str(self.cache_home),
      "KIMI_CODING_API_KEY": "synthetic-key",
    }

  def write_usage(self, *, output, append=False):
    path = self.kimi_home / "sessions" / "wd_test" / "session_one" / "agents" / "main" / "wire.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
      "type": "usage.record",
      "model": "kimi-code/k3",
      "usageScope": "turn",
      "usage": {
        "inputOther": 0,
        "output": output,
        "inputCacheRead": 0,
        "inputCacheCreation": 0,
      },
      "time": round(datetime.now(timezone.utc).timestamp() * 1000),
    }
    with path.open("a" if append else "w", encoding="utf-8") as handle:
      handle.write(json.dumps(event) + "\n")

  def successful_quota(self, access_token, env):
    return QuotaResult(limits=[{
      "label": "Weekly (7-day)",
      "percent": 0.25,
      "resetsAt": "2026-08-24T00:00:00Z",
    }])

  def failing_quota(self, access_token, env):
    return QuotaResult(
      status="Kimi limits temporarily unavailable",
      help_text="The Kimi usage service can be retried later.",
      transient=True,
    )

  def test_builds_display_ready_record_with_local_stats_and_limits(self):
    self.write_usage(output=12)

    record = build_record(self.env, quota_fetcher=self.successful_quota)

    self.assertEqual(record["schemaVersion"], 1)
    self.assertEqual(record["id"], "kimi")
    self.assertEqual(record["name"], "Kimi Code")
    self.assertTrue(record["ready"])
    self.assertTrue(record["hasLocalStats"])
    self.assertEqual(record["todayTotalTokens"], 12)
    self.assertEqual(record["limits"][0]["percent"], 0.25)
    self.assertNotIn("synthetic-key", json.dumps(record))

  def test_quota_failure_keeps_local_stats_and_advises_retry(self):
    self.write_usage(output=12)

    record = build_record(self.env, quota_fetcher=self.failing_quota)

    self.assertEqual(record["todayTotalTokens"], 12)
    self.assertEqual(record["limits"], [])
    self.assertTrue(record["retryAdvised"])
    self.assertNotIn("synthetic-key", json.dumps(record))

  def test_missing_auth_still_emits_local_stats(self):
    self.write_usage(output=7)
    env = {**self.env, "KIMI_CODING_API_KEY": "", "KIMI_API_KEY": ""}

    record = build_record(env, quota_fetcher=self.successful_quota)

    self.assertEqual(record["todayTotalTokens"], 7)
    self.assertEqual(record["limits"], [])
    self.assertIn("authentication", record["usageStatusText"].lower())

  def test_limits_only_reuses_recent_cache_but_force_rescans(self):
    self.write_usage(output=12)
    first = build_record(self.env, quota_fetcher=self.successful_quota)
    self.write_usage(output=5, append=True)

    cached = build_record(self.env, limits_only=True, quota_fetcher=self.successful_quota)
    forced = build_record(self.env, force=True, quota_fetcher=self.successful_quota)

    self.assertEqual(cached["todayTotalTokens"], first["todayTotalTokens"])
    self.assertEqual(forced["todayTotalTokens"], first["todayTotalTokens"] + 5)

  def test_executable_prints_json_when_only_local_stats_are_available(self):
    self.write_usage(output=9)
    env = os.environ.copy()
    env.update({**self.env, "KIMI_CODING_API_KEY": "", "KIMI_API_KEY": ""})

    result = subprocess.run(
      [str(COLLECTOR), "--force"],
      capture_output=True,
      text=True,
      env=env,
      timeout=10,
    )

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(result.stdout)["todayTotalTokens"], 9)


if __name__ == "__main__":
  unittest.main()
