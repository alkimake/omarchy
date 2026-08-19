# Kimi Agent Usage Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, install, and publish a standalone Kimi Code collector that feeds local token history and membership limits into Omarchy's stock Agents panel without modifying `/usr/share/omarchy`.

**Architecture:** A standard-library Python collector reads Kimi JSONL session events, refreshes or reuses Kimi credentials, queries the membership usage endpoint, and emits Omarchy's schema-v1 provider record. A separate atomic writer stores that record where the stock panel already watches; root-level setup installs executable and systemd symlinks from this general Omarchy-app repository.

**Tech Stack:** Python 3 standard library, Bash 5, systemd user units, `unittest`, Git, GitHub CLI.

## Global Constraints

- Repository: `/home/ake/Projects/alkimake/omarchy`, published as private `alkimake/omarchy` unless the user explicitly chooses public visibility before publication.
- Do not fork `basecamp/omarchy`, open a pull request, or modify `/usr/share/omarchy`.
- Use `$KIMI_CODE_HOME`, `$XDG_CACHE_HOME`, and `$XDG_STATE_HOME` when set; otherwise use their documented home-directory defaults.
- Runtime code may use only Python's standard library and commands already present in Omarchy.
- Real credentials must never appear in output, logs, caches, fixtures, commits, or command arguments.
- All generated usage-record and credential writes must be atomic; a failed update must retain the last valid record.
- Follow Omarchy's two-space indentation convention in Python and Bash files.
- Production behavior must be introduced through a failing test first.

---

## File Map

- `README.md`: repository purpose and root installation commands.
- `setup.sh`: idempotent dispatcher for installing or uninstalling repository applications.
- `apps/agents-kimi/README.md`: data sources, commands, authentication, limitations, and troubleshooting.
- `apps/agents-kimi/lib/kimi_usage.py`: local JSONL parsing and usage aggregation.
- `apps/agents-kimi/lib/kimi_auth.py`: credential precedence, OAuth refresh, secure credential persistence.
- `apps/agents-kimi/lib/kimi_quota.py`: usage endpoint request and Omarchy limit normalization.
- `apps/agents-kimi/bin/omarchy-agent-usage-kimi`: collector CLI, scan cache, record assembly.
- `apps/agents-kimi/bin/omarchy-agent-usage-kimi-update`: JSON validation and atomic state-file update.
- `apps/agents-kimi/systemd/omarchy-agent-usage-kimi.service`: one-shot update service.
- `apps/agents-kimi/systemd/omarchy-agent-usage-kimi.timer`: login and fifteen-minute refresh schedule.
- `apps/agents-kimi/tests/test_usage.py`: local scan behavior.
- `apps/agents-kimi/tests/test_auth.py`: credential and refresh behavior.
- `apps/agents-kimi/tests/test_quota.py`: endpoint parsing and error behavior.
- `apps/agents-kimi/tests/test_collector.py`: CLI/cache/record integration.
- `apps/agents-kimi/tests/test_update.py`: last-known-good atomic writer behavior.
- `apps/agents-kimi/tests/test_setup.sh`: installer behavior with isolated HOME/XDG paths.

---

### Task 1: Local Kimi Usage Scanner

**Files:**
- Create: `apps/agents-kimi/lib/kimi_usage.py`
- Create: `apps/agents-kimi/tests/test_usage.py`

