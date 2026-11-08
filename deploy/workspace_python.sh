#!/usr/bin/env bash
# Library: sourcing this file does not install packages or change services.
workspace_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1
}

workspace_find_python() {
  local candidate executable
  for candidate in "$@"; do
    executable=$(command -v "$candidate" 2>/dev/null) || continue
    if workspace_python_supported "$executable"; then
      printf '%s\n' "$executable"
      return 0
    fi
  done
  return 1
}

workspace_bootstrap_python() {
  if workspace_find_python .venv-workspace/bin/python python3.12 python3.11 python3.10 .venv/bin/python python3; then
    return 0
  fi
  . /etc/os-release
  if [[ "$ID" == amzn && "$VERSION_ID" == 2023 ]]; then
    # Keep the system's Python 3.9 and the legacy .venv intact.
    sudo dnf install -y python3.11 python3.11-pip >&2
    workspace_find_python python3.11
  else
    echo 'Install Python 3.10+ (with venv support), then rerun. Existing Python environments were not changed.' >&2
    return 1
  fi
}
