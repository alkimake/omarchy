#!/usr/bin/python3

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


def kimi_home(env: Mapping[str, str]) -> Path:
  configured = str(env.get("KIMI_CODE_HOME", "")).strip()
  if configured:
    return Path(configured).expanduser().resolve()
  configured_home = str(env.get("HOME", "")).strip()
  home = Path(configured_home).expanduser() if configured_home else Path.home()
  return (home / ".kimi-code").resolve()


def empty_bucket() -> dict[str, int]:
  return {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
  }


def empty_stats(today: date | None = None) -> dict[str, Any]:
  current = today or datetime.now().astimezone().date()
  recent_dates = [(current - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
  return {
    "todayPrompts": 0,
    "todaySessions": 0,
    "todayTotalTokens": 0,
    "todayTokensByModel": {},
    "recentDays": [{"date": day, "messageCount": 0} for day in recent_dates],
    "totalPrompts": 0,
    "totalSessions": 0,
    "activeDays": 0,
    "activeDates": [],
    "modelUsage": {},
  }


def number(value: Any) -> int:
  try:
    result = round(float(value or 0))
    return max(0, result)
  except (TypeError, ValueError, OverflowError):
    return 0


def day_from_time(value: Any, fallback: date) -> str:
  try:
    seconds = float(value)
    if seconds > 10_000_000_000:
      seconds /= 1000
    return datetime.fromtimestamp(seconds).astimezone().date().isoformat()
  except (TypeError, ValueError, OverflowError, OSError):
    return fallback.isoformat()


def parse_usage_event(raw: str, fallback: date) -> tuple[str, str, dict[str, int]] | None:
  try:
    event = json.loads(raw)
  except (TypeError, json.JSONDecodeError):
    return None
  if not isinstance(event, dict):
    return None
  if event.get("type") != "usage.record" or event.get("usageScope") != "turn":
    return None
  usage = event.get("usage")
  if not isinstance(usage, dict):
    return None

  bucket = {
    "inputTokens": number(usage.get("inputOther")),
    "outputTokens": number(usage.get("output")),
    "cacheReadInputTokens": number(usage.get("inputCacheRead")),
    "cacheCreationInputTokens": number(usage.get("inputCacheCreation")),
  }
  model = str(event.get("model") or "kimi").strip() or "kimi"
  return day_from_time(event.get("time"), fallback), model, bucket


def session_id_for(path: Path) -> str:
  for part in reversed(path.parts):
    if part.startswith("session_"):
      return part
  return str(path.parent)


def add_usage(stats: dict[str, Any], day: str, model: str, bucket: dict[str, int]) -> None:
  total = sum(bucket.values())
  stats["totalPrompts"] += 1

  model_bucket = stats["modelUsage"].setdefault(model, empty_bucket())
  for key, value in bucket.items():
    model_bucket[key] += value

  for recent_day in stats["recentDays"]:
    if recent_day["date"] == day:
      recent_day["messageCount"] += total
      break

  if stats["recentDays"][-1]["date"] == day:
    stats["todayPrompts"] += 1
    stats["todayTotalTokens"] += total
    stats["todayTokensByModel"][model] = stats["todayTokensByModel"].get(model, 0) + total


def scan_usage(root: Path, now: datetime | None = None) -> dict[str, Any]:
  current = now.astimezone() if now is not None else datetime.now().astimezone()
  stats = empty_stats(current.date())
  session_ids: set[str] = set()
  today_session_ids: set[str] = set()
  active_dates: set[str] = set()
  today = current.date().isoformat()

  for path in root.glob("sessions/**/agents/*/wire.jsonl"):
    try:
      fallback_day = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date()
      handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
      continue

    session_id = session_id_for(path)
    try:
      with handle:
        for raw in handle:
          event = parse_usage_event(raw, fallback_day)
          if event is None:
            continue
          day, model, bucket = event
          if sum(bucket.values()) <= 0:
            continue
          add_usage(stats, day, model, bucket)
          session_ids.add(session_id)
          active_dates.add(day)
          if day == today:
            today_session_ids.add(session_id)
    except OSError:
      continue

  stats["totalSessions"] = len(session_ids)
  stats["todaySessions"] = len(today_session_ids)
  stats["activeDates"] = sorted(active_dates)
  stats["activeDays"] = len(active_dates)
  return stats
