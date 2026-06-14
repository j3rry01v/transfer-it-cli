"""``Transferit.info`` — list nodes; stateless."""

from __future__ import annotations

from ..api import MegaAPI
from ..models import TransferNode


def do_info(
    api: MegaAPI,
    url_or_xh: str,
    *,
    password: str | None = None,
) -> list[TransferNode]:
    """List every file + folder in a transfer.  Read-only, no session needed."""
    xh = MegaAPI.parse_xh(url_or_xh)
    node_dicts, _ = api.fetch_transfer(xh, password=password)
    return [TransferNode.from_dict(n) for n in node_dicts]