**Interfaces:**
- Produces: `kimi_home(env: Mapping[str, str]) -> Path`
- Produces: `scan_usage(root: Path, now: datetime | None = None) -> dict[str, Any]`
- Produces: `empty_stats(today: date | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write failing scanner tests**

Create synthetic `sessions/wd_test/session_one/agents/main/wire.jsonl` and `.../agent-1/wire.jsonl` files inside a temporary directory. Write JSON lines directly from Python so fixtures cannot contain production credentials.

```python
class ScanUsageTest(unittest.TestCase):
  def test_aggregates_models_days_sessions_and_token_classes(self):
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    self.write_wire("session_one", "main", [
      usage_event("kimi-code/k3", now, input_other=10, output=3,
                  cache_read=20, cache_creation=2),
      usage_event("kimi-code/k3", now - timedelta(days=1), output=5),
    ])
    self.write_wire("session_two", "agent-1", [
      usage_event("kimi-code/k2.5", now, input_other=7, output=1),
    ])

    result = scan_usage(self.root, now=now)

    self.assertEqual(result["todayPrompts"], 2)
    self.assertEqual(result["todaySessions"], 2)
    self.assertEqual(result["todayTotalTokens"], 43)
    self.assertEqual(result["todayTokensByModel"], {
      "kimi-code/k2.5": 8,
      "kimi-code/k3": 35,
    })
    self.assertEqual(result["modelUsage"]["kimi-code/k3"], {
      "inputTokens": 10,
      "outputTokens": 8,
      "cacheReadInputTokens": 20,
      "cacheCreationInputTokens": 2,
    })

  def test_ignores_malformed_non_turn_and_zero_usage_records(self):
    self.write_raw("session_one", "main", [
      "not json\n",
      json.dumps({"type": "usage.record", "usageScope": "session", "usage": {"output": 9}}) + "\n",
      json.dumps(usage_event("", self.now, input_other=-4)) + "\n",
    ])
    self.assertEqual(scan_usage(self.root, now=self.now), empty_stats(self.now.date()))

  def test_falls_back_to_file_mtime_for_missing_timestamp(self):
    path = self.write_wire("session_one", "main", [usage_event("kimi-code/k3", None, output=4)])
    fallback = datetime(2026, 8, 18, 8).timestamp()
    os.utime(path, (fallback, fallback))
    result = scan_usage(self.root, now=self.now)
    self.assertEqual(result["recentDays"][-2]["messageCount"], 4)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python -m unittest apps/agents-kimi/tests/test_usage.py -v
```

Expected: import failure for missing `kimi_usage`.

- [ ] **Step 3: Implement the scanner**

Use this data flow in `kimi_usage.py`:

```python
def scan_usage(root: Path, now: datetime | None = None) -> dict[str, Any]:
  current = now.astimezone() if now else datetime.now().astimezone()
  stats = empty_stats(current.date())
  session_ids: set[str] = set()
  today_session_ids: set[str] = set()
  active_dates: set[str] = set()

  for path in root.glob("sessions/**/agents/*/wire.jsonl"):
    fallback_day = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date()
    session_id = next((part for part in path.parts if part.startswith("session_")), str(path.parent))
    with path.open(encoding="utf-8", errors="replace") as handle:
      for raw in handle:
        event = parse_usage_event(raw, fallback_day)
        if event is None:
          continue
        day, model, bucket = event
        total = sum(bucket.values())
        if total <= 0:
          continue
        add_usage(stats, day, session_id, model, bucket)
        session_ids.add(session_id)
        active_dates.add(day)
        if day == current.date().isoformat():
          today_session_ids.add(session_id)

  stats["totalSessions"] = len(session_ids)
  stats["todaySessions"] = len(today_session_ids)
  stats["activeDates"] = sorted(active_dates)
  stats["activeDays"] = len(active_dates)
  return stats
