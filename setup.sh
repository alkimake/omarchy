#!/bin/bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
supported_apps=(agents-kimi)
mode=install

if [[ ${1:-} == "--uninstall" ]]; then
  mode=uninstall
  shift
fi

selected_apps=("$@")
if (( ${#selected_apps[@]} == 0 )); then
  selected_apps=("${supported_apps[@]}")
fi

is_supported() {
  local requested="$1" supported
  for supported in "${supported_apps[@]}"; do
    [[ $requested == "$supported" ]] && return 0
  done
  return 1
}

for app in "${selected_apps[@]}"; do
  if ! is_supported "$app"; then
    echo "Unknown Omarchy application: $app" >&2
    exit 1
  fi
done

config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
bin_dir="$HOME/.local/bin"
unit_dir="$config_home/systemd/user"

for required_command in systemctl ln readlink unlink; do
  if ! command -v "$required_command" >/dev/null; then
    echo "Required command not found: $required_command" >&2
    exit 1
  fi
done

check_link_target() {
  local source="$1" target="$2" current
  if [[ -L $target ]]; then
    current=$(readlink "$target")
    if [[ $current != "$source" && $current != "$repo_root"/* ]]; then
      echo "Refusing to replace unrelated symlink: $target" >&2
      return 1
    fi
  elif [[ -e $target ]]; then
    echo "Refusing to replace existing path: $target" >&2
    return 1
  fi
}

link_owned() {
  local source="$1" target="$2" current
  if [[ -L $target ]]; then
    current=$(readlink "$target")
    [[ $current == "$source" ]] && return 0
    if [[ $current != "$repo_root"/* ]]; then
      echo "Refusing to replace unrelated symlink: $target" >&2
      return 1
    fi
    unlink "$target"
  elif [[ -e $target ]]; then
    echo "Refusing to replace existing path: $target" >&2
    return 1
  fi
  ln -s "$source" "$target"
}

unlink_owned() {
  local target="$1" current
  [[ -L $target ]] || return 0
  current=$(readlink "$target")
  if [[ $current == "$repo_root"/* ]]; then
    unlink "$target"
  fi
}

install_agents_kimi() {
  local app_root="$repo_root/apps/agents-kimi"
  check_link_target "$app_root/bin/omarchy-agent-usage-kimi" "$bin_dir/omarchy-agent-usage-kimi"
  check_link_target "$app_root/bin/omarchy-agent-usage-kimi-update" "$bin_dir/omarchy-agent-usage-kimi-update"
  check_link_target "$app_root/systemd/omarchy-agent-usage-kimi.service" "$unit_dir/omarchy-agent-usage-kimi.service"
  check_link_target "$app_root/systemd/omarchy-agent-usage-kimi.timer" "$unit_dir/omarchy-agent-usage-kimi.timer"
  mkdir -p "$bin_dir" "$unit_dir"
  link_owned "$app_root/bin/omarchy-agent-usage-kimi" "$bin_dir/omarchy-agent-usage-kimi"
  link_owned "$app_root/bin/omarchy-agent-usage-kimi-update" "$bin_dir/omarchy-agent-usage-kimi-update"
  link_owned "$app_root/systemd/omarchy-agent-usage-kimi.service" "$unit_dir/omarchy-agent-usage-kimi.service"
  link_owned "$app_root/systemd/omarchy-agent-usage-kimi.timer" "$unit_dir/omarchy-agent-usage-kimi.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now omarchy-agent-usage-kimi.timer
  if [[ ${OMARCHY_KIMI_SKIP_INITIAL_UPDATE:-0} != "1" ]]; then
    "$bin_dir/omarchy-agent-usage-kimi-update" --force
  fi
  echo "Installed agents-kimi"
}

uninstall_agents_kimi() {
  systemctl --user disable --now omarchy-agent-usage-kimi.timer >/dev/null 2>&1 || true
  unlink_owned "$bin_dir/omarchy-agent-usage-kimi"
  unlink_owned "$bin_dir/omarchy-agent-usage-kimi-update"
  unlink_owned "$unit_dir/omarchy-agent-usage-kimi.service"
  unlink_owned "$unit_dir/omarchy-agent-usage-kimi.timer"
  systemctl --user daemon-reload
  rm -f "$state_home/omarchy/agents/usage/kimi.json"
  echo "Uninstalled agents-kimi"
}

for app in "${selected_apps[@]}"; do
  if [[ $mode == "install" ]]; then
    install_agents_kimi
  else
    uninstall_agents_kimi
  fi
done
