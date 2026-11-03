#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_python="${PYTHON:-python3}"
task_environment="$task_root/runtime/venv"
if [[ ! -x "$task_environment/bin/python" ]]; then
  "$task_python" -m venv "$task_environment"
fi
"$task_environment/bin/python" -m pip install 'keyring>=25,<26'
"$task_environment/bin/python" "$task_root/runner.py" --root "$task_root" doctor --offline
if [[ "${1:-}" == '--prepare-only' ]]; then
  printf '%s\n' 'Environment prepared. Next: bash run.sh setup'
  exit 0
fi
"$task_environment/bin/python" "$task_root/runner.py" --root "$task_root" setup "$@"
"$task_environment/bin/python" "$task_root/runner.py" --root "$task_root" service install --mode logon
