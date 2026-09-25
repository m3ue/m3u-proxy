"""
FFmpeg input options for HLS sources.

Shared by every place that hands a provider URL straight to FFmpeg
(broadcasts/DVR and pooled transcoding), so they relax the HLS demuxer's
extension checks the same way.
"""

from typing import List
from urllib.parse import urlparse


def is_hls_url(url: object) -> bool:
    """Return True when the URL's path (query string ignored) is an .m3u8 playlist."""
    return isinstance(url, str) and urlparse(url).path.lower().endswith(".m3u8")


def hls_extension_args(url: object) -> List[str]:
    """FFmpeg input options that let HLS segments use any file extension.

    Some providers disguise HLS segments as .jpg/.css. Since FFmpeg 8 the HLS
    demuxer checks segment extensions separately from the playlist
    (allowed_segment_extensions) and extension_picky rejects mismatches, so
    -allowed_extensions ALL alone is not enough. These options are HLS-only:
    FFmpeg aborts with "Option not found" if they are passed for other inputs,
    so an empty list is returned for non-HLS URLs.
    """
    if not is_hls_url(url):
        return []
    return [
        "-allowed_extensions",
        "ALL",
        "-allowed_segment_extensions",
        "ALL",
        "-extension_picky",
        "0",
    ]
