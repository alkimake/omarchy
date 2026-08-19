# Kimi Agent Usage Integration Design

## Purpose

Add Kimi Code usage and quota reporting to Omarchy's existing Agents panel without modifying the packaged files under `/usr/share/omarchy` or maintaining a fork of `basecamp/omarchy`.

The implementation lives in the standalone `alkimake/omarchy` repository, which is a home for user-owned Omarchy applications. Its root setup command installs applications through symlinks so repository changes are immediately reflected on the machine and remain independent of Omarchy package upgrades.

## Goals

- Show Kimi Code as a provider in the stock Omarchy Agents panel.
- Report daily and per-model token totals from local Kimi Code sessions.
- Report Kimi account quota windows and reset times when authentication and the quota service are available.
- Reuse the existing Kimi Code OAuth login by default, with explicit environment credentials taking precedence.
- Preserve local token reporting when quota retrieval fails.
- Install and uninstall through an idempotent repository-level `setup.sh`.
- Keep `/usr/share/omarchy` untouched.
- Use only Python's standard library and utilities already required by Omarchy.

## Non-goals

- Forking or patching the `basecamp/omarchy` repository.
- Replacing or cloning the stock Agents QML plugin.
- Adding Kimi quota support to Kimi ACP.
- Supporting Kimi Open Platform billing accounts; this integration targets Kimi Code membership usage.
- Displaying a provider-specific Kimi logo in the stock panel. The panel resolves provider assets only inside its packaged plugin, so this integration accepts the generic fallback glyph.
- Opening an upstream pull request.

## Repository Layout

```text
omarchy/
├── README.md
├── setup.sh
├── docs/
│   └── superpowers/
│       ├── plans/
│       └── specs/
└── apps/
    └── agents-kimi/
        ├── README.md
        ├── bin/
        │   ├── omarchy-agent-usage-kimi
        │   └── omarchy-agent-usage-kimi-update
        ├── systemd/
        │   ├── omarchy-agent-usage-kimi.service
        │   └── omarchy-agent-usage-kimi.timer
        └── tests/
            ├── fixtures/
            ├── test_collector.py
            ├── test_setup.sh
            └── test_update.py
```

Each future application owns its executables, service definitions, documentation, and tests under `apps/<application-id>/`. The root setup command is the only repository-wide installer.

## Collector Interface

`apps/agents-kimi/bin/omarchy-agent-usage-kimi` is an executable Python program compatible with Omarchy's collector convention:

```text
omarchy-agent-usage-kimi [--force] [--limits-only]
```

It prints exactly one JSON object to standard output. Diagnostics go to standard error and never contain credentials. The record uses schema version 1 and provider ID `kimi`.

The output includes the fields consumed by the stock panel:

- Provider metadata: `schemaVersion`, `id`, `name`, `updatedAt`, `ready`, `hasLocalStats`.
- Status: `tierLabel`, `usageStatusText`, `authHelpText`, `retryAdvised` when appropriate.
- Limits: `limits`, with normalized labels, utilization ratios, and ISO-8601 reset times.
- Current usage: `todayPrompts`, `todaySessions`, `todayTotalTokens`, `todayTokensByModel`.
- Historical usage: `recentDays`, `totalPrompts`, `totalSessions`, `activeDays`, `activeDates`, `modelUsage`.

`--force` bypasses the local scan cache. `--limits-only` may reuse a recent local scan while still performing a fresh limits request. These flags match the built-in collectors even though the standalone timer normally runs without flags.

## Local Usage Data

The Kimi home directory is `$KIMI_CODE_HOME` when set and `~/.kimi-code` otherwise. The collector recursively scans:

```text
sessions/**/agents/*/wire.jsonl
```

Only objects with `type == "usage.record"`, `usageScope == "turn"`, and a dictionary-valued `usage` field contribute token usage. A record contributes:

- `usage.inputOther` to `inputTokens`.
- `usage.output` to `outputTokens`.
- `usage.inputCacheRead` to `cacheReadInputTokens`.
- `usage.inputCacheCreation` to `cacheCreationInputTokens`.

