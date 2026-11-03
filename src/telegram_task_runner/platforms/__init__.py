"""Thin native adapters; business plugins and the controller remain portable."""
import sys


def current():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    if sys.platform.startswith('linux'):
        return 'linux'
    raise RuntimeError('支持 Windows、macOS 和 Linux。')
