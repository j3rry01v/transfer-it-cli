"""MEGA direct backend entry points used by the CLI."""

from __future__ import annotations

from ..core.download import download_from_transfer_it_mega, download_mega_with_retries
from ..core.upload import upload_to_transfer_it_mega, upload_with_retries

__all__ = [
    "upload_to_transfer_it_mega",
    "upload_with_retries",
    "download_from_transfer_it_mega",
    "download_mega_with_retries",
]
