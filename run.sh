#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$task_root/runtime/venv/bin/python" "$task_root/runner.py" --root "$task_root" "$@"
