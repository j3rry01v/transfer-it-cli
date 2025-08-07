# Transfer.it File Uploader

A simple command-line tool to upload files to transfer.it using browser automation with Playwright.

## Features

- Upload files of any size to transfer.it
- Real-time progress monitoring
- Automatic share link generation
- Error handling with screenshots
- Cross-platform support (macOS, Linux, Windows)

## Installation

1. Clone this repository:
```bash
git clone https://github.com/yourusername/transfer-it-uploader.git
cd transfer-it-uploader
```

2. Create a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install playwright
playwright install chromium
```

## Usage

```bash
python3 transfer-it-uploader.py /path/to/your/file
```

### Example

```bash
python3 transfer-it-uploader.py ~/Downloads/document.pdf
```

The tool will:
1. Open transfer.it in a headless browser
2. Upload your file
3. Monitor the upload progress
4. Generate and return a shareable link

## Requirements

- Python 3.7+
- Playwright
- Chromium browser (installed via Playwright)

## How it Works

This tool uses Playwright to automate a Chromium browser and interact with the transfer.it web interface. It:

1. Navigates to transfer.it
2. Selects and uploads your file
3. Monitors the upload progress
4. Captures the generated share link
5. Returns the link for sharing

## Error Handling

If an error occurs during upload, the tool will:
- Display an error message
- Save a screenshot as `transfer_it_error.png` for debugging
- Exit with an error code

## License

MIT License - feel free to use and modify as needed.

## Contributing

Pull requests are welcome! Please feel free to submit issues or improvements.