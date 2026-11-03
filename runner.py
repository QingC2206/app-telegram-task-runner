"""Source-checkout launcher; no pip install or global Python changes required."""
from pathlib import Path
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from telegram_task_runner.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
