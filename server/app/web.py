"""Shared Jinja setup for the server-rendered UI."""

import pathlib
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates" / "ui"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def ago(dt) -> str:
    if not dt:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{int(s / 86400)}d ago"


def online(host) -> bool:
    if not host.last_seen:
        return False
    ls = host.last_seen
    if ls.tzinfo is None:
        ls = ls.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ls).total_seconds() < 300


templates.env.filters["ago"] = ago
templates.env.globals["online"] = online
