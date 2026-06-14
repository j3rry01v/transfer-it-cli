"""
WebSocket upload pipeline.

Fan-out matches MEGA's ``WsPoolMgr`` / ``WsUploadMgr`` behaviour from
``bdl4.js``: one pool per file (picked by size from ``usc``), up to
``DEFAULT_CONCURRENCY`` concurrent WS connections per pool, shared
chunk queue, ``bufferedAmount < 1.5 MB`` back-pressure gate.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import ssl
import sys
import struct
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import websockets

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is pulled by httpx in normal installs
    certifi = None

from .api import MegaAPIError
from .crypto import (
    CHUNKMAP,
    ONE_MB,
    crc32b,
    encrypt_chunk_and_mac,
)

if TYPE_CHECKING:
    from .api import MegaAPI

log = logging.getLogger(__name__)
DEBUG_WS = os.environ.get("TRANSFERIT_DEBUG_WS") == "1"


def _ssl_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _debug_ws(message: str) -> None:
    if DEBUG_WS:
        print(f"[transferit-ws] {message}", file=sys.stderr, flush=True)

# Matches ulmanager.ulDefConcurrency in bdl4.js — WS fan-out per pool.
DEFAULT_CONCURRENCY: int = 8

# Matches the ws.bufferedAmount < 1_500_000 gate in bdl4.js sendchunk().
WS_BUFFER_LIMIT: int = 1_500_000

# Per-chunk ack deadline — matches the 10 s timeout in enforcetimeouts().
ACK_TIMEOUT: float = 10.0

# Delay before re-opening a WS that dropped — bdl4.js schedules reconnectat
# = now + 5 s.
RECONNECT_DELAY: float = 5.0


class _MsgType(enum.IntEnum):
    CHUNK_ACK = 1
    CHUNK_ALREADY = 2
    CRC_FAIL = 3
    COMPLETE = 4
    SHED = 5
    BACKOFF = 6
    CHUNK_ACK_ALT = 7


# ---------- chunking ----------


def iter_chunks(size: int) -> tuple[list[tuple[int, int]], bool]:
    """Compute MEGA chunkmap offsets/lengths and whether an empty tail frame is needed."""
    chunks: list[tuple[int, int]] = []
    pos = 0
    truncated_last = False
    while pos < size:
        nominal = CHUNKMAP.get(pos, ONE_MB)
        remaining = size - pos
        if remaining < nominal:
            chunks.append((pos, remaining))
            pos += remaining
            truncated_last = True
        else:
            chunks.append((pos, nominal))
            pos += nominal
            truncated_last = False
    need_empty_tail = (size == 0) or (not truncated_last)
    return chunks, need_empty_tail


# ---------- single-file WebSocket upload ----------


class _WsDisconnect(Exception):
    """Internal signal: this worker's WS needs to reconnect (with chunk replay)."""


