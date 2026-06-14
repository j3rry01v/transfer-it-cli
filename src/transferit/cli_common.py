from __future__ import annotations

import datetime as dt
import json
import mimetypes
from typing import Iterable

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .mega.models import TransferInfo, TransferNode
from .mega.transfer import MAX_EXPIRY_SECONDS, MIN_EXPIRY_SECONDS, humanise_duration, parse_duration


console = Console()


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def humanise_bytes(num_bytes: int | None) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(num_bytes or 0)
    for idx, unit in enumerate(units):
        if value < 1024 or idx == len(units) - 1:
            return f"{value:,.1f} {unit}" if idx else f"{int(value):,} {unit}"
        value /= 1024
    return f"{value:,.1f} PiB"


def humanise_time(timestamp: int | float | None) -> str:
    if not timestamp:
        return "-"
    return dt.datetime.fromtimestamp(int(timestamp)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def guess_mime(name: str | None) -> str:
    if not name:
        return "application/octet-stream"
    return mimetypes.guess_type(name, strict=False)[0] or "application/octet-stream"


def parse_expiry(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    seconds = parse_duration(value)
    if not (MIN_EXPIRY_SECONDS <= seconds <= MAX_EXPIRY_SECONDS):
        raise ValueError(
            f"expiry must be between {MIN_EXPIRY_SECONDS}s and {humanise_duration(MAX_EXPIRY_SECONDS)}"
        )
    return seconds


def parse_schedule(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("schedule must be ISO 8601, for example 2026-04-25T09:00, or a Unix timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def metadata_panel(meta: TransferInfo) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(overflow="fold")
    table.add_row("handle", f"[bold blue]{meta.xh}[/bold blue]")
    table.add_row("url", f"[link={meta.url}]{meta.url}[/link]")
    if meta.title:
        table.add_row("title", f"[bold]{meta.title}[/bold]")
    if meta.sender:
        table.add_row("sender", meta.sender)
    table.add_row("password", "[yellow]required[/yellow]" if meta.password_protected else "[green]none[/green]")
    if meta.zip_handle:
        table.add_row("zip", f"{meta.zip_handle} [dim]({'building' if meta.zip_pending else 'ready'})[/dim]")
    table.add_row(
        "totals",
        f"{humanise_bytes(meta.total_bytes)} • [bold]{meta.file_count}[/bold] files, "
        f"[bold]{max(0, meta.folder_count - 1)}[/bold] subfolder(s)",
    )
    return Panel(table, title="transfer.it", title_align="right", border_style="blue", box=box.ROUNDED)


def print_metadata(meta: TransferInfo) -> None:
    console.print(metadata_panel(meta))
    if meta.message:
        console.print(Panel(meta.message, title="message", border_style="dim", title_align="left"))


def print_node_tree(nodes: Iterable[TransferNode]) -> None:
    nodes = list(nodes)
    if not nodes:
        console.print("[yellow]transfer contains no files[/yellow]")
        return

    root = next((node for node in nodes if node.is_folder and not node.parent), None)
    if root is None:
        console.print("[yellow]transfer has no root folder[/yellow]")
        return

    children_by_parent: dict[str, list[TransferNode]] = {}
    for node in nodes:
        if node.handle == root.handle:
            continue
        children_by_parent.setdefault(node.parent, []).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda node: (node.is_file, (node.name or "").lower()))

    table = Table(show_header=True, show_edge=False, box=None, pad_edge=False, header_style="bold", expand=False)
    table.add_column("name", no_wrap=True, overflow="fold")
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("mime", style="magenta", no_wrap=True)
    table.add_column("uploaded", no_wrap=True)
    table.add_column("handle", style="dim", no_wrap=True)

    def add_row(node: TransferNode, prefix: str) -> None:
        name = node.name or node.handle
        label = f"[bold blue]{name}/[/bold blue]" if node.is_folder else name
        if prefix:
            label = f"[dim]{prefix}[/dim]{label}"
        if node.is_folder:
            table.add_row(label, "", "", humanise_time(node.timestamp), node.handle)
        else:
            table.add_row(label, humanise_bytes(node.size or 0), guess_mime(node.name), humanise_time(node.timestamp), node.handle)

    add_row(root, "")

    def walk(parent: str, ancestor_prefix: str) -> None:
        children = children_by_parent.get(parent, [])
        for idx, child in enumerate(children):
            is_last = idx == len(children) - 1
            connector = "└── " if is_last else "├── "
            add_row(child, ancestor_prefix + connector)
            if child.is_folder:
                walk(child.handle, ancestor_prefix + ("    " if is_last else "│   "))

    walk(root.handle, "")
    console.print()
    console.print(table)
