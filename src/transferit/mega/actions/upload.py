"""
``Transferit.upload`` implementation.

Stateless — the caller (``Transferit``) owns the ``MegaAPI`` instance,
has already created an ephemeral session on it, and supplies a
``fileno_provider`` so file numbers stay monotonic across calls on the
same session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Iterable

from ..api import SHARE_BASE, MegaAPI, MegaAPIError
from ..crypto import rand_a32
from ..models import UploadResult
from ..transfer import cast_expiry_seconds, parse_duration
from ..upload import (
    DEFAULT_CONCURRENCY,
    _ws_upload_one,
    build_remote_tree,
    walk_folder,
)

ProgressCallback = Callable[[int, int], None]
StartCallback = Callable[[int, int], None]
FileStartCallback = Callable[[int, Path, int], None]
FileDoneCallback = Callable[[int, Path, int], None]
FileProgressCallback = Callable[[int, Path, int, int], None]


def do_upload(
    api: MegaAPI,
    path: str | Path,
    *,
    fileno_provider: Callable[[], int],
    title: str | None = None,
    message: str | None = None,
    password: str | None = None,
    sender: str | None = None,
    expiry: int | str | None = None,
    notify_expiry: bool = False,
    max_downloads: int | None = None,
    recipients: list[str] | None = None,
    schedule: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    parallel: int | None = None,
    exclude: Iterable[str] | None = None,
    on_start: StartCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_file_start: FileStartCallback | None = None,
    on_file_progress: FileProgressCallback | None = None,
    on_file_done: FileDoneCallback | None = None,
) -> UploadResult:
    """
    Upload ``path`` (file or folder) into a fresh transfer and return an
    :class:`UploadResult`.  See :class:`Transferit.upload` for docstring.
    """
    expiry_seconds = parse_duration(expiry) if isinstance(expiry, str) else expiry
    expiry_seconds = cast_expiry_seconds(expiry_seconds)

    p = Path(path).expanduser().resolve()
    if p.is_dir():
        raw_files, dir_rel_paths = walk_folder(p, exclude=exclude)
        local_root = p
    elif p.is_file():
        raw_files = [p]
        dir_rel_paths = []
        local_root = p.parent
    else:
        raise FileNotFoundError(f"not a file or directory: {p}")

    if not raw_files:
        raise FileNotFoundError(f"{p} contains no files to upload")

    if title is None:
        title = p.name

    if notify_expiry and (not expiry_seconds or not sender):
        raise MegaAPIError("notify_expiry requires both expiry>0 and sender")
    if (message or password or (expiry_seconds and expiry_seconds > 0)) and not sender:
        raise MegaAPIError(
            "sender email is required when setting message / password / expiry"
        )
    if recipients and not sender:
        raise MegaAPIError("recipients require sender email")

    # bdl4.js sorts folder-drops by ascending size so the small-file pool
    # drains first.  One stat() per file, reused for total, sort, and the
    # per-file size passed to _ws_upload_one below.
    pairs = sorted(((f.stat().st_size, f) for f in raw_files), key=lambda x: x[0])
    sizes = [s for s, _ in pairs]
    files = [f for _, f in pairs]
    total_bytes = sum(sizes)

    if on_start:
        on_start(total_bytes, len(files))

    # create_ephemeral_session is idempotent + thread-safe — no-ops once a sid
    # is already set.  Called here (not in Transferit.upload) so on_start fires
    # before the ~500 ms handshake and the UI can render the real total right
    # away.
    api.create_ephemeral_session()

    xh, root_h, _ = api.create_transfer(title)
    dir_handles = build_remote_tree(api, root_h, dir_rel_paths)

    pools = api.upload_pools()
    effective_parallel = parallel if parallel else max(2, len(pools))

    def _pick_pool(sz: int) -> tuple[str, str]:
        for entry in pools:
            if len(entry) < 2:
                continue
            host, uri = entry[0], entry[1]
            limit = entry[2] if len(entry) > 2 else 0
            if not limit or sz <= limit:
                return host, uri
        raise MegaAPIError(f"no upload pool available: {pools!r}")

    per_file_sent = [0] * len(files)
    total_sent = [0]
    lim = max(1, min(effective_parallel, len(files)))
    sem = asyncio.Semaphore(lim)

    async def upload_one(i: int, f: Path, fsize: int, rel_parent: str) -> None:
        host, uri = _pick_pool(fsize)
        ul_key = rand_a32(6)
        idx = fileno_provider()

        async with sem:
            if on_file_start:
                on_file_start(idx, f, fsize)

            def _cb(sent: int, _tot: int) -> None:
                # Maintain a running total instead of re-summing per_file_sent
                # on every chunk ack — O(1) instead of O(N) per tick.
                delta = sent - per_file_sent[i]
                per_file_sent[i] = sent
                total_sent[0] += delta
                if on_progress:
                    on_progress(min(total_sent[0], total_bytes), total_bytes)
                if on_file_progress:
                    on_file_progress(idx, f, sent, fsize)

            token, macs = await _ws_upload_one(
                host,
                uri,
                f,
                ul_key,
                fileno=idx,
                concurrency=concurrency,
                size=fsize,
                progress=_cb,
            )

            # Ensure the overall tally matches even if the final ack arrived
            # before `_cb` observed the last byte.
            delta = fsize - per_file_sent[i]
            per_file_sent[i] = fsize
            total_sent[0] += delta
            if on_progress:
                on_progress(min(total_sent[0], total_bytes), total_bytes)

            target_h = dir_handles.get(rel_parent, root_h)
            # finalise_file is a blocking HTTP call; hop to a thread so other
            # concurrent uploads keep draining the event loop.
            await asyncio.to_thread(
                api.finalise_file, target_h, token, ul_key, macs, f.name
            )

            if on_file_done:
                on_file_done(idx, f, fsize)

    # Pre-compute rel_parents once (sync work) so it doesn't run inside the
    # event loop on every upload.
    if p.is_dir():
        rel_parents = [
            "" if (rp := f.parent.relative_to(local_root).as_posix()) == "." else rp
            for f in files
        ]
    else:
        rel_parents = [""] * len(files)

    async def run_all() -> None:
        await asyncio.gather(
            *(
                upload_one(i, f, sz, rp)
                for i, (f, sz, rp) in enumerate(zip(files, sizes, rel_parents))
            )
        )

    asyncio.run(run_all())

    extras_set = (
        any(
            v is not None and v != ""
            for v in (message, password, sender, expiry_seconds, max_downloads)
        )
        or notify_expiry
    )

    if extras_set:
        api.set_transfer_attributes(
            xh,
            title=title,
            message=message,
            password=password,
            sender=sender,
            expiry_seconds=expiry_seconds,
            notify_before_expiry_seconds=(3 * 864_000) if notify_expiry else None,
            max_downloads=max_downloads,
        )

    if recipients:
        for email in recipients:
            api.set_transfer_recipient(xh, email, schedule=schedule)

    api.close_transfer(xh)

    return UploadResult(
        xh=xh,
        url=f"{SHARE_BASE}/t/{xh}",
        title=title,
        total_bytes=total_bytes,
        file_count=len(files),
        folder_count=len(dir_rel_paths),
    )
