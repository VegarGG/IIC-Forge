"""Channel-aware Jinja renderer.

Looks up templates by (channel, mode). Email rendering uses Jinja autoescaping;
the SMTP boundary also applies a strict HTML allow-list.
"""

from __future__ import annotations

from typing import Any, Dict

from jinja2 import Environment, PackageLoader, select_autoescape


_plain_env = Environment(
    loader=PackageLoader("tradingagents.delivery", "templates"),
    autoescape=False,
    keep_trailing_newline=True,
)
_email_env = Environment(
    loader=PackageLoader("tradingagents.delivery", "templates"),
    autoescape=select_autoescape(default=True),
    keep_trailing_newline=True,
)

_KNOWN_CHANNELS = ("telegram", "email", "cli")


def render_for_channel(*, channel: str, mode: str, brief: Dict[str, Any]) -> str:
    if channel not in _KNOWN_CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    template_path = f"{channel}/{mode}.j2"
    environment = _email_env if channel == "email" else _plain_env
    tmpl = environment.get_template(template_path)
    return tmpl.render(**brief)