```

`parse_usage_event` must require `type == "usage.record"`, `usageScope == "turn"`, and dictionary `usage`; clamp each numeric token field with `max(0, round(float(value or 0)))`; convert millisecond timestamps through `datetime.fromtimestamp(value / 1000).astimezone()`; and map the four Kimi counters exactly as specified.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 test command again. Expected: all scanner tests pass with no warnings.

- [ ] **Step 5: Commit the scanner**

```bash
git add apps/agents-kimi/lib/kimi_usage.py apps/agents-kimi/tests/test_usage.py
git commit -m "feat: aggregate local Kimi usage"
```

---

### Task 2: Kimi Credential Loading and OAuth Refresh

**Files:**
- Create: `apps/agents-kimi/lib/kimi_auth.py`
- Create: `apps/agents-kimi/tests/test_auth.py`

**Interfaces:**
- Produces: `AuthResult(access_token: str, status: str, help_text: str, transient: bool)`
- Produces: `resolve_access_token(root: Path, env: Mapping[str, str], now: float | None = None, opener: Callable = urlopen) -> AuthResult`
- Produces: `safe_service_url(value: str) -> str`

- [ ] **Step 1: Write failing credential-precedence and refresh tests**

```python
class ResolveAccessTokenTest(unittest.TestCase):
  def test_coding_key_precedes_general_key_and_oauth(self):
    self.write_credentials({"access_token": "oauth-token", "expires_at": self.now + 3600})
    result = resolve_access_token(self.root, {
      "KIMI_CODING_API_KEY": "coding-token",
      "KIMI_API_KEY": "general-token",
    }, now=self.now)
    self.assertEqual(result.access_token, "coding-token")

  def test_valid_oauth_access_token_is_reused_without_refresh(self):
    self.write_credentials({"access_token": "oauth-token", "expires_at": self.now + 3600})
    result = resolve_access_token(self.root, {}, now=self.now, opener=self.fail_opener)
    self.assertEqual(result.access_token, "oauth-token")

  def test_expired_oauth_is_refreshed_and_persisted_atomically(self):
    self.write_credentials({
      "access_token": "expired-token",
      "refresh_token": "synthetic-refresh",
      "expires_at": self.now - 1,
    })
    opener = RecordingOpener(json_response({
      "access_token": "new-token",
      "expires_in": 3600,
      "token_type": "Bearer",
    }))
    result = resolve_access_token(self.root, {
      "KIMI_CODE_OAUTH_HOST": "http://127.0.0.1:8765",
      "KIMI_VERSION_FOR_TESTS": "0.37.2",
    }, now=self.now, opener=opener)
    self.assertEqual(result.access_token, "new-token")
    saved = json.loads(self.credentials_path.read_text())
    self.assertEqual(saved["refresh_token"], "synthetic-refresh")
    self.assertEqual(stat.S_IMODE(self.credentials_path.stat().st_mode), 0o600)
    self.assertNotIn("synthetic-refresh", result.status + result.help_text)

  def test_rejects_non_https_non_loopback_override(self):
    with self.assertRaises(ValueError):
      safe_service_url("http://example.com")
```

Add cases for missing credentials, missing refresh token, invalid JSON, invalid-grant, and transient 5xx responses. Assert synthetic token strings never occur in returned status/help text or captured diagnostics.

- [ ] **Step 2: Run the auth tests and verify RED**

```bash
python -m unittest apps/agents-kimi/tests/test_auth.py -v
```

Expected: import failure for missing `kimi_auth`.

- [ ] **Step 3: Implement secure credential resolution**

Implement:

```python
@dataclass(frozen=True)
class AuthResult:
  access_token: str = ""
  status: str = ""
  help_text: str = ""
  transient: bool = False

def resolve_access_token(root, env, now=None, opener=urlopen):
  explicit = env.get("KIMI_CODING_API_KEY", "").strip() or env.get("KIMI_API_KEY", "").strip()
  if explicit:
    return AuthResult(access_token=explicit)

  path = root / "credentials" / "kimi-code.json"
  credentials = read_credentials(path)
  if not credentials:
    return AuthResult(status="Kimi limits unavailable", help_text="Run `kimi login` or set KIMI_CODING_API_KEY.")

  current_time = time.time() if now is None else now
  access_token = string_value(credentials.get("access_token"))
  if access_token and number_value(credentials.get("expires_at")) > current_time + 30:
    return AuthResult(access_token=access_token)
  return refresh_oauth(path, root, credentials, env, current_time, opener)
