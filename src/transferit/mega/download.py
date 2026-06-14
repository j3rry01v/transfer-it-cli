"""
Pure helpers used by the download flow.

API calls (``fetch_transfer``, ``fetch_transfer_info``, ``get_download_url``)
are methods on :class:`MegaAPI`; this module only contains helpers that
don't touch the wire.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

from .crypto import a32_to_bytes, attr_key


def decrypt_file_to(
    in_path: Path,
    out_path: Path,
    key_a32: list[int],
    on_progress=None,
) -> None:
    """
    Read an already-downloaded encrypted file from ``in_path``, AES-CTR-decrypt,
    and write plaintext to ``out_path``.

    Same cipher setup as :func:`stream_decrypt_to_file` but reads from a local
    file instead of streaming from HTTP — useful when aria2c downloaded the
    encrypted bytes.
    """
    aes_key = attr_key(key_a32)
    nonce = a32_to_bytes(key_a32[4:6])
    ctr = Counter.new(64, prefix=nonce, initial_value=0)
    cipher = AES.new(aes_key, AES.MODE_CTR, counter=ctr)

    total = in_path.stat().st_size
    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with in_path.open("rb") as f_in, out_path.open("wb") as f_out:
        while True:
            chunk = f_in.read(1024 * 1024)
            if not chunk:
                break
            f_out.write(cipher.decrypt(chunk))
            written += len(chunk)
            if on_progress:
                on_progress(written, total)


def stream_decrypt_to_file(
    url: str,
    out_path: Path,
    key_a32: list[int],
    size: int,
    on_progress=None,
) -> None:
    """
    Stream encrypted bytes from ``url``, AES-CTR-decrypt on the fly, and
    write to ``out_path``.

        key   = attr_key(filekey_a32)          # XOR-reduced node key
        nonce = filekey_a32[4:6]               # 64-bit
        counter starts at 0 (16-byte blocks)
    """
    aes_key = attr_key(key_a32)
    nonce = a32_to_bytes(key_a32[4:6])
    ctr = Counter.new(64, prefix=nonce, initial_value=0)
    cipher = AES.new(aes_key, AES.MODE_CTR, counter=ctr)

    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=httpx.Timeout(None, connect=30.0)) as resp:
        resp.raise_for_status()
        with out_path.open("wb") as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                if not chunk:
                    continue
                fh.write(cipher.decrypt(chunk))
                written += len(chunk)
                if on_progress:
                    on_progress(written, size)


def compute_folder_paths(nodes: list[dict], root_handle: str) -> dict[str, str]:
    """Build a ``folder_handle → posix-relative-path`` map, root = ``""``."""
    paths: dict[str, str] = {root_handle: ""}
    pending = [n for n in nodes if n["t"] == 1 and n["h"] != root_handle]
    while pending:
        made = False
        for n in list(pending):
            if n["p"] in paths:
                parent = paths[n["p"]]
                paths[n["h"]] = (f"{parent}/" if parent else "") + (n["name"] or n["h"])
                pending.remove(n)
                made = True
        if not made:
            break
    return paths