The top-level `model` identifies the model. Missing or blank models become `kimi`. The top-level millisecond `time` is converted to the machine's local calendar date; malformed or missing times fall back to the file modification time, then the current local date.

Each accepted usage record counts as one model request for the panel's prompt statistic. The enclosing Kimi session directory identifies the session. An active date is any local date with positive token usage.

Malformed lines, unknown event types, non-numeric counters, and unreadable files are ignored. Token counters are clamped to non-negative integers. The collector reads files in place and never modifies Kimi state.

## Scan Cache

Aggregated local statistics are cached below:

```text
${XDG_CACHE_HOME:-~/.cache}/omarchy/agent-usage/kimi-scan-<digest>.json
```

The digest identifies the resolved Kimi home path. Cache writes use a temporary file followed by an atomic rename and are protected by a lock file so the timer and manual invocations cannot corrupt each other.

Normal invocations only reuse a scan briefly to collapse concurrent runs. `--limits-only` may reuse a scan for up to fifteen minutes. `--force` always scans the local files.

## Authentication

Credential precedence is:

1. `KIMI_CODING_API_KEY`.
2. `KIMI_API_KEY`.
3. `$KIMI_CODE_HOME/credentials/kimi-code.json`, or `~/.kimi-code/credentials/kimi-code.json` when `KIMI_CODE_HOME` is unset.

The OAuth file contains `access_token`, `refresh_token`, and `expires_at`. A still-valid access token is used directly. When it is expired and a refresh token is present, the collector locks a sidecar file, re-reads the credential in case Kimi refreshed it concurrently, and otherwise performs the same refresh-token grant as Kimi Code 0.37.2: `POST https://auth.kimi.com/api/oauth/token` with client ID `17e5f671-d194-4dfb-9706-5516cb48c098`. `KIMI_CODE_OAUTH_HOST` and then `KIMI_OAUTH_HOST` may override the OAuth host, matching Kimi Code.

The refresh request carries Kimi's device headers using the existing `$KIMI_CODE_HOME/device_id`, hostname, operating-system information, and the installed `kimi --version`. A successful refresh is written atomically to the credential file with mode `0600`, preserving the previous refresh token when the response omits one. Invalid-grant responses do not delete the credential; they degrade to an authentication message and leave credential repair to `kimi login`. OAuth refresh credentials remain on disk; only the refresh token is sent to the OAuth host and only the access token is sent to the usage endpoint.

If no usable credential exists, local statistics remain available. The record explains how to authenticate with `kimi login` or provide a Kimi Coding API key.

## Quota Retrieval

The collector queries the Kimi Code membership usage service at the Kimi Coding API base URL, defaulting to:

```text
https://api.kimi.com/coding/v1/usages
```

The request has a short timeout and uses `Authorization: Bearer <token>`. The response's overall usage and window-specific limits are normalized into Omarchy limit entries. Durations determine stable labels such as `Session (5-hour)` and `Weekly`; unknown durations receive a descriptive duration label instead of being discarded.

Kimi normally reports `used` and `limit`; older responses may report `remaining` instead. Omarchy expects fraction used, calculated as `used / limit`, or with the compatibility fallback:

```text
(limit - remaining) / limit
```

The result is clamped to `[0, 1]`. Reset timestamps remain ISO-8601 strings. Missing, invalid, or zero limits are ignored.

The quota endpoint is not a stable public integration contract. Network failures, authorization failures, schema changes, and invalid responses therefore degrade to local statistics with `usageStatusText`, `authHelpText`, and, for transient failures, `retryAdvised`. They do not make the collector fail or erase a previously valid output record.

## Atomic Record Writer

`apps/agents-kimi/bin/omarchy-agent-usage-kimi-update` runs the collector, validates that its output is a JSON object for provider `kimi`, and atomically writes:

```text
${XDG_STATE_HOME:-~/.local/state}/omarchy/agents/usage/kimi.json
```

