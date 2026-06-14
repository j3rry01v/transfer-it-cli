"""
``MegaAPI`` — the low-level client for the MEGA bt7 API used by transfer.it.

Everything that touches the wire lives here: HTTP plumbing, the anonymous
ephemeral-account handshake, the transfer-container verbs (``xn`` / ``xp``
/ ``xc`` / ``xd``), transfer metadata (``xm`` / ``xr`` / ``xv`` / ``xi``),
and the read-side fetch endpoints (``f`` / ``g`` / ``usc``).

Pure utilities that don't need a session (``parse_xh``, ``derive_password``)
are exposed as ``@staticmethod`` on the same class so callers don't have
to reach into private modules.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time

import httpx

from .crypto import (
    a32_to_b64,
    a32_to_bytes,
    b64url_decode,
    b64url_encode,
    bytes_to_a32,
    condense_macs,
    decrypt_attr,
    encrypt_attr,
    encrypt_key_ecb,
    rand_a32,
)

API_BASE = "https://bt7.api.mega.co.nz/"
SHARE_BASE = "https://transfer.it"

log = logging.getLogger(__name__)


class MegaAPIError(RuntimeError):
    """
    Raised when the MEGA API returns a numeric error or malformed data.

    When the server returned a numeric code, it's available on :attr:`code`
    for programmatic inspection and :attr:`name` carries the canonical short
    label (``EKEY``, ``ENOENT``, …).  Pure client-side errors (shape mismatches,
    upload-completion failures) have ``code = None`` and ``name = ""``.

    Users generally don't construct these directly — the :class:`MegaAPI`
    client raises them with ``raise MegaAPIError(code=...)`` and the class
    looks up a friendly default message via :attr:`CODES`.  Callers can still
    provide a fully custom ``message``, in which case the code table is
    ignored::

        try:
            tx.metadata(url)
        except MegaAPIError as ex:
            if ex.code == -14:              # EKEY — password-protected
                ...

    Known codes come from MEGA's publicly-documented error enum, reverse-
    engineered from the web client and kept in sync here.
    """

    #: Server-side code → (canonical short name, friendly default message).
    #: Add to this table to surface new codes with nicer messages.
    CODES: dict[int, tuple[str, str]] = {
        -1: ("EINTERNAL", "server internal error"),
        -2: ("EARGS", "invalid arguments"),
        -3: ("EAGAIN", "server is busy — try again shortly"),
        -4: ("ERATELIMIT", "rate-limited by the server"),
        -5: ("EFAILED", "operation failed"),
        -6: ("ETOOMANY", "too many requests"),
        -7: ("ERANGE", "out of range"),
        -8: ("EEXPIRED", "transfer has expired"),
        -9: (
            "ENOENT",
            "transfer not found (wrong handle, or it was deleted / expired)",
        ),
        -10: ("ECIRCULAR", "circular reference"),
        -11: ("EACCESS", "access denied"),
        -12: ("EEXIST", "already exists"),
        -13: ("EINCOMPLETE", "incomplete request"),
        -14: ("EKEY", "this transfer is password-protected — pass a password"),
        -15: ("ESID", "invalid session — the ephemeral account may have been evicted"),
        -16: ("EBLOCKED", "transfer was blocked (abuse report)"),
        -17: ("EOVERQUOTA", "quota exceeded"),
        -18: ("ETEMPUNAVAIL", "temporarily unavailable"),
        -19: ("ETOOMANYCONNECTIONS", "too many connections"),
    }

    def __init__(self, message: str | None = None, *, code: int | None = None) -> None:
        self.code: int | None = code
        entry = self.CODES.get(code) if code is not None else None
        self.name: str = entry[0] if entry else ""
        if message is None:
            if entry is not None:
                message = entry[1]
            elif code is not None:
                message = f"API error {code}"
            else:
                message = "MEGA API error"
        super().__init__(message)

    @classmethod
    def from_code(cls, code: int) -> "MegaAPIError":
        """Preferred constructor when you only have the numeric code.

        Equivalent to ``MegaAPIError(code=code)``, exposed as a named factory
        so call sites read as "build an error from this code" rather than
        "construct with a magic keyword".
        """
        return cls(code=code)


_XH_RE = re.compile(r"(?:/t/|^)([A-Za-z0-9_-]{12})(?:[/?#]|$)")


def _user_agent() -> str:
    """Build the User-Agent string — includes the package version when available."""
    try:
        from . import __version__
    except ImportError:  # pragma: no cover
        __version__ = "0.0.0"
    return f"transferit-py/{__version__} (+https://github.com/viperadnan-git/transferit-py)"


class MegaAPI:
    """
    Stateful client for MEGA's bt7 cluster.

    Wraps a single ``httpx.Client`` (HTTP/1.1, connection-pooled) and an
    optional anonymous session.  Reuse one instance to share the pool and
    (for uploads) the ephemeral account.

    **Thread safety.**  Safe to call ``req()``/every wrapper method from
    multiple threads concurrently — the seqno counter and the one-shot
    ``create_ephemeral_session`` are protected with locks.  ``httpx.Client``
    is itself thread-safe.  Mutating ``.sid`` externally from several
    threads at once is still on you.
    """

    def __init__(self, base: str = API_BASE, *, timeout: float = 60.0) -> None:
        self.base = base
        self.seqno: int = secrets.randbelow(1_000_000_000)
        self.sid: str | None = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            http2=False,
            headers={"User-Agent": _user_agent()},
        )
        # Protects the seqno counter and the ephemeral-session handshake.
        # Individual HTTP requests can then proceed concurrently — httpx.Client
        # already pools connections in a thread-safe way.
        self._seqno_lock = threading.Lock()
        self._session_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MegaAPI":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Low-level request plumbing
    # ------------------------------------------------------------------

    def _next_seqno(self) -> int:
        with self._seqno_lock:
            self.seqno += 1
            return self.seqno

    def req(
        self,
        payload: dict | list[dict],
        *,
        x: str | None = None,
        pw: str | None = None,
    ) -> list | dict | int | str:
        """
        Issue a single bt7 command (or a bulk list).  ``x`` and ``pw`` are
        the query-string flavours used by read-side endpoints that scope
        access via the transfer handle instead of a session id.

        Returns the first element of the response array for a single-dict
        payload; the full list otherwise.  Raises :class:`MegaAPIError`
        on numeric errors (with a short retry on ``-3`` / EAGAIN).
        """
        params: dict[str, str | int] = {"id": self._next_seqno()}
        # x-based (transfer-handle) and sid-based (session) authentication
        # are mutually exclusive on bt7 — passing both yields -15/ESID.
        if x is not None:
            params["x"] = x
            if pw is not None:
                params["pw"] = pw
        elif self.sid:
            params["sid"] = self.sid

        body = payload if isinstance(payload, list) else [payload]
        verb = body[0].get("a", "?") if body and isinstance(body[0], dict) else "?"
        for attempt in range(5):
            log.debug("POST /cs a=%s attempt=%d params=%r", verb, attempt, params)
            r = self._http.post(self.base + "cs", params=params, json=body)
            r.raise_for_status()
            data = r.json()
            # The server may return a negative error code either bare
            # (`-9`) or wrapped in a single-element list (`[-9]`); positive
            # or zero ints (e.g. the `0` success reply from xc/xm) are
            # legitimate results.  Normalise the shape before raising.
            code = (
                data
                if isinstance(data, int)
                else (
                    data[0]
                    if isinstance(data, list)
                    and len(data) == 1
                    and isinstance(data[0], int)
                    else None
                )
            )
            if code is not None and code < 0:
                if code == -3 and attempt < 4:
                    log.debug(
                        "server returned -3 (EAGAIN); retrying in %ds", 1 + attempt
                    )
                    time.sleep(1 + attempt)
                    continue
                raise MegaAPIError.from_code(code)
            break
        return data[0] if isinstance(payload, dict) else data

    # ------------------------------------------------------------------
    # Anonymous ephemeral session
    # ------------------------------------------------------------------

    def create_ephemeral_session(self) -> list[int]:
        """
        Create an anonymous ephemeral MEGA account and attach the resulting
        ``sid`` to this client.  Returns the 128-bit master key as a 4-element
        a32 list (useful if you want to persist the account later).

        Thread-safe: if two threads race to call this, only one account is
        actually created — the second caller returns the same master key.
        To deliberately force a new account, clear ``self.sid`` first.
        """
        # Fast path: session already created.
        if self.sid is not None:
            log.debug("create_ephemeral_session: session already exists, skipping")
            return self._master_key  # type: ignore[attr-defined]

        with self._session_lock:
            # Double-checked: another thread may have beaten us to the lock.
            if self.sid is not None:
                return self._master_key  # type: ignore[attr-defined]

            master_key = rand_a32(4)
            pw_key = rand_a32(4)
            ssc = rand_a32(4)

            k_enc = encrypt_key_ecb(a32_to_bytes(pw_key), master_key)
            ssc_enc = encrypt_key_ecb(a32_to_bytes(master_key), ssc)
            ts = a32_to_bytes(ssc) + a32_to_bytes(ssc_enc)

            user_handle = self.req(
                {"a": "up", "k": a32_to_b64(k_enc), "ts": b64url_encode(ts)}
            )
            if not isinstance(user_handle, str):
                raise MegaAPIError(f"up returned unexpected: {user_handle!r}")

            res = self.req({"a": "us", "user": user_handle})
            if not isinstance(res, dict) or "tsid" not in res:
                raise MegaAPIError(f"us returned unexpected: {res!r}")

            tsid = b64url_decode(res["tsid"])
            check_enc = encrypt_key_ecb(
                a32_to_bytes(master_key), bytes_to_a32(tsid[:16])
            )
            if a32_to_bytes(check_enc) != tsid[-16:]:
                raise MegaAPIError("tsid verification failed")

            self.sid = res["tsid"]
            self._master_key = master_key
            log.info("created ephemeral account user_handle=%s", user_handle)
            return master_key

    # ------------------------------------------------------------------
    # Transfer container (xn / xp / xc / xd)
    # ------------------------------------------------------------------

    def create_transfer(self, name: str) -> tuple[str, str, list[int]]:
        """
        Create a new transfer container via ``{a:'xn'}``.

        Returns ``(xh, root_h, folder_key)``:
          * ``xh``         — 12-char transfer handle (appears in share URL)
          * ``root_h``     — 8-char root node handle (parent for uploads)
          * ``folder_key`` — 4-element a32 AES key for folder attributes
        """
        folder_key = rand_a32(4)
        attrs = {"name": name, "mtime": int(time.time())}
        at = b64url_encode(encrypt_attr(attrs, folder_key))
        k = a32_to_b64(folder_key)

        res = self.req({"a": "xn", "at": at, "k": k})
        if not (
            isinstance(res, list)
            and len(res) == 2
            and all(isinstance(s, str) for s in res)
        ):
            raise MegaAPIError(f"xn returned unexpected: {res!r}")
        xh, h = res
        return xh, h, folder_key

    def close_transfer(self, xh: str) -> None:
        """Close a transfer (``xc``) — it becomes read-only."""
        self.req({"a": "xc", "xh": xh})

    def delete_transfer(self, xh: str) -> None:
        """Delete a transfer (``xd``)."""
        self.req({"a": "xd", "xh": xh})

    def create_subfolder(self, parent_handle: str, name: str) -> str:
        """
        Create a sub-folder and return its node handle.  Mirrors
        transferit.js ``mkdirp``: POST an ``xp`` with ``h='xxxxxxxx'``
        placeholder and ``t:1`` for folder creation.
        """
        folder_key = rand_a32(4)
        attrs = {"n": name}
        at = b64url_encode(encrypt_attr(attrs, folder_key))
        k = a32_to_b64(folder_key)
        res = self.req(
            {
                "a": "xp",
                "t": parent_handle,
                "n": [{"t": 1, "h": "xxxxxxxx", "a": at, "k": k}],
            }
        )
        if not isinstance(res, dict) or not res.get("f"):
            raise MegaAPIError(f"mkdir failed: {res!r}")
        return res["f"][0]["h"]

    def finalise_file(
        self,
        transfer_root: str,
        completion_token: bytes,
        ul_key: list[int],
        macs_ordered: list[list[int]],
        filename: str,
    ) -> dict:
        """
        Attach a freshly-uploaded file to a transfer via ``{a:'xp', t:0}``.
        ``completion_token`` is the 36-byte token returned by the WS upload.
        """
        mac = condense_macs(macs_ordered, ul_key)
        filekey = [
            ul_key[0] ^ ul_key[4],
            ul_key[1] ^ ul_key[5],
            ul_key[2] ^ mac[0] ^ mac[1],
            ul_key[3] ^ mac[2] ^ mac[3],
            ul_key[4],
            ul_key[5],
            mac[0] ^ mac[1],
            mac[2] ^ mac[3],
        ]

        at = b64url_encode(encrypt_attr({"n": filename}, filekey))
        k = a32_to_b64(filekey)
        h = b64url_encode(completion_token)

        res = self.req(
            {
                "a": "xp",
                "t": transfer_root,
                "n": [{"t": 0, "h": h, "a": at, "k": k}],
            }
        )
        if not isinstance(res, dict) or "f" not in res:
            raise MegaAPIError(f"xp returned unexpected: {res!r}")
        return res

    # ------------------------------------------------------------------
    # Transfer attributes / recipients (xm / xr / xv / xrf)
    # ------------------------------------------------------------------

    def set_transfer_attributes(
        self,
        xh: str,
        *,
        title: str | None = None,
        message: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        expiry_seconds: int | None = None,
        notify_before_expiry_seconds: int | None = None,
        max_downloads: int | None = None,
    ) -> object:
        """
        Wrap ``{a:'xm', xh, t, m, pw, se, e, en, mc}``.  Only non-None fields
        are sent.  ``password`` is hashed via PBKDF2 against the xh-derived
        salt, matching ``createPassword`` in transferit.js.
        """
        payload: dict[str, object] = {"a": "xm", "xh": xh}

        if title is not None:
            payload["t"] = b64url_encode(title.strip().encode("utf-8"))
        if message is not None:
            payload["m"] = b64url_encode(message.strip().encode("utf-8"))
        if sender is not None:
            sender = sender.strip()
            if sender:
                payload["se"] = sender
        if password is not None:
            pw = password.strip()
            if pw:
                payload["pw"] = self.derive_password(xh, pw)
        if expiry_seconds is not None and expiry_seconds > 0:
            payload["e"] = int(expiry_seconds)
        if notify_before_expiry_seconds is not None:
            payload["en"] = (
                notify_before_expiry_seconds
                if notify_before_expiry_seconds > 1
                else 3 * 864_000
            )
        if max_downloads is not None and max_downloads > 0:
            payload["mc"] = int(max_downloads)

        return self.req(payload)

    def set_transfer_recipient(
        self,
        xh: str,
        email: str,
        *,
        schedule: int | None = None,
        execution: int | None = None,
        recipient_handle: str | None = None,
    ) -> object:
        """Wrap ``{a:'xr', xh, rh?, e, s?, ex?}``.  Returns ``[0, rh]``."""
        payload: dict[str, object] = {"a": "xr", "xh": xh, "e": email.strip()}
        if recipient_handle:
            payload["rh"] = recipient_handle
        if schedule is not None:
            payload["s"] = int(schedule)
        if execution is not None:
            payload["ex"] = int(execution)
        return self.req(payload)

    def validate_password(self, xh: str, pw_token: str) -> bool:
        """Validate a PBKDF2 password token via ``{a:'xv'}``."""
        return self.req({"a": "xv", "xh": xh, "pw": pw_token}) == 1

    # ------------------------------------------------------------------
    # Fetch (f / xi / g / usc)
    # ------------------------------------------------------------------

    def fetch_transfer(
        self, xh: str, *, password: str | None = None
    ) -> tuple[list[dict], str | None]:
        """
        List every node in a transfer (``{a:'f'}``).

        Returns ``(nodes, pw_token)``.  Each node is a dict with:
            ``{h, p, t, s, ts, name, k (a32), raw}``

        ``t=1`` is a folder, ``t=0`` a file.  ``name`` is the decrypted
        attribute, or ``None`` if decryption failed.

        Numeric server errors bubble up as :class:`MegaAPIError`; check
        ``.code`` to react programmatically (e.g. ``-14`` = password required).
        """
        pw_token = self._resolve_pw(xh, password)
        try:
            data = self.req({"a": "f", "c": 1, "r": 1}, x=xh, pw=pw_token)
        except MegaAPIError as ex:
            raise self._translate_protected(ex, pw_token) from ex

        if isinstance(data, dict):
            resp = data
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            resp = data[0]
        else:
            raise MegaAPIError(f"fetch returned unexpected: {data!r}")

        nodes: list[dict] = []
        for n in resp.get("f", []):
            k_a32 = bytes_to_a32(b64url_decode(n["k"])) if n.get("k") else []
            attrs = decrypt_attr(n["a"], k_a32) if n.get("a") and k_a32 else None
            nodes.append(
                {
                    "h": n["h"],
                    "p": n.get("p", ""),
                    "t": n["t"],
                    "s": n.get("s"),
                    "ts": n.get("ts"),
                    "k": k_a32,
                    "name": (attrs or {}).get("n") or (attrs or {}).get("name"),
                    "raw": n,
                }
            )
        return nodes, pw_token

    def fetch_transfer_info(self, xh: str) -> dict:
        """
        Transfer-level metadata (``{a:'xi'}``):
            ``{t, se, m, pw, z, zp, size:[bytes, files, folders, _, _]}``

        ``t`` / ``m`` are base64url UTF-8; this method decodes them into
        ``title`` / ``message`` convenience keys alongside the originals.

        **No password required** — ``xi`` returns basic metadata for any
        transfer, even password-protected ones, provided the ``x=`` query
        parameter is NOT attached.  Use :meth:`fetch_transfer` for the
        pw-gated file listing.
        """
        # Deliberately no ``x=`` / ``pw=``: attaching ``x=`` turns xi into
        # an access grant and the server then enforces pw → -14/EKEY.
        data = self.req({"a": "xi", "xh": xh})

        if isinstance(data, dict):
            resp = data
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            resp = data[0]
        else:
            raise MegaAPIError(f"xi returned unexpected: {data!r}")

        info = dict(resp)
        if "t" in info and isinstance(info["t"], str):
            try:
                info["title"] = b64url_decode(info["t"]).decode("utf-8")
            except UnicodeDecodeError:
                info["title"] = None
        if "m" in info and isinstance(info["m"], str):
            try:
                info["message"] = b64url_decode(info["m"]).decode("utf-8")
            except UnicodeDecodeError:
                info["message"] = None

        raw_size = info.get("size") or [0, 0, 0, 0, 0]
        info["total_bytes"] = raw_size[0] if len(raw_size) > 0 else 0
        info["file_count"] = raw_size[1] if len(raw_size) > 1 else 0
        info["folder_count"] = raw_size[2] if len(raw_size) > 2 else 0
        info["password_protected"] = bool(info.get("pw"))
        info["zip_handle"] = info.get("z")
        info["zip_pending"] = bool(info.get("zp"))
        return info

    def get_download_url(
        self, xh: str, node_handle: str, *, pw_token: str | None = None
    ) -> dict:
        """Wrap ``{a:'g', n:<handle>, g:1, ssl:1}``.  Returns ``{g, s, at, fa, ...}``."""
        try:
            data = self.req(
                {"a": "g", "n": node_handle, "g": 1, "ssl": 1},
                x=xh,
                pw=pw_token,
            )
        except MegaAPIError as ex:
            raise self._translate_protected(ex, pw_token) from ex

        if isinstance(data, dict):
            resp = data
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            resp = data[0]
        else:
            raise MegaAPIError(f"g returned unexpected: {data!r}")
        if "g" not in resp:
            raise MegaAPIError(f"g error: {resp}")
        return resp

    def upload_pools(self) -> list:
        """Return the WS upload pool list from ``{a:'usc'}``."""
        return self.req({"a": "usc"})

    # ------------------------------------------------------------------
    # Static utilities (no API call)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_xh(url_or_xh: str) -> str:
        """
        Accept ``https://transfer.it/t/xxxxxxxxxxxx`` / ``/t/xxxxxxxxxxxx`` /
        bare xh — return the 12-char handle.
        """
        s = url_or_xh.strip()
        m = _XH_RE.search(s)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{12}", s):
            return s
        raise ValueError(f"can't extract transfer handle from {url_or_xh!r}")

    @staticmethod
    def derive_password(xh: str, password: str) -> str:
        """
        PBKDF2-SHA256 (100 000 iterations, 32-byte output) with xh-derived
        salt — matches ``createPassword`` in transferit.js.
        """
        xh_bytes = b64url_decode(xh)
        salt = xh_bytes[-6:] * 3
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.strip().encode("utf-8"), salt, 100_000, 32
        )
        return b64url_encode(dk)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_pw(self, xh: str, password: str | None) -> str | None:
        """Derive a PBKDF2 token from the plain password.  Returns ``None``
        if no password was given.  Doesn't round-trip to the server — the
        downstream fetch call does that for free, and translates ``-14``
        into ``wrong transfer password`` when a token was supplied."""
        if password is None:
            return None
        return self.derive_password(xh, password)

    def _translate_protected(
        self, ex: "MegaAPIError", pw_token: str | None
    ) -> "MegaAPIError":
        """Turn a raw ``-14 / EKEY`` into a more specific message based on
        whether we had a password token to begin with."""
        if ex.code == -14:
            if pw_token is not None:
                return MegaAPIError("wrong transfer password", code=-14)
            # no pw supplied → default "pass a password" message from CODES is fine
        return ex
