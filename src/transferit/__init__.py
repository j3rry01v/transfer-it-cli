"""transferit command-line and Python client package."""

from __future__ import annotations

try:
    from importlib.metadata import version
except ImportError:  # pragma: no cover
    from importlib_metadata import version  # type: ignore

try:
    __version__ = version("transfer-it-cli")
except Exception:  # pragma: no cover - editable/source tree fallback
    __version__ = "0.2.1"

try:
    from .mega.api import MegaAPI, MegaAPIError
    from .mega.client import Transferit
    from .mega.models import DownloadResult, TransferInfo, TransferNode, UploadResult
except ImportError:  # pragma: no cover - lets CLI show dependency hints cleanly
    MegaAPI = None
    MegaAPIError = RuntimeError
    Transferit = None
    DownloadResult = None
    TransferInfo = None
    TransferNode = None
    UploadResult = None

__all__ = [
    "__version__",
    "Transferit",
    "TransferInfo",
    "TransferNode",
    "UploadResult",
    "DownloadResult",
    "MegaAPI",
    "MegaAPIError",
]
