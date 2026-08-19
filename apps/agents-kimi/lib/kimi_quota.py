#!/usr/bin/python3

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from kimi_auth import safe_service_url, string_value

DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"


@dataclass(frozen=True)
class QuotaResult:
  limits: list[dict[str, Any]] = field(default_factory=list)
  tier_label: str = ""
  status: str = ""
  help_text: str = ""
  transient: bool = False


def integer(value: Any) -> int | None:
  try:
    number = float(value)
    if number != number:
      return None
    return round(number)
  except (TypeError, ValueError, OverflowError):
    return None


def duration_label(raw: Any) -> str:
  if not isinstance(raw, dict):
    return "Limit"
  duration = integer(raw.get("duration"))
  unit = raw.get("timeUnit")
  if duration is None or duration <= 0:
    return "Limit"
  if unit == "TIME_UNIT_MINUTE" and duration == 300:
    return "Session (5-hour)"
  if (unit == "TIME_UNIT_DAY" and duration == 7) or (unit == "TIME_UNIT_WEEK" and duration == 1):
    return "Weekly (7-day)"
  names = {
    "TIME_UNIT_MINUTE": "minute",
    "TIME_UNIT_HOUR": "hour",
    "TIME_UNIT_DAY": "day",
    "TIME_UNIT_WEEK": "week",
  }
  name = names.get(unit)
  return f"{duration}-{name} window" if name else "Limit"


def normalize_row(raw: Any, label: str) -> dict[str, Any] | None:
  if not isinstance(raw, dict):
    return None
  limit = integer(raw.get("limit"))
  if limit is None or limit <= 0:
    return None
  used = integer(raw.get("used"))
  if used is None:
    remaining = integer(raw.get("remaining"))
    if remaining is None:
      return None
    used = limit - remaining
  percent = max(0.0, min(1.0, used / limit))
  reset = raw.get("resetTime")
  return {
    "label": label,
    "percent": percent,
    "resetsAt": reset if isinstance(reset, str) else "",
  }


def normalize_usage(payload: Any) -> list[dict[str, Any]]:
  if not isinstance(payload, dict):
    return []
  rows = []
  summary = normalize_row(payload.get("usage"), "Weekly (7-day)")
  if summary:
    rows.append(summary)
  raw_limits = payload.get("limits")
  if not isinstance(raw_limits, list):
    return rows
  for raw in raw_limits:
    if not isinstance(raw, dict):
      continue
    row = normalize_row(raw.get("detail"), duration_label(raw.get("window")))
    if row:
      rows.append(row)
  return rows


def tier_label(payload: Any) -> str:
  if not isinstance(payload, dict):
    return ""
  for key in ("planName", "tierLabel", "membershipName"):
    value = string_value(payload.get(key))
    if value:
      return value
  return ""


def http_failure(status: int) -> QuotaResult:
  if status in {401, 403}:
    return QuotaResult(
      status="Kimi authentication required",
      help_text="Authenticate again with `kimi login`.",
    )
  if status == 404:
    return QuotaResult(
      status="Kimi usage endpoint unavailable",
      help_text="The configured account does not expose Kimi Code membership usage.",
    )
  if status == 429 or status >= 500:
    return QuotaResult(
      status="Kimi limits temporarily unavailable",
      help_text="The Kimi usage service can be retried later.",
      transient=True,
    )
  return QuotaResult(
    status="Kimi limits unavailable",
    help_text=f"The Kimi usage service returned HTTP {status}.",
  )


def fetch_quota(
  access_token: str,
  env: Mapping[str, str],
  opener: Callable[..., Any] = urllib.request.urlopen,
) -> QuotaResult:
  raw_base = string_value(env.get("KIMI_CODE_BASE_URL")) or DEFAULT_BASE_URL
  try:
    base_url = safe_service_url(raw_base)
  except ValueError:
    return QuotaResult(
      status="Invalid Kimi usage URL",
      help_text="KIMI_CODE_BASE_URL must use HTTPS or a loopback HTTP address.",
    )
  request = urllib.request.Request(
    base_url + "/usages",
    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    method="GET",
  )
  try:
    with opener(request, timeout=8) as response:
      payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
      raise ValueError("usage response is not an object")
    limits = normalize_usage(payload)
    if not limits:
      raise ValueError("usage response has no recognized limits")
    return QuotaResult(limits=limits, tier_label=tier_label(payload))
  except urllib.error.HTTPError as error:
    return http_failure(error.code)
  except (urllib.error.URLError, TimeoutError, OSError):
    return QuotaResult(
      status="Kimi limits temporarily unavailable",
      help_text="The Kimi usage service could not be reached.",
      transient=True,
    )
  except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
    return QuotaResult(
      status="Kimi limits unavailable",
      help_text="The Kimi usage service returned an invalid response.",
    )