It forwards `--force` and `--limits-only`. A collector or validation failure returns non-zero and leaves the previous record untouched, preventing a temporary Kimi failure from blanking the panel.

## User Service and Timer

The user service is a one-shot unit that executes `omarchy-agent-usage-kimi-update`. The timer runs shortly after login and every fifteen minutes thereafter, matching the stock Agents panel's default refresh interval. The setup command also runs one immediate forced update so Kimi appears without waiting for the first timer firing.

The stock Agents panel watches the usage directory and enables unknown provider IDs by default. Therefore `kimi.json` creates a Kimi tab without changing the packaged manifest or QML.

The panel's built-in refresh action only runs packaged collectors, so pressing refresh will not directly invoke this standalone collector. The user can force an immediate Kimi refresh with:

```bash
omarchy-agent-usage-kimi-update --force
```

The timer bounds normal staleness to fifteen minutes.

## Setup and Uninstall

The root command supports:

```text
./setup.sh
./setup.sh agents-kimi
./setup.sh --uninstall
./setup.sh --uninstall agents-kimi
```

With no application IDs, it processes every application known to the script. The operation is idempotent.

Installation:

1. Resolve the repository root without assuming the caller's working directory.
2. Verify required commands before making changes.
3. Create `~/.local/bin` and `~/.config/systemd/user` when absent.
4. Create or replace only symlinks owned by this repository.
5. Refuse to overwrite unrelated files or symlinks.
6. Reload the user systemd manager and enable/start the timer.
7. Run one forced update and report whether local statistics and limits are available.

Uninstallation:

1. Disable and stop the timer.
2. Remove only symlinks that resolve into this repository.
3. Reload the user systemd manager.
4. Remove the generated `kimi.json` record so the stale provider tab disappears.
5. Preserve Kimi sessions, credentials, and collector caches.

No setup action uses `sudo`, `pkexec`, or writes below `/usr/share`.

## Error Handling and Security

- Real credential values never appear in JSON records, cache files, command arguments, diagnostics, test fixtures, or service logs. Tests use clearly synthetic tokens confined to temporary directories.
- HTTP requests are limited to the configured Kimi Coding API origin and use HTTPS by default. A non-HTTPS override is rejected except for loopback addresses used by tests.
- Network requests have bounded timeouts.
- Local parsing is tolerant of sessions being written concurrently.
- Update writes are atomic and retain the last known-good record on failure.
- Setup refuses destructive replacement of unrelated paths.
- Tests use synthetic credentials and a local HTTP server; they never contact Kimi.

## Testing

Python's built-in `unittest` framework exercises the real collector and updater through temporary Kimi, cache, and state homes. Test fixtures cover:

- Multiple sessions, agents, days, and models.
- Input, output, cache-read, and cache-creation aggregation.
- Local-day conversion from millisecond timestamps.
- Malformed JSONL and invalid or negative token values.
- Missing Kimi home and empty history.
- Cache reuse, forced scans, and atomic cache writes.
- Environment credential precedence and OAuth fallback through synthetic credential files.
- Overall, five-hour, weekly, and unknown quota windows.
- Invalid responses, timeouts, HTTP authentication failures, and transient errors.
- Redaction of credential values from all output.
- Atomic record updates and preservation of the last valid record.

Shell tests run `setup.sh` against temporary HOME/XDG directories and a fake `systemctl` executable. They verify installation, repeat installation, refusal to replace unrelated files, targeted application selection, and uninstall cleanup without touching the real user session.

After automated tests pass, live verification runs the installed collector and checks:

- `kimi.json` is valid and contains provider ID `kimi`.
- Kimi appears in the existing panel.
- Local totals are non-zero when session history exists.
- Limits either render or show an actionable degraded status.
- The user timer is active.
- `/usr/share/omarchy` remains unchanged.

## Delivery

The completed repository will have a clean `main` history, be published as `alkimake/omarchy`, and contain no fork relationship to `basecamp/omarchy`. No pull request will be opened. The installed integration remains linked to the checked-out repository, so future application updates require only pulling the repository and rerunning `setup.sh` when installation metadata changes.
