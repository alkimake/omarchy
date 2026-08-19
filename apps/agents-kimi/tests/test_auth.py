#!/usr/bin/python3

import io
import json
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from kimi_auth import resolve_access_token, safe_service_url


class FakeResponse:
  def __init__(self, payload, status=200):
    self.payload = json.dumps(payload).encode()
    self.status = status

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, traceback):
    return False

  def read(self):
    return self.payload


class RecordingOpener:
  def __init__(self, payload=None, error=None):
    self.payload = payload or {}
    self.error = error
    self.requests = []

  def __call__(self, request, timeout):
    self.requests.append((request, timeout))
    if self.error:
      raise self.error
    return FakeResponse(self.payload)


class ResolveAccessTokenTest(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    self.credentials_path = self.root / "credentials" / "kimi-code.json"
    self.now = 1_787_130_000.0

  def write_credentials(self, payload):
    self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    self.credentials_path.write_text(json.dumps(payload), encoding="utf-8")
    self.credentials_path.chmod(0o600)

  def fail_opener(self, request, timeout):
    self.fail(f"unexpected HTTP request to {request.full_url}")

  def test_coding_key_precedes_general_key_and_oauth(self):
    self.write_credentials({"access_token": "oauth-token", "expires_at": self.now + 3600})

    result = resolve_access_token(self.root, {
      "KIMI_CODING_API_KEY": "coding-token",
      "KIMI_API_KEY": "general-token",
    }, now=self.now, opener=self.fail_opener)

    self.assertEqual(result.access_token, "coding-token")
    self.assertEqual(result.status, "")

  def test_general_key_precedes_oauth_when_coding_key_is_blank(self):
    self.write_credentials({"access_token": "oauth-token", "expires_at": self.now + 3600})

    result = resolve_access_token(self.root, {
      "KIMI_CODING_API_KEY": " ",
      "KIMI_API_KEY": "general-token",
    }, now=self.now, opener=self.fail_opener)

    self.assertEqual(result.access_token, "general-token")

  def test_valid_oauth_access_token_is_reused_without_refresh(self):
    self.write_credentials({"access_token": "oauth-token", "expires_at": self.now + 3600})

    result = resolve_access_token(self.root, {}, now=self.now, opener=self.fail_opener)

    self.assertEqual(result.access_token, "oauth-token")

  def test_expired_oauth_is_refreshed_and_persisted_atomically(self):
    self.write_credentials({
      "access_token": "expired-token",
      "refresh_token": "synthetic-refresh",
      "expires_at": self.now - 1,
      "scope": "openid",
    })
    (self.root / "device_id").write_text("synthetic-device", encoding="utf-8")
    opener = RecordingOpener({
      "access_token": "new-token",
      "expires_in": 3600,
      "token_type": "Bearer",
    })

    result = resolve_access_token(self.root, {
      "KIMI_CODE_OAUTH_HOST": "http://127.0.0.1:8765",
      "KIMI_VERSION_FOR_TESTS": "0.37.2",
    }, now=self.now, opener=opener)

    self.assertEqual(result.access_token, "new-token")
    saved = json.loads(self.credentials_path.read_text(encoding="utf-8"))
    self.assertEqual(saved["refresh_token"], "synthetic-refresh")
    self.assertEqual(saved["expires_at"], self.now + 3600)
    self.assertEqual(saved["scope"], "openid")
    self.assertEqual(stat.S_IMODE(self.credentials_path.stat().st_mode), 0o600)
    request, timeout = opener.requests[0]
    form = parse_qs(request.data.decode())
    self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/oauth/token")
    self.assertEqual(form["grant_type"], ["refresh_token"])
    self.assertEqual(form["refresh_token"], ["synthetic-refresh"])
    self.assertEqual(request.get_header("X-msh-platform"), "kimi_code_cli")
    self.assertEqual(request.get_header("X-msh-device-id"), "synthetic-device")
    self.assertEqual(timeout, 8)
    self.assertNotIn("synthetic-refresh", result.status + result.help_text)

  def test_missing_credentials_returns_actionable_status(self):
    result = resolve_access_token(self.root, {}, now=self.now, opener=self.fail_opener)

    self.assertEqual(result.access_token, "")
    self.assertIn("kimi login", result.help_text)
    self.assertFalse(result.transient)

  def test_expired_token_without_refresh_token_does_not_reuse_access_token(self):
    self.write_credentials({"access_token": "expired-token", "expires_at": self.now - 1})

    result = resolve_access_token(self.root, {}, now=self.now, opener=self.fail_opener)

    self.assertEqual(result.access_token, "")
    self.assertIn("kimi login", result.help_text)

  def test_invalid_grant_is_redacted_and_non_transient(self):
    self.write_credentials({
      "access_token": "expired-token",
      "refresh_token": "synthetic-refresh",
      "expires_at": self.now - 1,
    })
    response = io.BytesIO(b'{"error":"invalid_grant","error_description":"synthetic-refresh rejected"}')
    error = urllib.error.HTTPError("http://127.0.0.1", 400, "Bad Request", {}, response)

    result = resolve_access_token(self.root, {
      "KIMI_CODE_OAUTH_HOST": "http://127.0.0.1:8765",
      "KIMI_VERSION_FOR_TESTS": "0.37.2",
    }, now=self.now, opener=RecordingOpener(error=error))

    self.assertEqual(result.access_token, "")
    self.assertFalse(result.transient)
    self.assertIn("kimi login", result.help_text)
    self.assertNotIn("synthetic-refresh", result.status + result.help_text)

  def test_server_failure_is_redacted_and_transient(self):
    self.write_credentials({
      "access_token": "expired-token",
      "refresh_token": "synthetic-refresh",
      "expires_at": self.now - 1,
    })
    error = urllib.error.HTTPError("http://127.0.0.1", 503, "Unavailable", {}, io.BytesIO(b"secret"))

    result = resolve_access_token(self.root, {
      "KIMI_CODE_OAUTH_HOST": "http://127.0.0.1:8765",
      "KIMI_VERSION_FOR_TESTS": "0.37.2",
    }, now=self.now, opener=RecordingOpener(error=error))

    self.assertTrue(result.transient)
    self.assertNotIn("synthetic-refresh", result.status + result.help_text)

  def test_rejects_non_https_non_loopback_override(self):
    with self.assertRaises(ValueError):
      safe_service_url("http://example.com")
    self.assertEqual(safe_service_url("https://auth.kimi.com/"), "https://auth.kimi.com")
    self.assertEqual(safe_service_url("http://localhost:8765/"), "http://localhost:8765")


if __name__ == "__main__":
  unittest.main()
