#!/usr/bin/python3

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from kimi_usage import empty_stats, scan_usage


def usage_event(
  model,
  when,
  *,
  input_other=0,
  output=0,
  cache_read=0,
  cache_creation=0,
  usage_scope="turn",
):
  event = {
    "type": "usage.record",
    "model": model,
    "usageScope": usage_scope,
    "usage": {
      "inputOther": input_other,
      "output": output,
      "inputCacheRead": cache_read,
      "inputCacheCreation": cache_creation,
    },
  }
  if when is not None:
    event["time"] = round(when.timestamp() * 1000)
  return event


class ScanUsageTest(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    self.now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)

  def wire_path(self, session, agent):
    path = self.root / "sessions" / "wd_test" / session / "agents" / agent / "wire.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

  def write_wire(self, session, agent, events):
    path = self.wire_path(session, agent)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path

  def write_raw(self, session, agent, lines):
    path = self.wire_path(session, agent)
    path.write_text("".join(lines), encoding="utf-8")
    return path

  def test_aggregates_models_days_sessions_and_token_classes(self):
    self.write_wire("session_one", "main", [
      usage_event(
        "kimi-code/k3",
        self.now,
        input_other=10,
        output=3,
        cache_read=20,
        cache_creation=2,
      ),
      usage_event("kimi-code/k3", self.now - timedelta(days=1), output=5),
    ])
    self.write_wire("session_two", "agent-1", [
      usage_event("kimi-code/k2.5", self.now, input_other=7, output=1),
    ])

    result = scan_usage(self.root, now=self.now)

    self.assertEqual(result["todayPrompts"], 2)
    self.assertEqual(result["todaySessions"], 2)
    self.assertEqual(result["todayTotalTokens"], 43)
    self.assertEqual(result["todayTokensByModel"], {
      "kimi-code/k2.5": 8,
      "kimi-code/k3": 35,
    })
    self.assertEqual(result["totalPrompts"], 3)
    self.assertEqual(result["totalSessions"], 2)
    self.assertEqual(result["activeDays"], 2)
    self.assertEqual(result["modelUsage"]["kimi-code/k3"], {
      "inputTokens": 10,
      "outputTokens": 8,
      "cacheReadInputTokens": 20,
      "cacheCreationInputTokens": 2,
    })
    self.assertEqual(result["recentDays"][-1]["messageCount"], 43)
    self.assertEqual(result["recentDays"][-2]["messageCount"], 5)

  def test_ignores_malformed_non_turn_and_zero_usage_records(self):
    self.write_raw("session_one", "main", [
      "not json\n",
      json.dumps(usage_event("kimi-code/k3", self.now, output=9, usage_scope="session")) + "\n",
      json.dumps(usage_event("", self.now, input_other=-4)) + "\n",
      json.dumps({"type": "usage.record", "usageScope": "turn", "usage": []}) + "\n",
      json.dumps({"type": "context.append_loop_event", "usage": {"output": 8}}) + "\n",
    ])

    self.assertEqual(scan_usage(self.root, now=self.now), empty_stats(self.now.astimezone().date()))

  def test_falls_back_to_file_mtime_for_missing_timestamp(self):
    path = self.write_wire("session_one", "main", [
      usage_event("kimi-code/k3", None, output=4),
    ])
    fallback = (self.now - timedelta(days=1)).timestamp()
    os.utime(path, (fallback, fallback))

    result = scan_usage(self.root, now=self.now)

    self.assertEqual(result["recentDays"][-2]["messageCount"], 4)
    self.assertEqual(result["recentDays"][-1]["messageCount"], 0)

  def test_uses_default_model_and_clamps_fractional_or_invalid_counters(self):
    self.write_wire("session_one", "main", [
      usage_event("", self.now, input_other="2.6", output="bad", cache_read=None),
    ])

    result = scan_usage(self.root, now=self.now)

    self.assertEqual(result["todayTokensByModel"], {"kimi": 3})
    self.assertEqual(result["modelUsage"]["kimi"]["inputTokens"], 3)


if __name__ == "__main__":
  unittest.main()
