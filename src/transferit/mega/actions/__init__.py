"""
Implementation of the four :class:`~transferit.Transferit` operations.

One module per operation; :class:`Transferit` is a thin class in
``_client.py`` that forwards to these functions via ``staticmethod``.
"""

from .download import do_download
from .info import do_info
from .metadata import do_metadata
from .upload import do_upload

__all__ = ["do_upload", "do_download", "do_info", "do_metadata"]
