"""Low-level crypto primitives shared by every higher-level module.

The transfer.it / MEGA file-sharing protocol uses a custom combination of
AES-128-CTR (for file encryption) and a CCM-style CBC-MAC (for integrity).
All helpers here are pure functions — no I/O, no API calls.
"""

from __future__ import annotations

import json
import secrets
import struct
import zlib
from base64 import urlsafe_b64decode, urlsafe_b64encode

from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter


# --- chunk sizing (matches FileUploadReader.chunkmap in bdl4.js) ---
# First 8 chunks grow 128 KiB → 1 MiB; everything after is 1 MiB.
def _build_chunkmap() -> dict[int, int]:
    res: dict[int, int] = {}
    p, dp = 0, 0
    while dp < 1048576:
        dp += 131072
        res[p] = dp
        p += dp
    return res


CHUNKMAP: dict[int, int] = _build_chunkmap()
ONE_MB: int = 1048576


# ---------- base64url helpers ----------


def b64url_encode(b: bytes) -> str:
    return urlsafe_b64encode(b).rstrip(b"=").decode()


def b64url_decode(s: str | bytes) -> bytes:
    if isinstance(s, str):
        s = s.encode()
    return urlsafe_b64decode(s + b"=" * ((-len(s)) % 4))


# ---------- a32 (big-endian uint32 array) helpers ----------


def a32_to_bytes(a: list[int]) -> bytes:
    return struct.pack(f">{len(a)}I", *(x & 0xFFFFFFFF for x in a))


def bytes_to_a32(b: bytes) -> list[int]:
    if len(b) % 4:
        b += b"\x00" * (4 - len(b) % 4)
    return list(struct.unpack(f">{len(b) // 4}I", b))


def a32_to_b64(a: list[int]) -> str:
    return b64url_encode(a32_to_bytes(a))


def b64_to_a32(s: str) -> list[int]:
    return bytes_to_a32(b64url_decode(s))


def rand_a32(n: int) -> list[int]:
    return list(struct.unpack(f">{n}I", secrets.token_bytes(n * 4)))


# ---------- AES-ECB key wrap (MEGA's encrypt_key / decrypt_key) ----------


def encrypt_key_ecb(key_bytes: bytes, data_a32: list[int]) -> list[int]:
    aes = AES.new(key_bytes, AES.MODE_ECB)
    out: list[int] = []
    for i in range(0, len(data_a32), 4):
        block = a32_to_bytes(data_a32[i : i + 4])
        out.extend(bytes_to_a32(aes.encrypt(block)))
    return out


def decrypt_key_ecb(key_bytes: bytes, data_a32: list[int]) -> list[int]:
    aes = AES.new(key_bytes, AES.MODE_ECB)
    out: list[int] = []
    for i in range(0, len(data_a32), 4):
        block = a32_to_bytes(data_a32[i : i + 4])
        out.extend(bytes_to_a32(aes.decrypt(block)))
    return out


# ---------- node attribute encryption ----------


def attr_key(key_a32: list[int]) -> bytes:
    """MEGA attr encryption key: [k0^k4, k1^k5, k2^k6, k3^k7].
    For 4-element folder keys this collapses to just k[:4] (tail = zeros)."""
    k = list(key_a32) + [0] * max(0, 8 - len(key_a32))
    return a32_to_bytes([k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7]])


def encrypt_attr(attrs: dict, key_a32: list[int]) -> bytes:
    raw = b"MEGA" + json.dumps(attrs, separators=(",", ":")).encode("utf-8")
    pad = (-len(raw)) % 16
    raw += b"\x00" * pad
    cipher = AES.new(attr_key(key_a32), AES.MODE_CBC, b"\x00" * 16)
    return cipher.encrypt(raw)


def decrypt_attr(enc_b64: str, key_a32: list[int]) -> dict | None:
    """Decrypt a MEGA attribute blob. Returns parsed JSON or None on failure."""
    raw = b64url_decode(enc_b64)
    if len(raw) % 16:
        raw += b"\x00" * (16 - len(raw) % 16)
    plain = AES.new(attr_key(key_a32), AES.MODE_CBC, b"\x00" * 16).decrypt(raw)
    if not plain.startswith(b"MEGA"):
        return None
    body = plain[4:].rstrip(b"\x00").rstrip()
    end = body.rfind(b"}")
    if end == -1:
        return None
    try:
        return json.loads(body[: end + 1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# ---------- per-chunk AES-CTR encryption + CBC-MAC ----------


def encrypt_chunk_and_mac(
    data: bytes, ul_key: list[int], byte_offset: int
) -> tuple[bytes, list[int]]:
    """
    Encrypt a chunk (<= 1 MiB) with AES-CTR and compute its CBC-MAC.
    Matches the encrypter.js worker from the MEGA web client.

        key    = ul_key[0..3]       (128-bit)
        nonce  = ul_key[4..5]       (64-bit, 8 bytes)
        CTR IV = nonce || counter   (counter counts 16-byte blocks)
        MAC IV = nonce || nonce     (reset per 1 MiB segment)

    Fast-path implementation: the CBC-MAC equals the last 16-byte block of
    an AES-CBC encryption of the zero-padded plaintext using ``mac_iv``,
    which pycryptodome does in native C.
    """
    assert len(data) <= ONE_MB, "caller must split reads into <= 1 MiB pieces"

    key_bytes = a32_to_bytes(ul_key[:4])
    nonce = a32_to_bytes(ul_key[4:6])  # 8 bytes

    # AES-CTR encryption
    initial_counter = byte_offset // 16
    ctr = Counter.new(64, prefix=nonce, initial_value=initial_counter)
    ciphertext = AES.new(key_bytes, AES.MODE_CTR, counter=ctr).encrypt(data)

    # CBC-MAC over the zero-padded plaintext
    if data:
        padded = data + b"\x00" * ((-len(data)) % 16)
        mac_iv = nonce + nonce
        cbc_out = AES.new(key_bytes, AES.MODE_CBC, mac_iv).encrypt(padded)
        mac_bytes = cbc_out[-16:]
    else:
        mac_bytes = nonce + nonce  # empty chunk: MAC = IV (never encrypted)

    return ciphertext, bytes_to_a32(mac_bytes)


def condense_macs(macs: list[list[int]], ul_key: list[int]) -> list[int]:
    """XOR each per-chunk MAC into the accumulator, AES-encrypt between."""
    acc = [0, 0, 0, 0]
    aes = AES.new(a32_to_bytes(ul_key[:4]), AES.MODE_ECB)
    for m in macs:
        for j in range(0, len(m), 4):
            acc = [acc[k] ^ m[j + k] for k in range(4)]
            acc = bytes_to_a32(aes.encrypt(a32_to_bytes(acc)))
    return acc


# ---------- CRC32b (zlib is the same polynomial MEGA's JS uses) ----------


def crc32b(data: bytes, init: int = 0) -> int:
    return zlib.crc32(data, init) & 0xFFFFFFFF
