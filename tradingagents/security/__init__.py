"""Security boundaries for content originating outside IIC-Forge."""

from .untrusted import UNTRUSTED_DATA_POLICY, render_untrusted_payload

__all__ = ["UNTRUSTED_DATA_POLICY", "render_untrusted_payload"]
