#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from . import __version__
from .cli_common import print_json, print_metadata, print_node_tree
from .core import config_cli, download, upload


console = Console()


def cmd_info(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="transferit info", description="Show transfer metadata and file list")
    parser.add_argument("link", help="transfer.it link or 12-character handle")
    parser.add_argument("-p", "--password", help="Password for protected transfers")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        from .mega.api import MegaAPIError
        from .mega.client import Transferit
    except ImportError as exc:
        console.print(f"[red]MEGA backend dependencies are missing: {exc}[/red]")
        return 1

    listing_error = None
    nodes = None
    with Transferit() as tx:
        meta = tx.metadata(args.link, password=args.password)
        try:
            nodes = tx.info(args.link, password=args.password)
        except MegaAPIError as exc:
            if exc.code == -14:
                listing_error = str(exc)
            else:
                raise

    if args.json:
        payload = {
            "metadata": meta.to_json_dict(),
            "nodes": [node.to_json_dict() for node in nodes] if nodes is not None else None,
        }
        if listing_error:
            payload["listing_error"] = listing_error
        print_json(payload)
        return 0

    print_metadata(meta)
    if nodes is None:
        console.print(f"\n[yellow]file listing hidden[/yellow] [dim]{listing_error}[/dim]")
        return 0
    print_node_tree(nodes)
    return 0


def cmd_metadata(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="transferit metadata", description="Show transfer metadata only")
    parser.add_argument("link", help="transfer.it link or 12-character handle")
    parser.add_argument("-p", "--password", help="Password for protected transfers")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        from .mega.client import Transferit
    except ImportError as exc:
        console.print(f"[red]MEGA backend dependencies are missing: {exc}[/red]")
        return 1

    with Transferit() as tx:
        meta = tx.metadata(args.link, password=args.password)

    if args.json:
        print_json(meta.to_json_dict())
    else:
        print_metadata(meta)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transferit",
        description="Upload and download transfer.it files using MEGA direct backend with browser fallback.",
    )
    parser.add_argument("-V", "--version", action="store_true", help="Show version and exit")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("upload", help="Upload a file or folder")
    sub.add_parser("download", help="Download a transfer link")
    sub.add_parser("config", help="Configure defaults")
    sub.add_parser("info", help="Show transfer metadata and file list")
    sub.add_parser("metadata", help="Show transfer metadata only")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_parser().print_help()
        return 1

    command, rest = argv[0], argv[1:]
    if command in {"-h", "--help"}:
        build_parser().print_help()
        return 0
    if command in {"-V", "--version"}:
        console.print(f"transferit {__version__}")
        return 0
    try:
        if command == "upload":
            upload.main(rest)
            return 0
        if command == "download":
            download.main(rest)
            return 0
        if command == "config":
            config_cli.main(rest)
            return 0
        if command == "info":
            return cmd_info(rest)
        if command == "metadata":
            return cmd_metadata(rest)
    except KeyboardInterrupt:
        console.print("[red]aborted[/red]")
        return 130
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 2
    except Exception as exc:
        try:
            from .mega.api import MegaAPIError
        except Exception:
            MegaAPIError = ()  # type: ignore[assignment]
        if isinstance(exc, MegaAPIError):
            console.print(f"[red]error:[/red] {exc}")
            return 1
        raise

    console.print(f"[red]Unknown command:[/red] {command}")
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
