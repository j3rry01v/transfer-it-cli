#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..config import config_path, load_config, reset_config, save_config


console = Console()


def render_config(config: dict) -> None:
    table = Table(box=box.ROUNDED)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Setting", style="bold")
    table.add_column("Value", style="magenta")
    table.add_row("1", "Upload backend", config["upload_backend"])
    table.add_row("2", "Download backend", config["download_backend"])
    table.add_row("3", "aria2c enabled", "yes" if config["aria2c_enabled"] else "no")
    table.add_row("4", "Download folder", config["download_dir"])
    table.add_row("5", "Retry count", str(config["retry_count"]))
    table.add_row("6", "Browser fallback prompt", "yes" if config["prompt_browser_fallback"] else "no")
    console.print(Panel(table, title="Transfer.it CLI Config", border_style="blue"))
    console.print(f"[dim]Config file: {config_path()}[/dim]")


def ask_backend(label: str, current: str) -> str:
    while True:
        value = input(f"{label} backend [mega/browser] ({current}): ").strip().lower()
        if not value:
            return current
        if value in {"mega", "browser"}:
            return value
        console.print("[yellow]Enter 'mega' or 'browser'.[/yellow]")


def ask_bool(label: str, current: bool) -> bool:
    suffix = "Y/n" if current else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return current
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        console.print("[yellow]Enter yes or no.[/yellow]")


def ask_retry_count(current: int) -> int:
    while True:
        value = input(f"Retry count ({current}): ").strip()
        if not value:
            return current
        try:
            return max(1, int(value))
        except ValueError:
            console.print("[yellow]Enter a number 1 or higher.[/yellow]")


def ask_download_dir(current: str) -> str:
    value = input(f"Download folder ({current}): ").strip()
    if not value:
        return current
    return str(Path(value).expanduser())


def interactive_config() -> None:
    config = load_config()
    while True:
        console.clear()
        render_config(config)
        console.print("\n[cyan]Select 1-6 to edit, s to save, r to reset, q to quit.[/cyan]")
        choice = input("Choice: ").strip().lower()
        if choice == "1":
            config["upload_backend"] = ask_backend("Upload", config["upload_backend"])
        elif choice == "2":
            config["download_backend"] = ask_backend("Download", config["download_backend"])
        elif choice == "3":
            config["aria2c_enabled"] = ask_bool("Enable aria2c for browser downloads", config["aria2c_enabled"])
        elif choice == "4":
            config["download_dir"] = ask_download_dir(config["download_dir"])
        elif choice == "5":
            config["retry_count"] = ask_retry_count(config["retry_count"])
        elif choice == "6":
            config["prompt_browser_fallback"] = ask_bool("Ask before browser fallback", config["prompt_browser_fallback"])
        elif choice == "s":
            saved = save_config(config)
            console.print("[green]Config saved.[/green]")
            render_config(saved)
            return
        elif choice == "r":
            config = reset_config()
            console.print("[green]Config reset to defaults.[/green]")
            input("Press Enter to continue...")
        elif choice == "q":
            return
        else:
            console.print("[yellow]Unknown choice.[/yellow]")
            input("Press Enter to continue...")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="transferit config", description="Configure Transfer.it CLI defaults")
    parser.add_argument("--show", action="store_true", help="Show current configuration")
    parser.add_argument("--reset", action="store_true", help="Reset configuration to defaults")
    args = parser.parse_args(argv)

    if args.reset:
        render_config(reset_config())
        console.print("[green]Config reset.[/green]")
        return
    if args.show:
        render_config(load_config())
        return
    interactive_config()


if __name__ == "__main__":
    main()