```

`refresh_oauth` must lock `<credential>.lock` with `fcntl.flock`, re-read after acquiring the lock, POST form fields `client_id`, `grant_type=refresh_token`, and `refresh_token`, add Kimi device headers, preserve the old refresh token when omitted, write with `mkstemp` + `os.fchmod(0o600)` + `os.replace`, and return redacted errors by category rather than embedding response bodies.

`safe_service_url` must accept HTTPS or a loopback hostname (`localhost`, `127.0.0.1`, `::1`) and reject everything else.

- [ ] **Step 4: Verify GREEN**

Run the Task 2 test command. Expected: all auth tests pass and no synthetic token is printed.

- [ ] **Step 5: Commit authentication**

```bash
git add apps/agents-kimi/lib/kimi_auth.py apps/agents-kimi/tests/test_auth.py
git commit -m "feat: reuse Kimi authentication"
```

---

### Task 3: Quota Endpoint and Omarchy Limit Normalization

**Files:**
- Create: `apps/agents-kimi/lib/kimi_quota.py`
- Create: `apps/agents-kimi/tests/test_quota.py`

**Interfaces:**
- Produces: `QuotaResult(limits: list[dict[str, Any]], tier_label: str, status: str, help_text: str, transient: bool)`
- Produces: `normalize_usage(payload: Any) -> list[dict[str, Any]]`
- Produces: `fetch_quota(access_token: str, env: Mapping[str, str], opener: Callable = urlopen) -> QuotaResult`

- [ ] **Step 1: Write failing normalization and HTTP tests**

```python
class NormalizeUsageTest(unittest.TestCase):
  def test_normalizes_weekly_and_five_hour_windows(self):
    payload = {
      "usage": {"limit": "100", "used": "25", "resetTime": "2026-08-24T00:00:00Z"},
      "limits": [{
        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
        "detail": {"limit": "40", "remaining": "10", "resetTime": "2026-08-19T15:00:00Z"},
      }],
    }
    self.assertEqual(normalize_usage(payload), [
      {"label": "Weekly (7-day)", "percent": 0.25, "resetsAt": "2026-08-24T00:00:00Z"},
      {"label": "Session (5-hour)", "percent": 0.75, "resetsAt": "2026-08-19T15:00:00Z"},
    ])

  def test_clamps_usage_and_discards_zero_or_invalid_limits(self):
    payload = {"limits": [
      {"window": {"duration": 2, "timeUnit": "TIME_UNIT_DAY"}, "detail": {"limit": 10, "used": 50}},
      {"detail": {"limit": 0, "used": 0}},
    ]}
    self.assertEqual(normalize_usage(payload), [
      {"label": "2-day window", "percent": 1.0, "resetsAt": ""},
    ])

  def test_fetch_uses_bearer_header_without_leaking_it(self):
    opener = RecordingOpener(json_response({"usage": {"limit": 100, "used": 1}}))
    result = fetch_quota("synthetic-access", {
      "KIMI_CODE_BASE_URL": "http://127.0.0.1:8765/coding/v1",
    }, opener=opener)
    self.assertEqual(opener.requests[0].headers["Authorization"], "Bearer synthetic-access")
    self.assertNotIn("synthetic-access", result.status + result.help_text)
```

Add 401, 403, 404, 429, 500, invalid JSON, and timeout cases. Mark 429/5xx/timeout as transient; make all messages token-free.

- [ ] **Step 2: Run quota tests and verify RED**

```bash
python -m unittest apps/agents-kimi/tests/test_quota.py -v
```

Expected: import failure for missing `kimi_quota`.

- [ ] **Step 3: Implement quota parsing and fetching**

```python
def normalize_usage(payload):
  if not isinstance(payload, dict):
    return []
  rows = []
  summary = normalize_row(payload.get("usage"), "Weekly (7-day)")
  if summary:
    rows.append(summary)
  for raw in payload.get("limits", []):
    if not isinstance(raw, dict):
      continue
    label = duration_label(raw.get("window"))
    row = normalize_row(raw.get("detail"), label)
    if row:
      rows.append(row)
  return rows
