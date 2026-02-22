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

mkdir -p "$HOME/.codex" "$cache_base"

exec bwrap \
  --die-with-parent \
  --ro-bind / / \
  --proc /proc \
  --dev /dev \
  --bind /tmp /tmp \
  --bind "$HOME/.codex" "$HOME/.codex" \
  --bind "$cache_base" "$cache_base" \
  --chdir "$project_root" \
  --setenv XDG_CACHE_HOME "$xdg_cache_home" \
  -- codex \
     --sandbox workspace-write \
     --ask-for-approval untrusted \
     "$@"
