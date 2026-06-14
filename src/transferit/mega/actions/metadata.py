"""``Transferit.metadata`` — xi wrapper; stateless."""

from __future__ import annotations

from ..api import SHARE_BASE, MegaAPI
from ..models import TransferInfo


def do_metadata(
    api: MegaAPI,
    url_or_xh: str,
    *,
    password: str | None = None,
) -> TransferInfo:
    """
    Fetch xi metadata for a transfer.  Read-only, no session needed.

    ``password`` is accepted for API symmetry with :func:`do_info` /
    :func:`do_download` but is *not* required: ``xi`` exposes basic metadata
    (title, sender, size, password flag, zip status) for every transfer,
    including password-protected ones.  The password is therefore ignored.
    """
    del password  # xi is a password-free peek by design
    xh = MegaAPI.parse_xh(url_or_xh)
    raw = api.fetch_transfer_info(xh)
    return TransferInfo.from_dict(xh, raw, url=f"{SHARE_BASE}/t/{xh}")
