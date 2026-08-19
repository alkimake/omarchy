#!/usr/bin/python3

from __future__ import annotations

import fcntl
import json
import os
import platform
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
LOGIN_HELP = "Run `kimi login` or set KIMI_CODING_API_KEY."


@dataclass(frozen=True)
class AuthResult:
  access_token: str = ""
  status: str = ""
  help_text: str = ""
  transient: bool = False


def safe_service_url(value: str) -> str:
  candidate = value.strip().rstrip("/")
  parsed = urllib.parse.urlparse(candidate)
  loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
  if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)):
    raise ValueError("Service URL must use HTTPS or a loopback HTTP address")
  return candidate


def string_value(value: Any) -> str:
  return value.strip() if isinstance(value, str) else ""


def number_value(value: Any) -> float:
  try:
    result = float(value)
    return result if result == result else 0
  except (TypeError, ValueError):
    return 0


def read_credentials(path: Path) -> dict[str, Any]:
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}
  return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
  temp = Path(temp_name)
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(payload, handle, separators=(",", ":"))
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temp, path)
  finally:
    try:
      temp.unlink()
    except FileNotFoundError:
      pass


def installed_version(env: Mapping[str, str]) -> str:
  test_version = string_value(env.get("KIMI_VERSION_FOR_TESTS"))
  if test_version:
    return test_version
  try:
    result = subprocess.run(
      ["kimi", "--version"],
      capture_output=True,
      text=True,
      timeout=2,
      check=False,
    )
    version = result.stdout.strip()
    return version or "unknown"
  except (OSError, subprocess.SubprocessError):
    return "unknown"


def device_id(root: Path) -> str:
  path = root / "device_id"
  try:
    existing = path.read_text(encoding="utf-8").strip()
    if existing:
      return existing
  except OSError:
    pass
  identifier = str(uuid.uuid4())
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(identifier + "\n")
  except FileExistsError:
    try:
      return path.read_text(encoding="utf-8").strip() or identifier
    except OSError:
      pass
  except OSError:
    pass
  return identifier


def device_headers(root: Path, env: Mapping[str, str]) -> dict[str, str]:
  version = installed_version(env)
  return {
    "X-Msh-Platform": "kimi_code_cli",
    "X-Msh-Version": version,
    "X-Msh-Device-Name": socket.gethostname(),
    "X-Msh-Device-Model": f"{platform.system()} {platform.release()} {platform.machine()}".strip(),
    "X-Msh-Os-Version": platform.release(),
    "X-Msh-Device-Id": device_id(root),
  }


def auth_failure(*, transient: bool = False) -> AuthResult:
  return AuthResult(
    status="Kimi limits temporarily unavailable" if transient else "Kimi authentication required",
    help_text="Kimi OAuth refresh failed; try again later." if transient else LOGIN_HELP,
    transient=transient,
  )


def refreshed_payload(current: dict[str, Any], response: dict[str, Any], now: float) -> dict[str, Any] | None:
  access_token = string_value(response.get("access_token"))
  expires_in = number_value(response.get("expires_in"))
  if not access_token or expires_in <= 0:
    return None
  result = dict(current)
  result.update({
    "access_token": access_token,
    "refresh_token": string_value(response.get("refresh_token")) or string_value(current.get("refresh_token")),
    "expires_at": now + expires_in,
    "expires_in": expires_in,
    "token_type": string_value(response.get("token_type")) or string_value(current.get("token_type")) or "Bearer",
  })
  if string_value(response.get("scope")):
    result["scope"] = string_value(response.get("scope"))
  return result


def refresh_oauth(
  path: Path,
  root: Path,
  env: Mapping[str, str],
  now: float,
  opener: Callable[..., Any],
) -> AuthResult:
  lock_path = path.with_suffix(path.suffix + ".lock")
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  try:
    lock = lock_path.open("a+", encoding="utf-8")
  except OSError:
    return auth_failure()

  with lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    current = read_credentials(path)
    access_token = string_value(current.get("access_token"))
    if access_token and number_value(current.get("expires_at")) > now + 30:
      return AuthResult(access_token=access_token)
    refresh_token = string_value(current.get("refresh_token"))
    if not refresh_token:
      return auth_failure()

    raw_host = (
      string_value(env.get("KIMI_CODE_OAUTH_HOST"))
      or string_value(env.get("KIMI_OAUTH_HOST"))
      or DEFAULT_OAUTH_HOST
    )
    try:
      oauth_host = safe_service_url(raw_host)
    except ValueError:
      return auth_failure()

    form = urllib.parse.urlencode({
      "client_id": CLIENT_ID,
      "grant_type": "refresh_token",
      "refresh_token": refresh_token,
    }).encode()
    request = urllib.request.Request(
      oauth_host + "/api/oauth/token",
      data=form,
      headers={
        **device_headers(root, env),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
      },
      method="POST",
    )
    try:
      with opener(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
      if not isinstance(payload, dict):
        return auth_failure()
      updated = refreshed_payload(current, payload, now)
      if updated is None:
        return auth_failure()
      atomic_write_json(path, updated)
      return AuthResult(access_token=string_value(updated.get("access_token")))
    except urllib.error.HTTPError as error:
      return auth_failure(transient=error.code == 429 or error.code >= 500)
    except (urllib.error.URLError, TimeoutError, OSError):
      return auth_failure(transient=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
      return auth_failure()


def resolve_access_token(
  root: Path,
  env: Mapping[str, str],
  now: float | None = None,
  opener: Callable[..., Any] = urllib.request.urlopen,
) -> AuthResult:
  explicit = string_value(env.get("KIMI_CODING_API_KEY")) or string_value(env.get("KIMI_API_KEY"))
  if explicit:
    return AuthResult(access_token=explicit)

  path = root / "credentials" / "kimi-code.json"
  credentials = read_credentials(path)
  if not credentials:
    return auth_failure()
  current_time = time.time() if now is None else now
  access_token = string_value(credentials.get("access_token"))
  if access_token and number_value(credentials.get("expires_at")) > current_time + 30:
    return AuthResult(access_token=access_token)
  return refresh_oauth(path, root, env, current_time, opener)
