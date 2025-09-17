# Transfer.it CLI

Modern command-line tools for uploading and downloading files from transfer.it with beautiful terminal UI and real-time progress tracking.

## 🚀 Features

- **Beautiful Terminal UI** - Colorful progress bars and modern interface
- **Real-time Progress** - Live upload/download progress with speed and ETA
- **Unlimited File Sizes** - No arbitrary limits, handles GB+ files
- **Fast Downloads** - aria2c integration for multi-connection downloads
- **Smart Link Extraction** - Robust handling of transfer.it page changes
- **Cross-platform** - Works on macOS, Linux, and Windows

## 📦 Installation

```bash
git clone https://github.com/j3rry01v/transfer-it-cli.git
cd transfer-it-cli
pip install -r requirements.txt
playwright install chromium
```

### Optional: Install aria2c for faster downloads
```bash
# macOS
brew install aria2

# Ubuntu/Debian
sudo apt-get install aria2

# Windows
# Download from https://aria2.github.io/
```

## 🔧 Usage

### Upload Files
```bash
python transfer-it-uploader.py /path/to/file.zip
```

### Download Files
```bash
python transfer-it-downloader.py https://transfer.it/t/abc123def456
```

## 📋 Requirements

- Python 3.7+
- playwright >= 1.40.0
- rich >= 13.0.0
- aria2c (optional, for faster downloads)

## 🎯 Examples

**Upload a large file:**
```bash
python transfer-it-uploader.py ~/Movies/large-video.mkv
```

**Download with aria2c:**
```bash
python transfer-it-downloader.py https://transfer.it/t/xyz789
```

## 🛠️ How It Works

**Uploader:**
- Uses Playwright to automate transfer.it interface
- Monitors real upload progress from webpage
- Extracts share links with multiple fallback methods
- No file size limits - supports unlimited uploads

**Downloader:**
- Extracts direct download URLs from transfer.it
- Uses aria2c for fast, multi-connection downloads
- Falls back to Playwright if aria2c unavailable
- Real-time progress monitoring

## 🔧 Troubleshooting

**aria2c not found:**
Install aria2c using your package manager (see installation section)

**Upload stuck:**
The tool automatically handles stalled uploads and browser cleanup

**Link extraction failed:**
Debug screenshots are saved automatically for troubleshooting

## 📄 License

MIT License