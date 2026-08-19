#!/usr/bin/python3

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from kimi_auth import AuthResult, resolve_access_token
from kimi_quota import QuotaResult, fetch_quota
from kimi_usage import kimi_home, scan_usage

SCAN_REUSE_SECONDS = 20
LIMITS_ONLY_REUSE_SECONDS = 900


def cache_root(env: Mapping[str, str]) -> Path:
  configured = str(env.get("XDG_CACHE_HOME", "")).strip()
  if configured:
    root = Path(configured).expanduser()
  else:
    home = Path(str(env.get("HOME", "")).strip() or Path.home())
    root = home / ".cache"
  return root.resolve() / "omarchy" / "agent-usage"


def cache_paths(root: Path, env: Mapping[str, str]) -> tuple[Path, Path]:
  digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
  base = cache_root(env)
  return base / f"kimi-scan-{digest}.json", base / f"kimi-scan-{digest}.lock"


def read_fresh_cache(path: Path, max_age: int) -> dict[str, Any] | None:
  if max_age <= 0:
    return None
  try:
    if time.time() - path.stat().st_mtime > max_age:
      return None
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return None
  return payload if isinstance(payload, dict) else None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
  temp = Path(temp_name)
  try:
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


def local_stats(root: Path, env: Mapping[str, str], max_age: int) -> dict[str, Any]:
  cache_path, lock_path = cache_paths(root, env)
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with lock_path.open("a+", encoding="utf-8") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    cached = read_fresh_cache(cache_path, max_age)
    if cached is not None:
      return cached
    stats = scan_usage(root)
    atomic_write_json(cache_path, stats)
    return stats


def has_local_stats(stats: Mapping[str, Any]) -> bool:
  return any(int(stats.get(key, 0) or 0) > 0 for key in (
    "todayPrompts",
    "todaySessions",
    "todayTotalTokens",
    "totalPrompts",
    "totalSessions",
    "activeDays",
  ))


def unavailable_quota(auth: AuthResult) -> QuotaResult:
  return QuotaResult(
    status=auth.status,
    help_text=auth.help_text,
    transient=auth.transient,
  )


def build_record(
  env: Mapping[str, str],
  *,
  force: bool = False,
  limits_only: bool = False,
  quota_fetcher: Callable[[str, Mapping[str, str]], QuotaResult] = fetch_quota,
  auth_resolver: Callable[..., AuthResult] = resolve_access_token,
) -> dict[str, Any]:
  root = kimi_home(env)
  max_age = 0 if force else (LIMITS_ONLY_REUSE_SECONDS if limits_only else SCAN_REUSE_SECONDS)
  stats = local_stats(root, env, max_age)
  auth = auth_resolver(root, env)
  quota = quota_fetcher(auth.access_token, env) if auth.access_token else unavailable_quota(auth)
  return {
    "schemaVersion": 1,
    "id": "kimi",
    "name": "Kimi Code",
    "updatedAt": datetime.now(timezone.utc).isoformat(),
    "ready": True,
    "hasLocalStats": has_local_stats(stats),
    "tierLabel": quota.tier_label,
    "usageStatusText": quota.status or auth.status,
    "authHelpText": quota.help_text or auth.help_text,
    "retryAdvised": quota.transient or auth.transient,
    "limits": quota.limits,
    **stats,
  }