async def _ws_upload_one(
    ws_host: str,
    ws_uri: str,
    path: Path,
    ul_key: list[int],
    *,
    fileno: int = 1,
    concurrency: int = DEFAULT_CONCURRENCY,
    size: int | None = None,
    progress=None,
) -> tuple[bytes, list[list[int]]]:
    """
    Upload a single file across up to ``concurrency`` WebSockets.  Returns
    ``(completion_token, chunk_macs_ordered_by_offset)``.

    ``fileno`` must be unique per file within a MEGA session — the server
    tracks upload state by ``(session, fileno)``, not per WS connection.

    Matches bdl4.js ``WsUploadMgr`` semantics: per-chunk 10 s ack deadline,
    on WS close the in-flight chunks are prepended back to the queue
    (``toresend``), and the socket re-opens after a 5 s delay.
    """
    url = f"wss://{ws_host}/{ws_uri}"
    _debug_ws(f"upload start file={path} size={size if size is not None else path.stat().st_size} url={url}")
    if size is None:
        size = path.stat().st_size

    chunk_offsets, need_empty_tail = iter_chunks(size)
    work_queue: list[tuple[int, int]] = list(chunk_offsets)
    if need_empty_tail:
        work_queue.append((size, 0))
    total_chunks = len(work_queue)

    q_lock = asyncio.Lock()
    file_lock = asyncio.Lock()
    macs_by_offset: dict[int, list[int]] = {}
    # Keyed by pos; popped on first ack, so duplicate acks become no-ops.
    unacked_lengths: dict[int, int] = dict(chunk_offsets)
    if need_empty_tail:
        unacked_lengths[size] = 0
    completion_token: list[bytes | None] = [None]
    done = asyncio.Event()
    bytes_acked = [0]
    progress_lock = asyncio.Lock()

    fh = path.open("rb")

    async def _take_chunk() -> tuple[int, int] | None:
        async with q_lock:
            if work_queue:
                return work_queue.pop(0)
        return None

    async def _prepend(chunks: list[tuple[int, int]]) -> None:
        if not chunks:
            return
        async with q_lock:
            work_queue[:0] = chunks

    async def _read_and_encrypt(pos: int, length: int) -> bytes:
        async with file_lock:
            fh.seek(pos)
            data = fh.read(length)
        ct, mac = encrypt_chunk_and_mac(data, ul_key, pos)
        macs_by_offset[pos] = mac
        return ct

    async def _record_ack(pos: int) -> None:
        length = unacked_lengths.pop(pos, None)
        if length is None:
            return
        async with progress_lock:
            bytes_acked[0] += length
            if progress:
                progress(min(bytes_acked[0], size), size)

    async def worker(worker_id: int) -> None:
        # pos -> (length, ack_deadline). Both pieces move together, so one
        # dict keeps them in sync and iteration hits cache.
        in_flight: dict[int, tuple[int, float]] = {}
        loop = asyncio.get_running_loop()

        async def _drop_in_flight() -> None:
            if in_flight:
                await _prepend([(p, length) for p, (length, _) in in_flight.items()])
                in_flight.clear()

        async def _recv_loop(ws) -> None:
            async for msg in ws:
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                mview = bytes(msg)
                if len(mview) < 9:
                    continue
                body = mview[:-4]
                mcrc = struct.unpack_from("<I", mview, len(mview) - 4)[0]
                if crc32b(body) != mcrc:
                    raise MegaAPIError("ws CRC mismatch on server msg")
                mtype = struct.unpack_from("<b", body, 12)[0]
                if mtype < 0:
                    raise MegaAPIError(f"server signalled upload error type={mtype}")
                mpos = struct.unpack_from("<Q", body, 4)[0]
                _debug_ws(f"worker {worker_id}: recv type={mtype} pos={mpos} bytes={len(mview)}")
                if mtype in (
                    _MsgType.CHUNK_ACK,
                    _MsgType.CHUNK_ALREADY,
                    _MsgType.CHUNK_ACK_ALT,
                ):
                    in_flight.pop(mpos, None)
                    await _record_ack(mpos)
                elif mtype == _MsgType.CRC_FAIL:
                    raise MegaAPIError(
                        f"server reports chunk CRC fail at offset {mpos}"
                    )
                elif mtype == _MsgType.COMPLETE:
                    tlen = body[13]
                    completion_token[0] = bytes(body[14 : 14 + tlen])
                    done.set()
                    return
                elif mtype == _MsgType.SHED:
                    # "shedding connections" — drop this WS and reconnect.
                    raise _WsDisconnect("server requested reconnect")
                elif mtype == _MsgType.BACKOFF:
                    await asyncio.sleep(max(0, mpos) / 1000)

        while not done.is_set():
            try:
                _debug_ws(f"worker {worker_id}: connecting")
                async with websockets.connect(
                    url,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=60,
                    ssl=_ssl_context(),
                ) as ws:
                    _debug_ws(f"worker {worker_id}: connected")
                    recv_task = asyncio.create_task(_recv_loop(ws))
                    try:
                        while not done.is_set():
                            # bdl4.js enforcetimeouts() runs every ~10 s —
                            # only scan in_flight when there's something in it.
                            if in_flight:
                                now = loop.time()
                                for pos, (_, dl) in list(in_flight.items()):
                                    if now > dl:
                                        raise _WsDisconnect(f"ack timeout pos={pos}")

                            if recv_task.done():
                                recv_task.result()
                                if not done.is_set():
                                    raise _WsDisconnect("recv loop ended")

                            chunk = await _take_chunk()
                            if chunk is None:
                                await asyncio.sleep(0.1)
                                continue

                            pos, length = chunk

                            while not done.is_set():
                                transport = getattr(ws, "transport", None)
                                buffered = (
                                    getattr(transport, "_buffer_size", 0)
                                    if transport
                                    else 0
                                )
                                if buffered < WS_BUFFER_LIMIT:
                                    break
                                await asyncio.sleep(0.01)

                            if done.is_set():
                                # Another worker raced us to completion.
                                await _prepend([chunk])
                                break

                            ct = await _read_and_encrypt(pos, length)
                            _debug_ws(f"worker {worker_id}: send pos={pos} len={length}")
                            header = bytearray(20)
                            struct.pack_into("<I", header, 0, fileno)
                            struct.pack_into("<Q", header, 4, pos)
                            struct.pack_into("<I", header, 12, length)
                            struct.pack_into(
                                "<I",
                                header,
                                16,
                                crc32b(ct, crc32b(bytes(header[:16]))),
                            )
                            in_flight[pos] = (length, loop.time() + ACK_TIMEOUT)
                            await ws.send(bytes(header))
                            if ct:
                                await ws.send(ct)
                    finally:
                        if not recv_task.done():
                            recv_task.cancel()
                            try:
                                await recv_task
                            except (asyncio.CancelledError, Exception):
                                pass
            except asyncio.CancelledError:
                await _drop_in_flight()
                raise
            except (_WsDisconnect, websockets.ConnectionClosed, OSError) as ex:
                _debug_ws(f"worker {worker_id}: reconnect reason={ex}")
                log.info(
                    "ws worker %d: %s — reconnecting in %.0fs",
                    worker_id,
                    ex,
                    RECONNECT_DELAY,
                )
                await _drop_in_flight()
                if done.is_set():
                    return
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception:
                _debug_ws(f"worker {worker_id}: hard failure")
                await _drop_in_flight()
                done.set()
                raise

    n = max(1, min(concurrency, total_chunks))
    workers = [asyncio.create_task(worker(i)) for i in range(n)]
    done_task = asyncio.create_task(done.wait())
    try:
        await asyncio.wait([done_task, *workers], return_when=asyncio.FIRST_COMPLETED)
        # Workers only finish on cancellation or hard error; surface it.
        for w in workers:
            if w.done() and not w.cancelled():
                w.result()
    finally:
        done_task.cancel()
        for w in workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(done_task, *workers, return_exceptions=True)
        fh.close()

    if completion_token[0] is None:
        raise MegaAPIError("upload ended without completion token")

    ordered_macs = [macs_by_offset[o] for o in sorted(macs_by_offset)]
    return completion_token[0], ordered_macs


