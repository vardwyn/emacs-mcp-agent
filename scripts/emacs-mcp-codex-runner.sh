#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'emacs-mcp runner: %s\n' "$*" >&2
  exit 1
}

if ! command -v bwrap >/dev/null 2>&1; then
  die "bwrap not found in PATH"
fi
if ! command -v codex >/dev/null 2>&1; then
  die "codex not found in PATH"
fi

project_root="$(pwd -P)"
xdg_cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
cache_base="${EMACS_MCP_CACHE_DIR:-$xdg_cache_home/emacs-mcp}"
codex_profile="${EMACS_MCP_CODEX_PROFILE:-}"
server_path="${EMACS_MCP_SERVER_PATH:-$project_root/server/emacs-mcp-server.py}"
skill_path="${EMACS_MCP_SKILL_PATH:-$project_root/skills/emacs-mcp}"

[[ -f "$server_path" ]] || die "MCP server script not found: $server_path"
[[ -d "$skill_path" ]] || die "Skill directory not found: $skill_path"

mkdir -p "$HOME/.codex/skills" "$cache_base"

codex_args=(
  -c 'mcp_servers.emacs.command="python3"'
  -c "mcp_servers.emacs.args=[\"$server_path\"]"
  --sandbox workspace-write
  --ask-for-approval untrusted
)
if [[ -n "$codex_profile" ]]; then
  codex_args=(--profile "$codex_profile" "${codex_args[@]}")
fi
codex_args+=("$@")

exec bwrap \
  --die-with-parent \
  --ro-bind / / \
  --proc /proc \
  --dev /dev \
  --bind /tmp /tmp \
  --bind "$HOME/.codex" "$HOME/.codex" \
  --ro-bind "$skill_path" "$HOME/.codex/skills/emacs-mcp" \
  --bind "$cache_base" "$cache_base" \
  --chdir "$project_root" \
  --setenv XDG_CACHE_HOME "$xdg_cache_home" \
  -- codex \
     "${codex_args[@]}"
