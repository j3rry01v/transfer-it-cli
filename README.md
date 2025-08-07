# Transfer.it CLI Uploader

A modern, colorful command-line tool to upload files to transfer.it with beautiful terminal UI and real-time progress tracking.

## Features

- 🎨 **Beautiful Terminal UI** - Colorful progress bars, styled tables, and modern interface
- 📊 **Real-time Progress Tracking** - Live upload progress with speed and ETA display
- 📁 **File Information Display** - Elegant file details with size and path
- 🔗 **Automatic Share Link Generation** - Clickable links in terminal
- ⚡ **Smart Size Parsing** - Supports GB, MB, KB with accurate progress calculation
- 🛡️ **Robust Error Handling** - Visual error indicators with debug screenshots
- 🌍 **Cross-platform Support** - Works on macOS, Linux, and Windows
- ⏱️ **Upload Timeout Protection** - 5-minute timeout to prevent hanging

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
pip install -r requirements.txt
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

### What You'll See

The tool provides a beautiful, modern interface with:

- 📁 **File Information Panel** - Shows file name, size, and path in a styled table
- 🚀 **Initialization Progress** - Spinner showing browser startup and page loading
- 📤 **Upload Progress Bar** - Real-time progress with:
  - Current uploaded size vs total size
  - Upload speed (MB/s)
  - ETA in H:M:S format
  - Visual progress bar with percentage
- 🎉 **Success Display** - Elegant results panel with clickable share link

### Sample Output

```
🚀 Transfer.it CLI Uploader

┌─────────────────── 📁 File Information ───────────────────┐
│ Property  │ Value                                         │
│ File Name │ document.pdf                                  │
│ File Size │ 15.30 MB                                      │
│ File Path │ /Users/username/Downloads/document.pdf        │
└───────────────────────────────────────────────────────────┘

🚀 Starting upload...
⠋ 📤 8.45 MB / 15.30 MB • 2.1 MB/s ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  55% ETA: 0:00:03

🎉 SUCCESS! Your file has been uploaded successfully!
┌─────────────────────────────────────────────────────────────┐
│ 📁 File       │ document.pdf                                │
│ 🔗 Share Link │ https://transfer.it/t/abc123def456          │
│ 📋 Status     │ ✅ Ready to share                           │
└─────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.7+
- Playwright (for browser automation)
- Rich (for beautiful terminal UI)
- Chromium browser (installed via Playwright)

## How it Works

This tool uses Playwright to automate a Chromium browser and interact with the transfer.it web interface. It:

1. Navigates to transfer.it
2. Selects and uploads your file
3. Monitors the upload progress
4. Captures the generated share link
5. Returns the link for sharing

## Error Handling

The tool includes comprehensive error handling with visual feedback:

- ❌ **Clear Error Messages** - Color-coded error display with emojis
- 📸 **Debug Screenshots** - Automatic screenshots saved for troubleshooting
- ⏱️ **Timeout Protection** - 5-minute upload timeout prevents hanging
- 🔄 **Fallback Methods** - Multiple approaches for link extraction
- 🎯 **Specific Error Types** - Different handling for file not found, upload failures, etc.

### Debug Files

- `transfer_it_error.png` - Screenshot when general errors occur
- `transfer_it_debug.png` - Screenshot when link extraction fails

## License

MIT License - feel free to use and modify as needed.

## Contributing

Pull requests are welcome! Please feel free to submit issues or improvements.