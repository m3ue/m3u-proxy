"""
HMAC signatures for the upstream URLs the proxy writes into rewritten
HLS playlists and DASH manifests.

The segment, file and variant-playlist routes fetch whatever URL they are
handed, so they only accept URLs carrying a signature the proxy produced
itself. Each signature also covers the stream ID the URL was issued for and
the route family it belongs to, so a URL signed in stream A's playlist cannot
be replayed under stream B's route (which would fetch it with B's headers).
The key is derived from API_TOKEN, which every worker sharing a deployment
already has; without a token a random per-process key is used.
"""

import base64
import functools
import hashlib
import hmac
import json
import secrets
from typing import Optional

from config import settings

_FALLBACK_KEY = secrets.token_bytes(32)


@functools.lru_cache(maxsize=4)
def _derive_key(api_token: str) -> bytes:
    # Rewritten playlists expose (url, signature) pairs, so a fast hash would let
    # anyone holding one test API_TOKEN guesses offline. PBKDF2 makes each guess
    # expensive; the result is cached, so this runs once per process.
    return hashlib.pbkdf2_hmac(
        "sha256", api_token.encode(), b"m3u-proxy-url-signing", 600_000
    )


def _signing_key() -> bytes:
    if settings.API_TOKEN:
        return _derive_key(settings.API_TOKEN)
    return _FALLBACK_KEY


# Route families a signature is valid for. A signature issued for one family
# is rejected by the others.
ROLE_HLS_SEGMENT = "hls-segment"
ROLE_DASH_FILE = "dash-file"
ROLE_DASH_BASE = "dash-base"


def hls_playlist_role(parent_stream_id: Optional[str]) -> str:
    """Variant playlist links also cover their `parent` query param."""
    return f"hls-playlist:{parent_stream_id or ''}"


def sign_url(url: str, *, stream_id: str, role: str) -> str:
    """Return a short URL-safe signature for an upstream URL issued by a stream."""
    payload = json.dumps([role, stream_id, url]).encode()
    digest = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def verify_url(
    url: str, signature: Optional[str], *, stream_id: str, role: str
) -> bool:
    """Check a signature produced by sign_url, in constant time."""
    if not signature:
        return False
    # Compare bytes: compare_digest raises on non-ASCII str input.
    expected = sign_url(url, stream_id=stream_id, role=role)
    return hmac.compare_digest(expected.encode(), signature.encode())
