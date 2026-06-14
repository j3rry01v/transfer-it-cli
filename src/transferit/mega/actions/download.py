"""``Transferit.download`` implementation — stateless; accepts a MegaAPI."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from ..api import MegaAPI
from ..download import compute_folder_paths, decrypt_file_to, stream_decrypt_to_file
from ..models import DownloadResult, TransferNode


_active_aria2c_process: subprocess.Popen | None = None


def terminate_active_aria2c_process(timeout: float = 3.0) -> bool:
    """Terminate the active MEGA aria2c process, if one is running."""
    global _active_aria2c_process
    proc = _active_aria2c_process
    if proc is None or proc.poll() is not None:
        _active_aria2c_process = None
        return False
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    _active_aria2c_process = None
    return True


def _size_to_bytes(value: str) -> int:
    value = value.strip()
    units = (
        ("TiB", 1024**4),
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("KiB", 1024),
        ("TB", 1000**4),
        ("GB", 1000**3),
        ("MB", 1000**2),
        ("KB", 1000),
        ("B", 1),
    )
    for suffix, multiplier in units:
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * multiplier)
    try:
        return int(float(value))
    except ValueError:
        return 0


def _parse_aria2c_progress(line: str) -> tuple[int, int] | None:
    # Example: [#2c1434 68MiB/10GiB(0%) CN:16 DL:9.7MiB ETA:17m56s]
    if "[#" not in line or "/" not in line:
        return None
    for part in line.strip().split():
        if "/" not in part or "(" not in part:
            continue
        downloaded_raw, total_raw = part.split("/", 1)
        downloaded_raw = downloaded_raw.replace("[#", "").replace("[", "")
        total_raw = total_raw.split("(", 1)[0]
        downloaded = _size_to_bytes(downloaded_raw)
        total = _size_to_bytes(total_raw)
        if total > 0:
            return downloaded, total
    return None


def _download_one_stream(
    url: str,
    out_path: Path,
    key_a32: list[int],
    size: int,
    on_progress,
) -> None:
    """Stream + decrypt via httpx (default, no aria2c)."""
    stream_decrypt_to_file(url, out_path, key_a32, size, on_progress=on_progress)


def _download_one_aria2c(
    url: str,
    out_path: Path,
    key_a32: list[int],
    size: int,
    on_progress,
) -> None:
    """Download encrypted blob via aria2c, then decrypt locally."""
    global _active_aria2c_process
    temp_path = out_path.with_name(out_path.name + ".encrypted")
    aria2_control_path = Path(str(temp_path) + ".aria2")
    try:
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        _active_aria2c_process = subprocess.Popen(
            [
                "aria2c",
                "--no-conf=true",
                "--continue=true",
                "--max-connection-per-server=16",
                "--split=16",
                "--min-split-size=1M",
                "--console-log-level=error",
                "--summary-interval=1",
                "--file-allocation=none",
                "--max-tries=5",
                "--retry-wait=5",
                "--timeout=60",
                "--connect-timeout=60",
                "--human-readable=true",
                "--show-console-readout=true",
                "--allow-overwrite=true",
                "--auto-file-renaming=false",
                "--dir", str(temp_path.parent),
                "--out", temp_path.name,
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        last_update = 0.0
        assert _active_aria2c_process.stdout is not None
        for line in _active_aria2c_process.stdout:
            parsed = _parse_aria2c_progress(line)
            now = time.monotonic()
            if parsed and now - last_update >= 0.5:
                downloaded, total = parsed
                on_progress(min(downloaded, size), size or total)
                last_update = now

        return_code = _active_aria2c_process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, "aria2c")

        on_progress(size, size)
        decrypt_file_to(temp_path, out_path, key_a32)
    finally:
        _active_aria2c_process = None
        if temp_path.exists():
            temp_path.unlink()
        if aria2_control_path.exists():
            aria2_control_path.unlink()


def do_download(
    api: MegaAPI,
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
    """
    Mirror a transfer into ``output_dir``.  Folder hierarchy is recreated.
    Existing files are skipped unless ``force=True``.

    When ``aria2c=True`` the encrypted blob is downloaded via the external
    **aria2c** utility before being decrypted locally — useful for faster
    downloads on high-latency links.  If aria2c is not installed, falls
    back to the built-in streaming decryption.
    """
    xh = MegaAPI.parse_xh(url_or_xh)
    out_root = Path(output_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    node_dicts, pw_token = api.fetch_transfer(xh, password=password)
    nodes = [TransferNode.from_dict(n) for n in node_dicts]

    root = next((n.handle for n in nodes if n.is_folder and not n.parent), None)
    folder_paths = compute_folder_paths(node_dicts, root) if root else {}

    files = [n for n in nodes if n.is_file]
    total_bytes = sum(n.size or 0 for n in files)
    if on_start:
        on_start(files, total_bytes)

    if aria2c and shutil.which("aria2c") is None:
        aria2c = False

    download_one = _download_one_aria2c if aria2c else _download_one_stream

    paths: list[str] = []
    skipped: list[str] = []

    for n in files:
        rel = folder_paths.get(n.parent, "")
        out_path = out_root / rel / (n.name or n.handle)
        paths.append(str(out_path))

        if out_path.exists() and not force:
            skipped.append(str(out_path))
            if on_skip:
                on_skip(n, out_path)
            continue

        dl = api.get_download_url(xh, n.handle, pw_token=pw_token)
        size = dl["s"]

        if on_file_start:
            on_file_start(n, out_path)

        def _cb(d: int, t: int, _n=n) -> None:
            if on_file_progress:
                on_file_progress(_n, d, t)

        download_one(dl["g"], out_path, n.key, size, on_progress=_cb)

        if on_file_done:
            on_file_done(n, out_path)

    return DownloadResult(
        xh=xh,
        output_dir=str(out_root),
        paths=paths,
        skipped=skipped,
        total_bytes=total_bytes,
    )