```

`normalize_row` prefers `used / limit`, falls back to `(limit - remaining) / limit`, clamps the result, and preserves `resetTime` as `resetsAt`. `duration_label` converts 300 minutes to `Session (5-hour)`, seven days or one week to `Weekly (7-day)`, and other valid durations to `N-minute/hour/day/week window`.

`fetch_quota` validates `${KIMI_CODE_BASE_URL:-https://api.kimi.com/coding/v1}`, appends `/usages`, performs an eight-second GET with Bearer and Accept headers, and returns categorized `QuotaResult` objects without embedding server response bodies.

- [ ] **Step 4: Verify GREEN**

Run the Task 3 test command. Expected: all quota tests pass.

- [ ] **Step 5: Commit quota support**

```bash
git add apps/agents-kimi/lib/kimi_quota.py apps/agents-kimi/tests/test_quota.py
git commit -m "feat: collect Kimi quota limits"
```

---

### Task 4: Collector CLI, Scan Cache, and Provider Record

**Files:**
- Create: `apps/agents-kimi/bin/omarchy-agent-usage-kimi`
- Create: `apps/agents-kimi/tests/test_collector.py`

**Interfaces:**
- Consumes: `scan_usage`, `resolve_access_token`, `fetch_quota`
- Produces: executable `omarchy-agent-usage-kimi [--force] [--limits-only]`
- Produces: schema-v1 JSON record on stdout

- [ ] **Step 1: Write failing end-to-end collector tests**

Invoke the executable with `subprocess.run`, temporary HOME/XDG directories, and local HTTP fixtures.

```python
def test_collector_emits_display_ready_record_with_local_stats_and_limits(self):
  self.write_usage(output=12)
  result = self.run_collector({"KIMI_CODING_API_KEY": "synthetic-key"})
  self.assertEqual(result.returncode, 0)
  record = json.loads(result.stdout)
  self.assertEqual(record["schemaVersion"], 1)
  self.assertEqual(record["id"], "kimi")
  self.assertEqual(record["name"], "Kimi Code")
  self.assertTrue(record["ready"])
  self.assertTrue(record["hasLocalStats"])
  self.assertEqual(record["todayTotalTokens"], 12)
  self.assertEqual(record["limits"][0]["percent"], 0.25)

def test_quota_failure_keeps_local_stats_and_advises_retry(self):
  self.write_usage(output=12)
  result = self.run_collector({"KIMI_CODING_API_KEY": "synthetic-key"}, quota_status=500)
  record = json.loads(result.stdout)
  self.assertEqual(record["todayTotalTokens"], 12)
  self.assertEqual(record["limits"], [])
  self.assertTrue(record["retryAdvised"])
  self.assertNotIn("synthetic-key", result.stdout + result.stderr)

def test_limits_only_reuses_recent_cache_but_force_rescans(self):
  first = self.run_collector({"KIMI_CODING_API_KEY": "synthetic-key"})
  self.add_usage(output=5)
  cached = self.run_collector({"KIMI_CODING_API_KEY": "synthetic-key"}, "--limits-only")
  forced = self.run_collector({"KIMI_CODING_API_KEY": "synthetic-key"}, "--force")
  self.assertEqual(json.loads(first.stdout)["todayTotalTokens"], json.loads(cached.stdout)["todayTotalTokens"])
  self.assertEqual(json.loads(forced.stdout)["todayTotalTokens"], json.loads(first.stdout)["todayTotalTokens"] + 5)
```

- [ ] **Step 2: Run collector tests and verify RED**

```bash
python -m unittest apps/agents-kimi/tests/test_collector.py -v
```

Expected: executable-not-found error.

- [ ] **Step 3: Implement the executable and cache**

The executable inserts its sibling `../lib` into `sys.path`, accepts both flags with `argparse`, and builds:

```python
record = {
  "schemaVersion": 1,
  "id": "kimi",
  "name": "Kimi Code",
  "updatedAt": datetime.now(timezone.utc).isoformat(),
  "ready": True,
  "hasLocalStats": any_local_stats(stats),
  "tierLabel": quota.tier_label,
  "usageStatusText": quota.status or auth.status,
  "authHelpText": quota.help_text or auth.help_text,
  "retryAdvised": quota.transient or auth.transient,
  "limits": quota.limits,
  **stats,
}
```

Resolve authentication first. Call `fetch_quota` only when `auth.access_token` is non-empty; otherwise construct an empty `QuotaResult` carrying the authentication status/help/transient fields. Cache path is `${XDG_CACHE_HOME:-~/.cache}/omarchy/agent-usage/kimi-scan-<sha1>.json`. Protect reads/scans/writes with `fcntl.flock`; write through `mkstemp` and `os.replace`. Use max age 0 for `--force`, 900 seconds for `--limits-only`, and 20 seconds normally. Print compact JSON followed by one newline and return zero even when only limits fail.

- [ ] **Step 4: Verify GREEN and executable metadata**

```bash
chmod +x apps/agents-kimi/bin/omarchy-agent-usage-kimi
python -m unittest apps/agents-kimi/tests/test_collector.py -v
apps/agents-kimi/bin/omarchy-agent-usage-kimi --help
```

Expected: tests pass; help lists `--force` and `--limits-only`.

- [ ] **Step 5: Commit collector integration**

```bash
git add apps/agents-kimi/bin/omarchy-agent-usage-kimi apps/agents-kimi/tests/test_collector.py
git commit -m "feat: emit Omarchy Kimi usage records"
```

---

### Task 5: Atomic Usage Record Updater

**Files:**
- Create: `apps/agents-kimi/bin/omarchy-agent-usage-kimi-update`
- Create: `apps/agents-kimi/tests/test_update.py`

**Interfaces:**
- Consumes: collector CLI JSON stdout
- Produces: atomic `${XDG_STATE_HOME:-~/.local/state}/omarchy/agents/usage/kimi.json`

- [ ] **Step 1: Write failing writer tests**

```python
def test_writes_valid_kimi_record_atomically(self):
  result = self.run_update(collector_output='{"schemaVersion":1,"id":"kimi"}\n')
  self.assertEqual(result.returncode, 0)
  self.assertEqual(json.loads(self.output.read_text())["id"], "kimi")
  self.assertEqual(list(self.output.parent.glob(".kimi.*")), [])

def test_invalid_record_leaves_last_known_good_file_untouched(self):
  self.output.parent.mkdir(parents=True)
  self.output.write_text('{"id":"kimi","sentinel":true}\n')
  result = self.run_update(collector_output='{"id":"codex"}\n')
  self.assertNotEqual(result.returncode, 0)
  self.assertTrue(json.loads(self.output.read_text())["sentinel"])

def test_forwards_force_and_limits_only_flags(self):
  self.run_update("--force", "--limits-only")
  self.assertEqual(self.read_collector_args(), ["--force", "--limits-only"])
```

- [ ] **Step 2: Run updater tests and verify RED**

```bash
python -m unittest apps/agents-kimi/tests/test_update.py -v
```

Expected: executable-not-found error.

- [ ] **Step 3: Implement the updater**

Use a Python executable so JSON validation and atomic writing need no extra process dependency:

```python
collector = Path(__file__).with_name("omarchy-agent-usage-kimi")
completed = subprocess.run([str(collector), *sys.argv[1:]], capture_output=True, text=True)
if completed.returncode != 0:
  sys.stderr.write(completed.stderr)
  raise SystemExit(completed.returncode)
record = json.loads(completed.stdout)
if not isinstance(record, dict) or record.get("schemaVersion") != 1 or record.get("id") != "kimi":
  print("Kimi collector returned an invalid provider record", file=sys.stderr)
  raise SystemExit(1)
atomic_write(output_path(), json.dumps(record, separators=(",", ":")) + "\n")
```

The test-only collector override is supplied through `OMARCHY_KIMI_COLLECTOR`; production defaults to the sibling executable. `atomic_write` uses `mkstemp`, `fsync`, and `os.replace`, deleting only its own temporary file on failure.

- [ ] **Step 4: Verify GREEN**

```bash
chmod +x apps/agents-kimi/bin/omarchy-agent-usage-kimi-update
python -m unittest apps/agents-kimi/tests/test_update.py -v
```

Expected: all updater tests pass.

- [ ] **Step 5: Commit updater**

```bash
git add apps/agents-kimi/bin/omarchy-agent-usage-kimi-update apps/agents-kimi/tests/test_update.py
git commit -m "feat: update Kimi usage records atomically"
```

---

### Task 6: User Service and Idempotent Setup

**Files:**
- Create: `setup.sh`
- Create: `apps/agents-kimi/systemd/omarchy-agent-usage-kimi.service`
- Create: `apps/agents-kimi/systemd/omarchy-agent-usage-kimi.timer`
- Create: `apps/agents-kimi/tests/test_setup.sh`

**Interfaces:**
- Produces: `./setup.sh [--uninstall] [agents-kimi]`
- Produces: symlinks in `~/.local/bin` and `~/.config/systemd/user`

- [ ] **Step 1: Write failing isolated setup tests**

The shell test creates a temporary HOME, XDG state/config/cache directories, and a fake `systemctl` that appends arguments to `$SYSTEMCTL_LOG`.

```bash
test_install_and_repeat_install() {
  HOME="$test_home" XDG_CONFIG_HOME="$test_home/config" PATH="$fake_bin:$PATH" "$repo/setup.sh" agents-kimi
  HOME="$test_home" XDG_CONFIG_HOME="$test_home/config" PATH="$fake_bin:$PATH" "$repo/setup.sh" agents-kimi
  [[ -L $test_home/.local/bin/omarchy-agent-usage-kimi ]]
  [[ -L $test_home/.local/bin/omarchy-agent-usage-kimi-update ]]
  [[ -L $test_home/config/systemd/user/omarchy-agent-usage-kimi.timer ]]
}

test_refuses_unrelated_target() {
  mkdir -p "$test_home/.local/bin"
  printf 'owned elsewhere\n' >"$test_home/.local/bin/omarchy-agent-usage-kimi"
  if HOME="$test_home" PATH="$fake_bin:$PATH" "$repo/setup.sh" agents-kimi; then
    return 1
  fi
  grep -q 'owned elsewhere' "$test_home/.local/bin/omarchy-agent-usage-kimi"
}

test_uninstall_removes_only_owned_links_and_generated_record() {
  install_fixture
  HOME="$test_home" XDG_CONFIG_HOME="$test_home/config" XDG_STATE_HOME="$test_home/state" \
    PATH="$fake_bin:$PATH" "$repo/setup.sh" --uninstall agents-kimi
  [[ ! -e $test_home/.local/bin/omarchy-agent-usage-kimi ]]
  [[ ! -e $test_home/state/omarchy/agents/usage/kimi.json ]]
}
```

The test must also assert fake-systemctl received `--user daemon-reload`, `--user enable --now omarchy-agent-usage-kimi.timer`, and on uninstall `--user disable --now ...`.

- [ ] **Step 2: Run setup test and verify RED**

```bash
bash apps/agents-kimi/tests/test_setup.sh
```

Expected: failure because `setup.sh` and units do not exist.

- [ ] **Step 3: Add systemd units**

```ini
# omarchy-agent-usage-kimi.service
[Unit]
Description=Refresh Kimi usage for the Omarchy Agents panel
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/omarchy-agent-usage-kimi-update
```

```ini
# omarchy-agent-usage-kimi.timer
[Unit]
Description=Refresh Kimi usage for Omarchy every fifteen minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Implement `setup.sh`**

Use `#!/bin/bash`, `set -euo pipefail`, an explicit supported-app array containing `agents-kimi`, and helpers:

```bash
link_owned() {
  local source="$1" target="$2"
  if [[ -L $target ]]; then
    local current
    current=$(readlink -f "$target")
    [[ $current == "$source" ]] && return
    [[ $current == "$repo_root"/* ]] || { echo "Refusing to replace $target" >&2; return 1; }
    unlink "$target"
  elif [[ -e $target ]]; then
    echo "Refusing to replace $target" >&2
    return 1
  fi
  ln -s "$source" "$target"
}

unlink_owned() {
  local target="$1"
  [[ -L $target ]] || return 0
  local current
  current=$(readlink -f "$target")
  [[ $current == "$repo_root"/* ]] && unlink "$target"
}
```

Install both executables and units, reload systemd, enable/start the timer, and invoke `~/.local/bin/omarchy-agent-usage-kimi-update --force`. In tests, set `OMARCHY_KIMI_SKIP_INITIAL_UPDATE=1` to avoid live Kimi access. Uninstall disables the timer, removes owned links, reloads systemd, and removes only the exact generated `kimi.json` path.

- [ ] **Step 5: Verify GREEN**

```bash
chmod +x setup.sh apps/agents-kimi/tests/test_setup.sh
bash apps/agents-kimi/tests/test_setup.sh
```

Expected: all setup assertions pass and all writes stay within the temporary HOME.

- [ ] **Step 6: Commit setup integration**

```bash
git add setup.sh apps/agents-kimi/systemd apps/agents-kimi/tests/test_setup.sh
git commit -m "feat: install Kimi usage integration"
```

---

### Task 7: Documentation, Full Verification, Live Installation, and Publication

**Files:**
- Create: `README.md`
- Create: `apps/agents-kimi/README.md`

**Interfaces:**
- Consumes: all completed application commands
- Produces: documented and installed application; private GitHub repository `alkimake/omarchy`

- [ ] **Step 1: Write documentation acceptance checks**

Create a shell check inside the task command rather than adding another permanent test file:

```bash
for required in \
  './setup.sh agents-kimi' \
  './setup.sh --uninstall agents-kimi' \
  'omarchy-agent-usage-kimi-update --force' \
  'KIMI_CODING_API_KEY' \
  'generic'; do
  rg -F "$required" README.md apps/agents-kimi/README.md >/dev/null
done
```

Run it before creating the docs. Expected: failure because the files are absent.

- [ ] **Step 2: Write repository and application documentation**

Root README must explain the repository is standalone, list `agents-kimi`, and show install/uninstall commands. Application README must document:

- Local Kimi JSONL source and the four token classes.
- OAuth and API-key precedence.
- `/usages` quota dependency and graceful degradation.
- Fifteen-minute timer and manual refresh command.
- Generic-icon limitation.
- State/cache paths and troubleshooting with `systemctl --user status` and `journalctl --user -u`.
- Assurance that setup never changes `/usr/share/omarchy`.

- [ ] **Step 3: Run the complete automated suite**

```bash
python -m unittest discover -s apps/agents-kimi/tests -p 'test_*.py' -v
bash apps/agents-kimi/tests/test_setup.sh
git diff --check
```

Expected: every test passes, shell setup assertions pass, and `git diff --check` prints nothing.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md apps/agents-kimi/README.md
git commit -m "docs: explain Omarchy applications"
```

- [ ] **Step 5: Install on this machine**

```bash
journal_dir=$(mktemp -d /tmp/omarchy-kimi-verify.XXXXXX)
find /usr/share/omarchy -type f -exec sha256sum {} + | sort >"$journal_dir/usr-share.before"
./setup.sh agents-kimi
```

Expected: symlinks are installed, the user timer is enabled, and an immediate forced update succeeds or reports a limits-only degradation while still writing local stats.

- [ ] **Step 6: Verify live behavior without exposing credentials**

```bash
jq '{schemaVersion,id,name,ready,hasLocalStats,todayTotalTokens,limits,usageStatusText,retryAdvised}' \
  "${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/agents/usage/kimi.json"
systemctl --user is-enabled omarchy-agent-usage-kimi.timer
systemctl --user is-active omarchy-agent-usage-kimi.timer
find /usr/share/omarchy -type f -exec sha256sum {} + | sort >"$journal_dir/usr-share.after"
diff -u "$journal_dir/usr-share.before" "$journal_dir/usr-share.after"
rmdir "$journal_dir"
```

Expected: record ID is `kimi`, local totals are present, timer is enabled/active, and the `/usr/share/omarchy` check prints nothing created by this work. Open the Agents panel and confirm the Kimi tab renders.

- [ ] **Step 7: Run final repository verification**

```bash
git status --short
git log --oneline --decorate -8
```

Expected: clean worktree and a focused history containing the design plus Tasks 1-7.

- [ ] **Step 8: Create and push the private GitHub repository without a PR**

First verify the remote does not already exist, then publish safely as private:

```bash
gh repo view alkimake/omarchy --json nameWithOwner,visibility
gh repo create alkimake/omarchy --private --source=. --remote=origin --push
gh repo view alkimake/omarchy --json nameWithOwner,url,visibility,defaultBranchRef
```

Expected: the first command reports not found, creation succeeds, visibility is `PRIVATE`, default branch is `main`, and no PR is created. If the repository unexpectedly exists, stop and inspect it instead of overwriting or force-pushing.
