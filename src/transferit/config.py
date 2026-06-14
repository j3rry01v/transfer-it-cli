#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
from pathlib import Path


APP_NAME = "transfer-it-cli"
VALID_BACKENDS = {"mega", "browser"}


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / APP_NAME
    return Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


DEFAULT_CONFIG = {
    "upload_backend": "mega",
    "download_backend": "mega",
    "aria2c_enabled": True,
    "download_dir": default_download_dir(),
    "retry_count": 3,
    "prompt_browser_fallback": True,
}


def normalize_backend(value: str | None, fallback: str = "mega") -> str:
    value = (value or fallback).strip().lower()
    return value if value in VALID_BACKENDS else fallback


def expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def normalize_config(raw: dict | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)

    cfg["upload_backend"] = normalize_backend(cfg.get("upload_backend"), "mega")
    cfg["download_backend"] = normalize_backend(cfg.get("download_backend"), "mega")
    cfg["aria2c_enabled"] = bool(cfg.get("aria2c_enabled", True))
    cfg["download_dir"] = expand_path(str(cfg.get("download_dir") or default_download_dir()))
    try:
        cfg["retry_count"] = max(1, int(cfg.get("retry_count", 3)))
    except (TypeError, ValueError):
        cfg["retry_count"] = 3
    cfg["prompt_browser_fallback"] = bool(cfg.get("prompt_browser_fallback", True))
    return cfg


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return normalize_config()
    try:
        with path.open("r", encoding="utf-8") as fh:
            return normalize_config(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return normalize_config()


def save_config(config: dict) -> dict:
    cfg = normalize_config(config)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    return cfg


def reset_config() -> dict:
    return save_config(DEFAULT_CONFIG)


def env_backend(name: str) -> str | None:
    return os.environ.get(name) or os.environ.get("TRANSFER_IT_BACKEND")


def resolve_upload_backend(cli_backend: str | None, config: dict) -> str:
    return normalize_backend(cli_backend or env_backend("TRANSFER_IT_UPLOAD_BACKEND") or config.get("upload_backend"), "mega")


def resolve_download_backend(cli_backend: str | None, config: dict) -> str:
    return normalize_backend(cli_backend or env_backend("TRANSFER_IT_DOWNLOAD_BACKEND") or config.get("download_backend"), "mega")


def prompt_yes_no(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{message} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")
