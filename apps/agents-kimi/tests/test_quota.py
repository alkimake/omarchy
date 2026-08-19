#!/usr/bin/python3

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from kimi_quota import fetch_quota, normalize_usage


class FakeResponse:
  def __init__(self, payload, raw=False):
    self.body = payload if raw else json.dumps(payload).encode()

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, traceback):
    return False

  def read(self):
    return self.body


class RecordingOpener:
  def __init__(self, payload=None, error=None, raw=False):
    self.payload = {} if payload is None else payload
    self.error = error
    self.raw = raw
    self.requests = []

  def __call__(self, request, timeout):
    self.requests.append((request, timeout))
    if self.error:
      raise self.error
    return FakeResponse(self.payload, raw=self.raw)


class NormalizeUsageTest(unittest.TestCase):
  def test_normalizes_weekly_and_five_hour_windows(self):
    payload = {
      "usage": {
        "limit": "100",
        "used": "25",
        "resetTime": "2026-08-24T00:00:00Z",
      },
      "limits": [{
        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
        "detail": {
          "limit": "40",
          "remaining": "10",
          "resetTime": "2026-08-19T15:00:00Z",
        },
      }],
    }

    self.assertEqual(normalize_usage(payload), [
      {
        "label": "Weekly (7-day)",
        "percent": 0.25,
        "resetsAt": "2026-08-24T00:00:00Z",
      },
      {
        "label": "Session (5-hour)",
        "percent": 0.75,
        "resetsAt": "2026-08-19T15:00:00Z",
      },
    ])

  def test_clamps_usage_and_discards_zero_or_invalid_limits(self):
    payload = {"limits": [
      {
        "window": {"duration": 2, "timeUnit": "TIME_UNIT_DAY"},
        "detail": {"limit": 10, "used": 50},
      },
      {"detail": {"limit": 0, "used": 0}},
      {"detail": "invalid"},
    ]}

    self.assertEqual(normalize_usage(payload), [
      {"label": "2-day window", "percent": 1.0, "resetsAt": ""},
    ])

  def test_supports_hour_week_and_unknown_duration_labels(self):
    payload = {"limits": [
      {"window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"}, "detail": {"limit": 10, "used": 1}},
      {"window": {"duration": 3, "timeUnit": "TIME_UNIT_HOUR"}, "detail": {"limit": 10, "used": 2}},
      {"window": {"duration": 9, "timeUnit": "OTHER"}, "detail": {"limit": 10, "used": 3}},
    ]}

    self.assertEqual([row["label"] for row in normalize_usage(payload)], [
      "Weekly (7-day)",
      "3-hour window",
      "Limit",
    ])


class FetchQuotaTest(unittest.TestCase):
  def test_fetch_uses_bearer_header_and_parses_limits(self):
    opener = RecordingOpener({"usage": {"limit": 100, "used": 1}})

    result = fetch_quota("synthetic-access", {
      "KIMI_CODE_BASE_URL": "http://127.0.0.1:8765/coding/v1",
    }, opener=opener)

    request, timeout = opener.requests[0]
    self.assertEqual(request.full_url, "http://127.0.0.1:8765/coding/v1/usages")
    self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-access")
    self.assertEqual(request.get_header("Accept"), "application/json")
    self.assertEqual(timeout, 8)
    self.assertEqual(result.limits[0]["percent"], 0.01)
    self.assertNotIn("synthetic-access", result.status + result.help_text)

  def test_categorizes_http_failures_without_response_body_leaks(self):
    cases = [
      (401, False, "authenticate"),
      (403, False, "authenticate"),
      (404, False, "unavailable"),
      (429, True, "temporarily"),
      (500, True, "temporarily"),
    ]
    for status, transient, expected_text in cases:
      with self.subTest(status=status):
        error = urllib.error.HTTPError(
          "http://127.0.0.1",
          status,
          "failure",
          {},
          io.BytesIO(b"synthetic-access secret body"),
        )
        result = fetch_quota("synthetic-access", {
          "KIMI_CODE_BASE_URL": "http://127.0.0.1:8765/coding/v1",
        }, opener=RecordingOpener(error=error))
        self.assertEqual(result.limits, [])
        self.assertEqual(result.transient, transient)
        self.assertIn(expected_text, (result.status + " " + result.help_text).lower())
        self.assertNotIn("synthetic-access", result.status + result.help_text)

  def test_timeout_is_transient_and_invalid_json_is_not(self):
    timeout_result = fetch_quota("synthetic-access", {
      "KIMI_CODE_BASE_URL": "http://127.0.0.1:8765/coding/v1",
    }, opener=RecordingOpener(error=TimeoutError()))
    invalid_result = fetch_quota("synthetic-access", {
      "KIMI_CODE_BASE_URL": "http://127.0.0.1:8765/coding/v1",
    }, opener=RecordingOpener(payload=b"not json", raw=True))

    self.assertTrue(timeout_result.transient)
    self.assertFalse(invalid_result.transient)
    self.assertEqual(invalid_result.limits, [])

  def test_rejects_unsafe_base_url_without_sending_request(self):
    opener = RecordingOpener({})

    result = fetch_quota("synthetic-access", {
      "KIMI_CODE_BASE_URL": "http://example.com/coding/v1",
    }, opener=opener)

    self.assertEqual(opener.requests, [])
    self.assertFalse(result.transient)
    self.assertIn("invalid", result.status.lower())


if __name__ == "__main__":
  unittest.main()
