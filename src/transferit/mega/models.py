"""
Typed containers returned by the high-level ``Transferit`` client.

Raw API responses come back as dicts; the library wraps them in frozen
dataclasses so callers get attribute access, editor autocompletion, and
clear types instead of ``n.get("h")`` / ``n["s"] or 0`` idioms.

The raw dicts are preserved on ``.raw`` as an escape hatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TransferNode:
    """A single node (file or folder) inside a transfer listing."""

    handle: str
    """8-char MEGA node handle."""

    parent: str
    """Parent node handle (``""`` if this is the transfer root)."""

    kind: int
    """``0`` = file, ``1`` = folder."""

    name: str | None
    """Decrypted display name.  None if attrs couldn't be decrypted."""

    size: int | None
    """Byte size for files; ``None`` for folders."""

    timestamp: int | None
    """Upload / creation unix timestamp (seconds)."""

    key: list[int] = field(default_factory=list)
    """a32 (big-endian uint32s) key from the server — 8 elements for files,
    4 for folders.  Pass as-is to ``stream_decrypt_to_file``."""

    raw: dict = field(default_factory=dict, compare=False, repr=False)
    """Raw server node as returned by the ``f`` command."""

    @property
    def is_file(self) -> bool:
        return self.kind == 0

    @property
    def is_folder(self) -> bool:
        return self.kind == 1

    @classmethod
    def from_dict(cls, n: dict) -> "TransferNode":
        """Build from the dict shape returned by the ``{a:'f'}`` pipeline."""
        return cls(
            handle=n["h"],
            parent=n.get("p", ""),
            kind=n["t"],
            name=n.get("name"),
            size=n.get("s"),
            timestamp=n.get("ts"),
            key=n.get("k") or [],
            raw=n.get("raw", n),
        )

    def to_json_dict(self) -> dict:
        """Serialisable form for ``--json`` output (no raw blob, no binary key)."""
        return {
            "handle": self.handle,
            "parent": self.parent,
            "kind": "folder" if self.is_folder else "file",
            "name": self.name,
            "size": self.size,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class TransferInfo:
    """Transfer-level metadata from ``{a:'xi'}`` + the root node from ``f``."""

    xh: str
    """12-char transfer handle (the path segment of the share URL)."""

    url: str
    """Share URL (``https://transfer.it/t/<xh>``)."""

    root_handle: str | None
    """8-char handle of the transfer root folder."""

    title: str | None
    """Decoded transfer title (or None if never set via xm)."""

    sender: str | None
    """Sender's email if set via xm."""

    message: str | None
    """Decoded message body if set via xm."""

    password_protected: bool
    """True if the transfer requires a password."""

    zip_handle: str | None
    """Handle of the server-built zip bundle (or None)."""

    zip_pending: bool
    """True while the server is still assembling the zip."""

    total_bytes: int
    """Sum of all file sizes, as reported by xi.size[0]."""

    file_count: int
    """File count from xi.size[1]."""

    folder_count: int
    """Folder count from xi.size[2] (includes the root folder)."""

    raw: dict = field(default_factory=dict, compare=False, repr=False)
    """Raw xi response for fields not surfaced on the dataclass."""

    @classmethod
    def from_dict(
        cls,
        xh: str,
        raw: dict,
        *,
        url: str,
        root_handle: str | None = None,
    ) -> "TransferInfo":
        """Build from the dict shape returned by ``fetch_transfer_info``."""
        return cls(
            xh=xh,
            url=url,
            root_handle=root_handle,
            title=raw.get("title"),
            sender=raw.get("se"),
            message=raw.get("message"),
            password_protected=bool(raw.get("pw")),
            zip_handle=raw.get("z"),
            zip_pending=bool(raw.get("zp")),
            total_bytes=raw.get("total_bytes", 0),
            file_count=raw.get("file_count", 0),
            folder_count=raw.get("folder_count", 0),
            raw=raw,
        )

    def to_json_dict(self) -> dict:
        """Serialisable form for ``--json`` output (skips the raw response blob)."""
        return {
            "xh": self.xh,
            "url": self.url,
            "root_handle": self.root_handle,
            "title": self.title,
            "sender": self.sender,
            "message": self.message,
            "password_protected": self.password_protected,
            "zip_handle": self.zip_handle,
            "zip_pending": self.zip_pending,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "folder_count": self.folder_count,
        }


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Returned by ``Transferit.upload``.

    Serialises cleanly to JSON and also stringifies to the share URL so
    existing ``print(Transferit.upload(...))`` idioms keep working::

        result = Transferit.upload("file.pdf")
        str(result)          # "https://transfer.it/t/…"
        result.url           # same
        result.xh            # "xxxxxxxxxxxx"
    """

    xh: str
    """12-char transfer handle."""

    url: str
    """Share URL (``https://transfer.it/t/<xh>``)."""

    title: str
    """Title used when creating the transfer."""

    total_bytes: int
    """Total bytes uploaded."""

    file_count: int
    """Number of files uploaded."""

    folder_count: int
    """Number of sub-folders created (excludes the transfer root)."""

    def __str__(self) -> str:
        return self.url

    def to_json_dict(self) -> dict:
        return {
            "xh": self.xh,
            "url": self.url,
            "title": self.title,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "folder_count": self.folder_count,
        }


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Returned by ``Transferit.download`` — paths written plus counts."""

    xh: str
    output_dir: str
    """Absolute path of the output root (as a string for JSON-friendliness)."""

    paths: list[str]
    """Absolute paths of every file in the transfer, as strings.
    Files that existed on disk and were skipped (no ``--force``) are still
    listed so the result is stable regardless of disk state."""

    total_bytes: int
    skipped: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "xh": self.xh,
            "output_dir": self.output_dir,
            "paths": self.paths,
            "skipped": self.skipped,
            "total_bytes": self.total_bytes,
        }
