#!/bin/bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
test_root=$(mktemp -d /tmp/omarchy-setup-test.XXXXXX)
trap 'rm -rf "$test_root"' EXIT
tests_run=0

new_environment() {
  tests_run=$((tests_run + 1))
  case_root="$test_root/case-$tests_run"
  test_home="$case_root/home"
  fake_bin="$case_root/bin"
  systemctl_log="$case_root/systemctl.log"
  mkdir -p "$test_home" "$fake_bin"
  # Variables expand when the generated fixture runs.
  # shellcheck disable=SC2016
  printf '%s\n' \
    '#!/bin/bash' \
    'printf "%s\n" "$*" >>"$SYSTEMCTL_LOG"' \
    >"$fake_bin/systemctl"
  chmod +x "$fake_bin/systemctl"
}

run_setup() {
  HOME="$test_home" \
  XDG_CONFIG_HOME="$test_home/config" \
  XDG_STATE_HOME="$test_home/state" \
  XDG_CACHE_HOME="$test_home/cache" \
  SYSTEMCTL_LOG="$systemctl_log" \
  OMARCHY_KIMI_SKIP_INITIAL_UPDATE=1 \
  PATH="$fake_bin:/usr/bin:/bin" \
    "$repo_root/setup.sh" "$@"
}

assert_link() {
  local target="$1" expected="$2"
  [[ -L $target ]]
  [[ $(readlink -f "$target") == "$expected" ]]
}

test_install_and_repeat_install() {
  new_environment
  run_setup agents-kimi
  run_setup agents-kimi

  assert_link "$test_home/.local/bin/omarchy-agent-usage-kimi" \
    "$repo_root/apps/agents-kimi/bin/omarchy-agent-usage-kimi"
  assert_link "$test_home/.local/bin/omarchy-agent-usage-kimi-update" \
    "$repo_root/apps/agents-kimi/bin/omarchy-agent-usage-kimi-update"
  assert_link "$test_home/config/systemd/user/omarchy-agent-usage-kimi.service" \
    "$repo_root/apps/agents-kimi/systemd/omarchy-agent-usage-kimi.service"
  assert_link "$test_home/config/systemd/user/omarchy-agent-usage-kimi.timer" \
    "$repo_root/apps/agents-kimi/systemd/omarchy-agent-usage-kimi.timer"
  grep -Fx 'EnvironmentFile=-%E/omarchy/agents-kimi.env' \
    "$test_home/config/systemd/user/omarchy-agent-usage-kimi.service" >/dev/null
  grep -Fx -- '--user daemon-reload' "$systemctl_log" >/dev/null
  grep -Fx -- '--user enable --now omarchy-agent-usage-kimi.timer' "$systemctl_log" >/dev/null
}

test_refuses_unrelated_target() {
  new_environment
  mkdir -p "$test_home/.local/bin"
  printf 'owned elsewhere\n' >"$test_home/.local/bin/omarchy-agent-usage-kimi"

  if run_setup agents-kimi >/dev/null 2>&1; then
    echo "setup replaced an unrelated executable" >&2
    return 1
  fi
  grep -Fx 'owned elsewhere' "$test_home/.local/bin/omarchy-agent-usage-kimi" >/dev/null
}

test_preflights_every_target_before_linking_anything() {
  new_environment
  mkdir -p "$test_home/.local/bin"
  printf 'owned elsewhere\n' >"$test_home/.local/bin/omarchy-agent-usage-kimi-update"

  if run_setup agents-kimi >/dev/null 2>&1; then
    echo "setup accepted a conflict on a later target" >&2
    return 1
  fi
  [[ ! -e $test_home/.local/bin/omarchy-agent-usage-kimi ]]
  grep -Fx 'owned elsewhere' "$test_home/.local/bin/omarchy-agent-usage-kimi-update" >/dev/null
}

test_uninstall_removes_owned_links_and_generated_record() {
  new_environment
  run_setup agents-kimi
  mkdir -p "$test_home/state/omarchy/agents/usage"
  printf '{"id":"kimi"}\n' >"$test_home/state/omarchy/agents/usage/kimi.json"

  run_setup --uninstall agents-kimi

  [[ ! -e $test_home/.local/bin/omarchy-agent-usage-kimi ]]
  [[ ! -e $test_home/.local/bin/omarchy-agent-usage-kimi-update ]]
  [[ ! -e $test_home/config/systemd/user/omarchy-agent-usage-kimi.service ]]
  [[ ! -e $test_home/config/systemd/user/omarchy-agent-usage-kimi.timer ]]
  [[ ! -e $test_home/state/omarchy/agents/usage/kimi.json ]]
  grep -Fx -- '--user disable --now omarchy-agent-usage-kimi.timer' "$systemctl_log" >/dev/null
}

test_uninstall_preserves_unrelated_symlink() {
  new_environment
  mkdir -p "$test_home/.local/bin"
  printf '#!/bin/bash\n' >"$case_root/unrelated"
  ln -s "$case_root/unrelated" "$test_home/.local/bin/omarchy-agent-usage-kimi"

  run_setup --uninstall agents-kimi

  [[ -L $test_home/.local/bin/omarchy-agent-usage-kimi ]]
  [[ $(readlink -f "$test_home/.local/bin/omarchy-agent-usage-kimi") == "$case_root/unrelated" ]]
}

test_uninstall_does_not_disable_unrelated_timer() {
  new_environment
  mkdir -p "$test_home/config/systemd/user"
  printf '[Timer]\nOnBootSec=1h\n' >"$case_root/unrelated.timer"
  ln -s "$case_root/unrelated.timer" \
    "$test_home/config/systemd/user/omarchy-agent-usage-kimi.timer"

  run_setup --uninstall agents-kimi

  [[ -L $test_home/config/systemd/user/omarchy-agent-usage-kimi.timer ]]
  if grep -Fx -- '--user disable --now omarchy-agent-usage-kimi.timer' "$systemctl_log" >/dev/null; then
    echo "uninstall disabled an unrelated timer" >&2
    return 1
  fi
}

test_unknown_application_fails_without_changes() {
  new_environment
  if run_setup unknown-app >/dev/null 2>&1; then
    echo "setup accepted an unknown application" >&2
    return 1
  fi
  [[ ! -e $test_home/.local/bin ]]
}

test_install_and_repeat_install
test_refuses_unrelated_target
test_preflights_every_target_before_linking_anything
test_uninstall_removes_owned_links_and_generated_record
test_uninstall_preserves_unrelated_symlink
test_uninstall_does_not_disable_unrelated_timer
test_unknown_application_fails_without_changes

echo "setup tests passed: $tests_run"