# ---------- folder walker ----------


def walk_folder(
    root: Path,
    *,
    exclude: Iterable[str] | None = None,
) -> tuple[list[Path], list[str]]:
    """
    Walk a folder (non-symlink-following), return:
        files          — regular files, deterministic order
        dir_rel_paths  — sub-directories as POSIX-style relative paths,
                          parent-before-child, ready to feed to mkdir

    ``exclude`` is a sequence of :mod:`fnmatch` glob patterns.  Each directory
    basename, file basename, and full POSIX-relative path is tested against
    every pattern — any match skips the entry (directories are pruned, so
    their contents aren't walked at all).

    Empty folders are preserved.  Hidden / dotfiles are included unless
    matched by an ``exclude`` pattern.
    """
    if not root.is_dir():
        raise NotADirectoryError(root)

    patterns = list(exclude or ())

    def matches(name: str, rel: str) -> bool:
        return any(fnmatch(name, pat) or fnmatch(rel, pat) for pat in patterns)

    files: list[Path] = []
    dir_set: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        rel = base.relative_to(root)
        rel_posix = "" if str(rel) == "." else rel.as_posix()

        if patterns:
            # Prune in-place so os.walk never descends into excluded trees.
            dirnames[:] = [
                d
                for d in dirnames
                if not matches(d, f"{rel_posix}/{d}" if rel_posix else d)
            ]

        if rel_posix:
            dir_set.add(rel_posix)

        for fn in filenames:
            f_rel = f"{rel_posix}/{fn}" if rel_posix else fn
            if patterns and matches(fn, f_rel):
                continue
            files.append(base / fn)

    dir_rel_paths = sorted(dir_set, key=lambda s: (s.count("/"), s))
    return files, dir_rel_paths


def build_remote_tree(
    api: MegaAPI, root_handle: str, dir_rel_paths: list[str]
) -> dict[str, str]:
    """Materialise a remote folder tree; return ``rel_posix_path → handle``."""
    handles: dict[str, str] = {"": root_handle}
    for rel in dir_rel_paths:  # parents precede children
        parent_rel, _, name = rel.rpartition("/")
        parent_h = handles.get(parent_rel, root_handle)
        handles[rel] = api.create_subfolder(parent_h, name)
    return handles
