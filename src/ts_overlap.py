"""
Overlap trimming for live MPEG-TS silent reconnects.

Some providers close live connections periodically. When the proxy silently
reconnects, the new connection usually starts from the provider's rolling
buffer, i.e. a few seconds *behind* the last byte already sent to the client.
Forwarding that overlap makes players replay those seconds (a "jump-back").

Restreamers serving a rolling buffer typically resend byte-identical data, so
we remember the tail of what was delivered and, after a reconnect, look for it
at the start of the new connection. On a match, everything up to and including
the tail is dropped and the stream continues seamlessly from the exact next
byte. If no match is found within a bounded window, the held data is released
unchanged (the previous behaviour).
"""

from typing import Optional, Tuple

# Result values returned by OverlapTrimmer.feed()
TRIM_PENDING = "pending"
TRIM_MATCHED = "matched"
TRIM_NO_MATCH = "no_match"


class OverlapTrimmer:
    """Tracks delivered bytes and trims replayed overlap after a reconnect.

    Usage (per client generator):
      - record(chunk) after every chunk yielded to the client
      - arm() when silently reconnecting to the same upstream URL
      - while pending, pass each upstream chunk through feed(); emit whatever
        bytes it returns
    """

    # Minimum tail length required to attempt a match. Shorter signatures
    # (e.g. runs of null/stuffing packets) risk false positives.
    MIN_SIGNATURE_BYTES = 188 * 10

    def __init__(
        self,
        signature_bytes: int,
        max_search_bytes: int,
        max_wait_seconds: float,
    ):
        self.signature_bytes = max(signature_bytes, self.MIN_SIGNATURE_BYTES)
        self.max_search_bytes = max_search_bytes
        self.max_wait_seconds = max_wait_seconds

        self._tail = b""
        self._signature: Optional[bytes] = None
        self._search_buffer = bytearray()
        self._search_started_at: Optional[float] = None
        self.last_trimmed_bytes = 0

    @property
    def pending(self) -> bool:
        return self._signature is not None

    def record(self, chunk: bytes) -> None:
        """Remember the most recent bytes delivered to the client."""
        if not chunk:
            return
        if len(chunk) >= self.signature_bytes:
            self._tail = bytes(chunk[-self.signature_bytes :])
        else:
            self._tail = (self._tail + chunk)[-self.signature_bytes :]

    def arm(self) -> bool:
        """Start looking for the delivered tail in the next upstream data.

        Returns False when not enough data has been delivered to form a
        reliable signature. Re-arming while already pending discards any held
        (never delivered) bytes; the next connection will replay them anyway.
        """
        self._search_buffer = bytearray()
        self._search_started_at = None
        if len(self._tail) < self.MIN_SIGNATURE_BYTES:
            self._signature = None
            return False
        self._signature = self._tail
        return True

    def feed(self, chunk: bytes, now: float) -> Tuple[bytes, str]:
        """Consume an upstream chunk while a trim is pending.

        Returns (bytes_to_emit, result) where result is TRIM_PENDING (hold,
        emit nothing yet), TRIM_MATCHED (overlap dropped, remainder returned)
        or TRIM_NO_MATCH (window exhausted, all held bytes returned as-is).
        """
        if self._signature is None:
            return chunk, TRIM_NO_MATCH

        if self._search_started_at is None:
            self._search_started_at = now
        self._search_buffer.extend(chunk)

        match_at = self._search_buffer.find(self._signature)
        if match_at != -1:
            cut = match_at + len(self._signature)
            self.last_trimmed_bytes = cut
            remainder = bytes(self._search_buffer[cut:])
            self._reset()
            return remainder, TRIM_MATCHED

        if (
            len(self._search_buffer) >= self.max_search_bytes
            or now - self._search_started_at >= self.max_wait_seconds
        ):
            held = bytes(self._search_buffer)
            self.last_trimmed_bytes = 0
            self._reset()
            return held, TRIM_NO_MATCH

        return b"", TRIM_PENDING

    def _reset(self) -> None:
        self._signature = None
        self._search_buffer = bytearray()
        self._search_started_at = None
