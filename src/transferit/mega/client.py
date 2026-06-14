"""
``Transferit`` — the stateful high-level client.

One instance owns:

    * a long-lived :class:`MegaAPI` (connection pool + optional sid)
    * a monotonic per-session ``fileno`` counter for WS uploads
    * user-configurable default options

Use it as a context manager for explicit cleanup::

    with Transferit(default_sender="me@x.com", default_expiry="7d") as tx:
        url  = tx.upload("report.pdf").url
        url2 = tx.upload("slides.pdf")          # reuses session + httpx pool
        meta = tx.metadata(url)
        tx.download(url2, "./dl")

A throw-away one-shot also works — the ``MegaAPI`` will be garbage-collected
along with the instance::

    url = Transferit().upload("file.pdf").url
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Iterable

from .actions.download import do_download
from .actions.info import do_info
from .actions.metadata import do_metadata
from .actions.upload import do_upload
from .api import MegaAPI
from .models import DownloadResult, TransferInfo, TransferNode, UploadResult
from .upload import DEFAULT_CONCURRENCY

log = logging.getLogger(__name__)  # noqa: F401


ProgressCallback = Callable[[int, int], None]


class Transferit:
    """
    Stateful client for transfer.it.

    Parameters
    ----------
    api
        A pre-configured :class:`MegaAPI` to share.  If ``None`` a fresh
        one is constructed.  The instance takes ownership only when it
        created the ``MegaAPI`` — an injected one will NOT be closed by
        :meth:`close`.
    default_sender
        Default ``sender`` email for uploads (overridable per call).
    default_expiry
        Default ``expiry`` for uploads — int (seconds) or duration string
        (``"7d"``, ``"2h30m"``) — overridable per call.
    default_concurrency
        Default number of WebSocket connections per file upload.
    default_parallel
        Default number of files uploaded simultaneously per transfer.  Leave
        as ``None`` (the default) to have it match ``max(2, len(usc))`` at
        upload time — the same rule bdl4.js's ``ulQueue.setSize`` uses.

    Thread safety
    -------------
    Safe to call from multiple threads concurrently.  The monotonic upload
    ``fileno`` counter is protected by a local lock; the ephemeral session
    handshake is idempotent and synchronised inside ``MegaAPI`` itself.
    """

    def __init__(
        self,
        *,
        api: MegaAPI | None = None,
        default_sender: str | None = None,
        default_expiry: int | str | None = None,
        default_concurrency: int = DEFAULT_CONCURRENCY,
        default_parallel: int | None = None,
    ) -> None:
        self._api: MegaAPI = api if api is not None else MegaAPI()
        self._owns_api: bool = api is None
        self._fileno: int = 0
        self._fileno_lock = threading.Lock()

        self.default_sender = default_sender
        self.default_expiry = default_expiry
        self.default_concurrency = default_concurrency
        self.default_parallel = default_parallel

    # ---- context manager ----

    def __enter__(self) -> "Transferit":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session (if we own it)."""
        if self._owns_api:
            self._api.close()

    # ---- accessors ----

    @property
    def api(self) -> MegaAPI:
        """Underlying :class:`MegaAPI` — escape hatch for low-level calls."""
        return self._api

    # ---- internal ----

    def _next_fileno(self) -> int:
        with self._fileno_lock:
            self._fileno += 1
            return self._fileno

    # ---- upload ----

    def upload(
        self,
        path: str | Path,
        *,
        title: str | None = None,
        message: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        expiry: int | str | None = None,
        notify_expiry: bool = False,
        max_downloads: int | None = None,
        recipients: list[str] | None = None,
        schedule: int | None = None,
        concurrency: int | None = None,
        parallel: int | None = None,
        exclude: Iterable[str] | None = None,
        on_start: Callable[[int, int], None] | None = None,
        on_progress: ProgressCallback | None = None,
        on_file_start: Callable[[int, Path, int], None] | None = None,
        on_file_progress: Callable[[int, Path, int, int], None] | None = None,
        on_file_done: Callable[[int, Path, int], None] | None = None,
    ) -> UploadResult:
        """
        Upload ``path`` (file or folder) and return an :class:`UploadResult`.

        ``concurrency`` is the number of WebSocket connections **per file**;
        ``parallel`` is the number of files uploaded simultaneously.  The
        total socket count is roughly ``parallel × concurrency``.

        ``exclude`` is a sequence of :mod:`fnmatch` glob patterns; matching
        files or directories inside the uploaded folder are skipped.

        Every keyword defaulting to ``None`` falls back to the matching
        ``default_*`` attribute on this instance (if any).
        """
        return do_upload(
            self._api,
            path,
            fileno_provider=self._next_fileno,
            title=title,
            message=message,
            password=password,
            sender=sender if sender is not None else self.default_sender,
            expiry=expiry if expiry is not None else self.default_expiry,
            notify_expiry=notify_expiry,
            max_downloads=max_downloads,
            recipients=recipients,
            schedule=schedule,
            concurrency=concurrency
            if concurrency is not None
            else self.default_concurrency,
            parallel=parallel if parallel is not None else self.default_parallel,
            exclude=exclude,
            on_start=on_start,
            on_progress=on_progress,
            on_file_start=on_file_start,
            on_file_progress=on_file_progress,
            on_file_done=on_file_done,
        )

    # ---- download ----

    def download(
        self,
        url_or_xh: str,
        output_dir: str | Path,
        *,
        password: str | None = None,
        force: bool = False,
        aria2c: bool = False,
        on_start: Callable[[list[TransferNode], int], None] | None = None,
        on_file_start: Callable[[TransferNode, Path], None] | None = None,
        on_file_progress: Callable[[TransferNode, int, int], None] | None = None,
        on_file_done: Callable[[TransferNode, Path], None] | None = None,
        on_skip: Callable[[TransferNode, Path], None] | None = None,
    ) -> DownloadResult:
        """Mirror a transfer to disk.  Read-only — no session required."""
        return do_download(
            self._api,
            url_or_xh,
            output_dir,
            password=password,
            force=force,
            aria2c=aria2c,
            on_start=on_start,
            on_file_start=on_file_start,
            on_file_progress=on_file_progress,
            on_file_done=on_file_done,
            on_skip=on_skip,
        )

    # ---- info ----

    def info(
        self,
        url_or_xh: str,
        *,
        password: str | None = None,
    ) -> list[TransferNode]:
        """List every node in a transfer.  Read-only — no session required."""
        return do_info(self._api, url_or_xh, password=password)

    # ---- metadata ----

    def metadata(
        self,
        url_or_xh: str,
        *,
        password: str | None = None,
    ) -> TransferInfo:
        """Fetch transfer-level metadata (xi).  Read-only — no session required."""
        return do_metadata(self._api, url_or_xh, password=password)


__all__ = ["Transferit"]
