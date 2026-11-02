"""
Log anonymization filter — scrubs URLs, usernames/passwords, and auth
tokens/headers (Plex, Emby/Jellyfin, generic Authorization/Bearer) from log
records before they are written. Activated when LOG_ANONYMIZE=true (default).
"""

import logging
import re

_URL_RE = re.compile(
    r'(?:https?|rtmps?|ftps?|hls|rediss?)://[^\s"\'<>\[\]{}\|\\^`]+',
    re.IGNORECASE,
)
# Matches key=value, key: value, key='value', key="value".
# Group 1: keyword, group 2: optional quote, group 3: value, \2: matching close quote.
_USER_RE = re.compile(
    r"""(?i)\b(username|user|login|password|ip|host|hostname|server)\s*[:=]\s*(['"']?)([^&\s"'<>#,\)\]]+)\2""",
)
# Auth tokens/credentials that can appear OUTSIDE a URL — e.g. Plex's X-Plex-Token is
# deliberately pulled out of the stream URL into a raw FFmpeg -headers argument (so
# the URL itself isn't tagged with it), which means _URL_RE's whole-URL redaction
# never sees it. Covers the header form (`Key: value`, as FFmpeg receives it) and the
# query-string form (`key=value`, e.g. Emby/Jellyfin's api_key) for both.
# Group 1: keyword, group 2: separator + optional quote, group 3: quote, group 4: value.
_TOKEN_RE = re.compile(
    r"""(?i)\b(x-plex-token|x-emby-token|x-mediabrowser-token|api[_-]?key|api[_-]?token|access[_-]?token)(\s*[:=]\s*)(['"']?)([^&\s"'<>#,\)\]]+)\3""",
)
# Authorization-style headers carry more than a single bare token — a scheme prefix
# + token separated by a space (`Bearer <token>`, `Basic <base64>`), or Emby's
# structured `MediaBrowser Client="...", Token="..."` form. _TOKEN_RE's
# whitespace/quote-terminated value would only swallow the first word or segment
# and leave the actual credential exposed, so this redacts the entire header value
# (everything after the colon up to end-of-line) instead of trying to parse it.
_AUTH_HEADER_RE = re.compile(
    r"""(?i)\b(authorization|x-emby-authorization)\s*[:=]\s*[^\r\n]+""",
)
# UUIDs are resource identifiers that can be used to probe APIs
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    text = _URL_RE.sub("****", text)
    text = _USER_RE.sub(r"\1=\2****\2", text)
    text = _TOKEN_RE.sub(r"\1\2\3****\3", text)
    text = _AUTH_HEADER_RE.sub(r"\1: ****", text)
    text = _UUID_RE.sub("****", text)
    return text


class AnonymizingFilter(logging.Filter):
    """Logging filter that redacts URLs and credentials from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access log records: AccessFormatter reads record.args directly as
        # (client_addr, method, full_path, http_version, status_code) — do not call
        # getMessage() or clear args, as that would break the formatter. Scrub only
        # the full_path component (index 2) which contains the query string.
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            lst = list(record.args)
            lst[2] = _scrub(str(lst[2]))
            record.args = tuple(lst)
            return True

        # Standard records: getMessage() merges msg + args into the final string.
        # Store the scrubbed result back and clear args to avoid double-formatting.
        record.msg = _scrub(record.getMessage())
        record.args = None
        # Tracebacks are rendered after filters run, so pre-render and scrub them;
        # Formatter.format() reuses a populated exc_text instead of re-rendering.
        if record.exc_info and not record.exc_text:
            record.exc_text = _scrub(
                logging.Formatter().formatException(record.exc_info)
            )
        return True
