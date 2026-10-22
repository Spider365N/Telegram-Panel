"""User + admin panels and their router."""
from __future__ import annotations

from src.panels.common import Screen, fmt_money, fmt_number  # noqa: F401
from src.panels.router import PanelRouter, get_panel_router, reset_panel_router  # noqa: F401

__all__ = [
    "PanelRouter", "get_panel_router", "reset_panel_router", "Screen",
    "fmt_money", "fmt_number",
]
