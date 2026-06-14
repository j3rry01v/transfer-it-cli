# transferit

A clean command-line client for transfer.it with a fast direct MEGA backend, browser fallback, Rich terminal UI, folder uploads, and optional aria2c acceleration for downloads.

## Features

- One command: `transferit`
- Fast default MEGA backend with no browser required
- Browser backend retained as fallback
- File and folder uploads
- Password, sender, message, expiry, max-download, recipient, schedule, exclude, concurrency, and JSON options for MEGA uploads
- Transfer downloads with optional password support
- Machine-readable JSON output for MEGA upload/download/info/metadata
- Transfer metadata and file-tree inspection
- Configurable retries and browser fallback prompt
- Rich progress UI
- Optional aria2c acceleration for MEGA and browser downloads
- Persistent config stored in `~/.config/transfer-it-cli/config.json`

## Install

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

Install Playwright Chromium for browser fallback:

```bash
python3 -m playwright install chromium
```

Optional aria2c for faster downloads:

```bash
# macOS
brew install aria2

# Ubuntu/Debian
sudo apt-get install aria2
```

## Usage

Upload a file or folder:

```bash
transferit upload /path/to/file.zip
transferit upload /path/to/folder
```

Password-protect an upload:

```bash
transferit upload /path/to/file.zip --password "secret" --sender me@example.com
```

Add upload metadata and limits:

```bash
transferit upload ./project \
  --title "Project files" \
  --message "Latest build" \
  --sender me@example.com \
  --expiry 7d \
  --max-downloads 5
```

Email recipients, optionally scheduled:

```bash
transferit upload big.mp4 \
  --sender me@example.com \
  --recipient alice@example.com \
  --recipient bob@example.com \
  --schedule 2026-04-25T09:00
```

Tune MEGA uploads or exclude folder content:

```bash
transferit upload ./project --concurrency 8 --parallel 4 -x .git -x '__pycache__' -x '*.pyc'
```

JSON upload result:

```bash
transferit upload --json /path/to/file.zip
```

Force a backend:

```bash
transferit upload --backend mega /path/to/file.zip
transferit upload --backend browser /path/to/file.zip
```

Download a transfer:

```bash
transferit download "https://transfer.it/t/abc123def456"
transferit download abc123def456
```

Download to a folder:

```bash
transferit download --output-dir ~/Downloads "https://transfer.it/t/abc123def456"
```

Password-protected transfer:

```bash
transferit download --password "secret" "https://transfer.it/t/abc123def456"
```

JSON download result:

```bash
transferit download --json abc123def456
```

Browser backend download with aria2c:

```bash
transferit download --backend browser --aria2c "https://transfer.it/t/abc123def456"
```

MEGA backend download with aria2c:

```bash
transferit download --aria2c "https://transfer.it/t/abc123def456"
```

Browser backend download without aria2c:

```bash
transferit download --backend browser --no-aria2c "https://transfer.it/t/abc123def456"
```

Show transfer info:

```bash
transferit info "https://transfer.it/t/abc123def456"
transferit info --json abc123def456
```

Show only transfer metadata:

```bash
transferit metadata "https://transfer.it/t/abc123def456"
transferit metadata --json abc123def456
```

Show version:

```bash
transferit --version
```

Configure defaults:

```bash
transferit config
transferit config --show
transferit config --reset
```

## Defaults

```json
{
  "upload_backend": "mega",
  "download_backend": "mega",
  "aria2c_enabled": true,
  "download_dir": "~/Downloads",
  "retry_count": 3,
  "prompt_browser_fallback": true
}
```

## Backend Notes

`mega` is the default. It talks directly to transfer.it's MEGA backend, uploads through WebSockets, downloads through streaming HTTP, and decrypts locally.

`browser` uses Playwright automation and is kept as a fallback if the direct method stops working.

`aria2c` can be enabled for both backends. In MEGA mode, aria2c downloads the encrypted blob to a temporary file and transferit decrypts it locally. In browser mode, aria2c downloads the direct URL extracted by Playwright.

Browser upload currently supports simple file uploads only. MEGA-only upload options such as password, sender, message, expiry, recipients, schedule, exclude, concurrency, and JSON are intentionally rejected in browser mode so metadata is not silently dropped.

## License And Attribution

This project is MIT licensed. The direct MEGA backend is adapted from the MIT-licensed `transferit-py` project by Adnan Ahmad. See `THIRD_PARTY_NOTICES.md`.
