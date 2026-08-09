"""Small allow-list HTML sanitizer for generated outbound email."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "body",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "head",
    "hr",
    "html",
    "i",
    "li",
    "meta",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "title",
    "tr",
    "ul",
}
_VOID_TAGS = {"br", "hr", "meta"}
_DROP_CONTENT_TAGS = {"iframe", "object", "script", "style", "svg"}


def _safe_href(value: str) -> bool:
    if any(character in value for character in ("\r", "\n", "\x00")):
        return False
    return urlsplit(value.strip()).scheme.lower() in {"", "http", "https", "mailto"}


class _EmailHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._suppressed_depth = 0

    def handle_decl(self, decl: str) -> None:
        if self._suppressed_depth == 0 and decl.lower().strip() == "doctype html":
            self.output.append("<!DOCTYPE html>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth or tag not in _ALLOWED_TAGS:
            return

        safe_attrs: list[str] = []
        for name, value in attrs:
            name = name.lower()
            value = value or ""
            if tag == "a" and name == "href" and _safe_href(value):
                safe_attrs.append(f' href="{escape(value, quote=True)}"')
            elif tag == "meta" and name == "charset" and value.lower() == "utf-8":
                safe_attrs.append(' charset="utf-8"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppressed_depth:
            if tag in _DROP_CONTENT_TAGS:
                self._suppressed_depth -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth == 0:
            self.output.append(escape(data, quote=False))


def sanitize_email_html(value: str) -> str:
    """Remove active content, unsafe URLs, event handlers, and inline CSS."""
    parser = _EmailHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.output)
