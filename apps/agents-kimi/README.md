# Kimi Code Usage for Omarchy

`agents-kimi` adds a Kimi Code provider to Omarchy's existing Agents panel. It writes the same schema-v1 usage record consumed by the built-in Claude Code and Codex collectors, so no QML changes or packaged-file modifications are required.

## Install

From the repository root:

```bash
./setup.sh agents-kimi
```

The setup command links two executables into `~/.local/bin`, links a user service and timer into `${XDG_CONFIG_HOME:-~/.config}/systemd/user`, enables the timer, and runs an initial update. It never changes `/usr/share/omarchy`.

Uninstall with:

```bash
./setup.sh --uninstall agents-kimi
```

Uninstall removes only repository-owned symlinks and the generated Kimi panel record. Kimi sessions, credentials, and scan caches remain intact.

## Data shown

The collector reads Kimi Code's local `sessions/**/agents/*/wire.jsonl` files under `$KIMI_CODE_HOME`, or `~/.kimi-code` when that variable is unset. Each `usage.record` contributes:

- `inputOther` as input tokens
- `output` as output tokens
- `inputCacheRead` as cache-read input tokens
- `inputCacheCreation` as cache-creation input tokens

The result includes today's total, the last seven local calendar days, per-model totals, request counts, session counts, and active days.

For membership limits, the collector calls the Kimi Code `/usages` endpoint and converts its overall, rolling, and weekly allowances into the meters expected by Omarchy.

## Authentication

Credential precedence is:

1. `KIMI_CODING_API_KEY`
2. `KIMI_API_KEY`
3. `$KIMI_CODE_HOME/credentials/kimi-code.json`
4. `~/.kimi-code/credentials/kimi-code.json`

Existing Kimi OAuth access tokens are reused. Expired OAuth credentials are refreshed under a file lock and persisted atomically with mode `0600`. Credential values are never included in usage records, caches, or diagnostic messages.

The `/usages` endpoint is not a stable public integration contract. If authentication, networking, or response parsing fails, local token charts remain available and the panel displays a limits status instead of dropping Kimi entirely.

## Refresh behavior

The user timer runs shortly after login and every fifteen minutes. Force an immediate refresh with:

```bash
omarchy-agent-usage-kimi-update --force
```

The stock panel's own refresh button only invokes collectors packaged inside Omarchy, so it does not trigger this standalone collector directly.

Generated state:

```text
${XDG_STATE_HOME:-~/.local/state}/omarchy/agents/usage/kimi.json
```

Scan cache:

```text
${XDG_CACHE_HOME:-~/.cache}/omarchy/agent-usage/kimi-scan-*.json
```

## Troubleshooting

Inspect the schedule and most recent service run:

```bash
systemctl --user status omarchy-agent-usage-kimi.timer
systemctl --user status omarchy-agent-usage-kimi.service
journalctl --user -u omarchy-agent-usage-kimi.service
```

Run the collector without writing panel state:

```bash
omarchy-agent-usage-kimi --force | jq
```

If limits report an authentication problem, run `kimi login` and retry the update. Local token history does not require network access.

## Known limitation

Kimi uses the panel's generic agent glyph. The stock panel resolves provider icons only from its packaged asset directory, and copying an icon there would violate this application's update-safe rule. A custom icon would require either upstream Omarchy support or maintaining a cloned Agents panel.

## Tests

```bash
python -m unittest discover -s apps/agents-kimi/tests -p 'test_*.py' -v
bash apps/agents-kimi/tests/test_setup.sh
```
