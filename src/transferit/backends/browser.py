"""Browser backend entry points used by the CLI."""

from __future__ import annotations

from ..core.download import download_from_transfer_it, download_with_browser_native
from ..core.upload import upload_to_transfer_it, upload_to_transfer_it_simple

__all__ = [
    "upload_to_transfer_it",
    "upload_to_transfer_it_simple",
    "download_from_transfer_it",
    "download_with_browser_native",
]
