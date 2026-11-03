"""Stable management entry point; platform definitions live in platforms/."""
from .platforms.services import (
    arguments, identity, linux_definition, macos_definition, manage,
    systemd_quote, windows_xml,
)
