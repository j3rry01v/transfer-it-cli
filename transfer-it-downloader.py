import sys
import os
import time
import signal
import atexit
import termios
import tty
import subprocess
import threading
import psutil 
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

console = Console()
browser_instance = None
original_terminal_settings = None
aria2c_process = None
download_file_path = None  # Track the file being downloaded

def setup_terminal_for_progress():
    """Configure terminal to allow Ctrl+C while minimizing interference"""
    global original_terminal_settings
    if sys.stdin.isatty():
        try:
            original_terminal_settings = termios.tcgetattr(sys.stdin.fileno())
            new_settings = termios.tcgetattr(sys.stdin.fileno())
            new_settings[3] = new_settings[3] & ~termios.ECHO
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, new_settings)
        except:
            pass

def restore_terminal_input():
    """Restore original terminal input settings"""
    global original_terminal_settings
    if original_terminal_settings and sys.stdin.isatty():
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_terminal_settings)
        except:
            pass

def kill_aria2c_processes():
    """Kill all aria2c processes"""
    try:
        # Method 1: Using psutil (reliable)
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if 'aria2c' in proc.info['name'] or (proc.info['cmdline'] and any('aria2c' in arg for arg in proc.info['cmdline'])):
                        console.print(f"[yellow]Terminating aria2c process (PID: {proc.info['pid']})[/yellow]")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except ImportError:
            pass
        
        # Method 2: Using system commands (fallback)
        subprocess.run(['pkill', '-f', 'aria2c'], capture_output=True)
        time.sleep(0.5)
        subprocess.run(['pkill', '-9', '-f', 'aria2c'], capture_output=True)
        
        # Method 3: Platform specific
        if sys.platform == "darwin":
            subprocess.run(['killall', 'aria2c'], capture_output=True)
        elif sys.platform.startswith('linux'):
            subprocess.run(['killall', 'aria2c'], capture_output=True)
            
    except Exception as e:
        console.print(f"[yellow]Warning: Could not kill aria2c processes: {e}[/yellow]")

def cleanup_partial_download():
    """Remove partial download files"""
    global download_file_path
    if download_file_path and os.path.exists(download_file_path):
        try:
            file_size_mb = os.path.getsize(download_file_path) / (1024 * 1024)
            console.print(f"[yellow]🗑️  Removing partial download ({file_size_mb:.2f} MB): {download_file_path}[/yellow]")
            os.remove(download_file_path)
            
            # Also remove aria2 control file if exists, Make this optional in future update via congif
            control_file = f"{download_file_path}.aria2"
            if os.path.exists(control_file):
                os.remove(control_file)
                
        except Exception as e:
            console.print(f"[yellow]Warning: Could not remove partial file: {e}[/yellow]")

def check_aria2c():
    """Check if aria2c is installed"""
    try:
        result = subprocess.run(['aria2c', '--version'], capture_output=True, text=True)
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    return False

def install_aria2c():
    """Provide instructions to install aria2c"""
    console.print("[red]❌ aria2c is not installed![/red]")
    console.print("\n[yellow]Please install aria2c first:[/yellow]")
    console.print("[cyan]• Ubuntu/Debian: sudo apt-get install aria2[/cyan]")
    console.print("[cyan]• macOS: brew install aria2[/cyan]")
    console.print("[cyan]• Fedora: sudo dnf install aria2[/cyan]")
    console.print("[cyan]• Arch: sudo pacman -S aria2[/cyan]")
    sys.exit(1)

def kill_existing_browsers(show_message=False):
    """Terminate existing browser processes to prevent resource leaks."""
    processes_found = False
    try:
        check_result = subprocess.run(['pgrep', '-f', 'chromium|chrome|playwright'], 
                                     capture_output=True, text=True)
        if check_result.returncode == 0 and check_result.stdout.strip():
            processes_found = True
        
        if processes_found and show_message:
            with Progress(
                SpinnerColumn(),
                TextColumn("🧹 Cleaning up existing browser processes..."),
                console=console,
                transient=True
            ) as cleanup_progress:
                cleanup_task = cleanup_progress.add_task("cleanup", total=None)
                subprocess.run(['pkill', '-f', 'chromium'], capture_output=True)
                subprocess.run(['pkill', '-f', 'chrome'], capture_output=True)
                subprocess.run(['pkill', '-f', 'playwright'], capture_output=True)
                time.sleep(1)
                subprocess.run(['pkill', '-9', '-f', 'chromium'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'chrome'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'playwright'], capture_output=True)
        elif processes_found:
            subprocess.run(['pkill', '-f', 'chromium'], capture_output=True)
            subprocess.run(['pkill', '-f', 'chrome'], capture_output=True)
            subprocess.run(['pkill', '-f', 'playwright'], capture_output=True)
            time.sleep(1)
            subprocess.run(['pkill', '-9', '-f', 'chromium'], capture_output=True)
            subprocess.run(['pkill', '-9', '-f', 'chrome'], capture_output=True)
            subprocess.run(['pkill', '-9', '-f', 'playwright'], capture_output=True)
    except Exception:
        try:
            if sys.platform == "darwin":
                subprocess.run(['killall', '-9', 'Chromium'], capture_output=True)
                subprocess.run(['killall', '-9', 'Google Chrome'], capture_output=True)
        except:
            pass

def full_cleanup(remove_partial=True):
    """Complete cleanup of all resources"""
    global browser_instance, aria2c_process
    
    console.print("\n[yellow]🧹 Cleaning up resources...[/yellow]")
    
    # Restore terminal
    restore_terminal_input()
    
    # Kill aria2c process if running
    if aria2c_process:
        try:
            console.print("[yellow]Terminating aria2c download...[/yellow]")
            aria2c_process.terminate()
            try:
                aria2c_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                aria2c_process.kill()
                aria2c_process.wait()
        except:
            pass
        aria2c_process = None
    
    # Kill any remaining aria2c processes
    kill_aria2c_processes()
    
    # Clean up partial downloads if requested - check line 90
    if remove_partial:
        cleanup_partial_download()
    
    # Close brwsr
    if browser_instance:
        try:
            browser_instance.close()
        except:
            pass
        browser_instance = None
    
    # Kill brwser process
    kill_existing_browsers(show_message=False)

def signal_handler(signum, frame):
    """Handle various signals gracefully"""
    signal_name = signal.Signals(signum).name
    console.print(f"\n[yellow]⚠️  Received {signal_name} signal - Cleaning up...[/yellow]")
    full_cleanup(remove_partial=True)
    console.print("[red]❌ Download cancelled[/red]")
    sys.exit(130 if signum == signal.SIGINT else 1)

# Register signal handlers for various interruption scenarios
signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # Termination
signal.signal(signal.SIGTSTP, signal_handler)  # Ctrl+Z
signal.signal(signal.SIGHUP, signal_handler)   # Terminal closed

# Cleanup on normal exit
atexit.register(lambda: full_cleanup(remove_partial=False))

def show_transfer_info(transfer_url):
    """Display transfer link information"""
    table = Table(box=box.ROUNDED)
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")
    table.add_row("Transfer Link", transfer_url)
    table.add_row("Status", "🔍 Analyzing...")
    console.print(Panel(table, title="📁 Transfer Information", border_style="blue"))

def extract_file_info(page):
    """Extract file information from the page"""
    file_info = {}
    try:
        file_name_elem = page.locator('.ready-to-download-box .link-info .title').first
        if file_name_elem.is_visible():
            file_info['name'] = file_name_elem.text_content().strip()
            console.print(f"[cyan]📄 File: {file_info['name']}[/cyan]")
        
        file_size_elem = page.locator('.ready-to-download-box .it-grid-info .size').first
        if file_size_elem.is_visible():
            file_info['size'] = file_size_elem.text_content().strip()
            console.print(f"[cyan]📊 Size: {file_info['size']}[/cyan]")
        
        file_count_elem = page.locator('.ready-to-download-box .it-grid-info .num').first
        if file_count_elem.is_visible():
            file_info['count'] = file_count_elem.text_content().strip()
            console.print(f"[cyan]📁 Files: {file_info['count']}[/cyan]")
    except:
        pass
    
    return file_info

def find_download_button(page):
    """Locate the download button on the page"""
    button_selectors = [
        'button.it-button.xl-size.js-download:has(span:has-text("Download all"))',
        'button.js-download',
        'button:has-text("Download all")',
        '.ready-to-download-box button.js-download'
    ]
    
    for selector in button_selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible():
                console.print("[green]✅ Download button found[/green]")
                return btn
        except:
            continue
    
    return None

def parse_aria2c_output(line):
    """Parse aria2c output to extract progress information"""
    try:
        # Parse lines like: [#2c1434 68MiB/10GiB(0%) CN:16 DL:9.7MiB ETA:17m56s]
        if '[#' in line and 'CN:' in line:
            parts = line.strip().split()
            for part in parts:
                if '/' in part and ('MiB' in part or 'GiB' in part or 'KiB' in part):
                    # find downloaded/total
                    downloaded_str = part.split('/')[0].replace('[#', '').replace('[', '')
                    total_str = part.split('/')[1].split('(')[0]
                    
                
                    def to_bytes(size_str):
                        if 'GiB' in size_str:
                            return float(size_str.replace('GiB', '')) * 1024 * 1024 * 1024
                        elif 'MiB' in size_str:
                            return float(size_str.replace('MiB', '')) * 1024 * 1024
                        elif 'KiB' in size_str:
                            return float(size_str.replace('KiB', '')) * 1024
                        else:
                            return 0
                    
                    downloaded = to_bytes(downloaded_str)
                    total = to_bytes(total_str)
                    
                    # find speed
                    speed = 0
                    for p in parts:
                        if p.startswith('DL:'):
                            speed_str = p.replace('DL:', '')
                            speed = to_bytes(speed_str)
                    
                    # find ETA
                    eta = ""
                    for p in parts:
                        if p.startswith('ETA:'):
                            eta = p.replace('ETA:', '')
                    
                    return {
                        'downloaded': downloaded,
                        'total': total,
                        'speed': speed,
                        'eta': eta
                    }
    except:
        pass
    return None

def download_with_aria2c(download_url, output_path, file_name, expected_size_bytes=0):
    """Download file using aria2c with custom progress tracking"""
    global aria2c_process, download_file_path
    
    download_file_path = output_path  # Track the file for cleanup
    
    console.print(f"\n[cyan]🚀 Starting download with aria2c...[/cyan]")
    console.print(f"[dim]Output: {output_path}[/dim]")
    console.print(f"[dim]Press Ctrl+C to cancel download and cleanup[/dim]\n")
    
    # aria2c command 
    cmd = [
        'aria2c',
        '--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        '--referer=https://transfer.it/',
        '--no-conf=true',
        '--continue=true',
        '--max-connection-per-server=16',
        '--split=16',
        '--min-split-size=1M',
        '--console-log-level=error', 
        '--file-allocation=none',
        '--summary-interval=1', 
        '--max-tries=5',
        '--retry-wait=5',
        '--timeout=60',
        '--connect-timeout=60',
        '--check-certificate=false',
        '--human-readable=true',
        '--show-console-readout=true',
        '--allow-overwrite=true',
        '--auto-file-renaming=false',
        '-d', os.path.dirname(output_path),
        '-o', os.path.basename(output_path),
        download_url
    ]
    
    try:
        # Start aria2c 
        aria2c_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        #  progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("•"),
            DownloadColumn(),
            TextColumn("•"),
            TransferSpeedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
            refresh_per_second=2
        ) as progress:
            
            task = progress.add_task(
                f"📥 Downloading {file_name}",
                total=expected_size_bytes if expected_size_bytes > 0 else None
            )
            
            # Monitor aria2c output
            last_update = time.time()
            download_completed = False
            
            for line in aria2c_process.stdout:
                if time.time() - last_update < 0.5:  # Update at most every 0.5 seconds
                    continue
                    
                progress_info = parse_aria2c_output(line)
                if progress_info:
                    if progress_info['total'] > 0:
                        progress.update(
                            task,
                            completed=progress_info['downloaded'],
                            total=progress_info['total']
                        )
                    last_update = time.time()
                
                # Check for completion
                if 'download completed' in line.lower() or '(ok):' in line.lower():
                    download_completed = True
                    progress.update(task, completed=progress_info['total'] if progress_info else expected_size_bytes)
                    break
                
                # Check for errors
                if 'error' in line.lower() and 'console-log-level=error' not in line:
                    console.print(f"[red]❌ Error: {line.strip()}[/red]")
            
            # Wait for process to complete
            aria2c_process.wait()
            
            if aria2c_process.returncode == 0 and download_completed:
                progress.update(task, completed=expected_size_bytes if expected_size_bytes > 0 else 100)
                console.print(f"\n[green]✅ Download completed successfully![/green]")
                download_file_path = None  # Clear so it won't be deleted
                return True
            else:
                console.print(f"\n[red]❌ Download failed with exit code: {aria2c_process.returncode}[/red]")
                return False
                
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Download interrupted by user[/yellow]")
        if aria2c_process:
            aria2c_process.terminate()
            try:
                aria2c_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                aria2c_process.kill()
        raise  # Re raise to trigger cleanup
    except Exception as e:
        console.print(f"\n[red]❌ Error during download: {e}[/red]")
        if aria2c_process:
            aria2c_process.terminate()
        return False
    finally:
        aria2c_process = None

def download_from_transfer_it(transfer_url, output_dir="./downloads"):
    """Main function to download from transfer.it"""
    global download_file_path
    
    if not transfer_url or 'transfer.it/t/' not in transfer_url:
        console.print("[red]❌ Invalid transfer.it URL[/red]")
        return None
    
    # Check if aria2c installed
    if not check_aria2c():
        install_aria2c()
    
    # Check and install psutil if not available
    try:
        import psutil
    except ImportError:
        console.print("[yellow]Installing psutil for better process management...[/yellow]")
        subprocess.run([sys.executable, "-m", "pip", "install", "psutil"], capture_output=True)
    
    show_transfer_info(transfer_url)
    kill_existing_browsers(show_message=True)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        init_task = progress.add_task("🚀 Initializing browser...", total=None)
        
        with sync_playwright() as p:
            global browser_instance
            browser_instance = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled']
            )
            browser = browser_instance
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                accept_downloads=True
            )
            page = context.new_page()
            
            try:
                progress.update(init_task, description="🌐 Opening transfer.it link...")
                page.goto(transfer_url, wait_until="networkidle", timeout=30000)
                
                progress.update(init_task, description="⏳ Waiting for page to load...")
                page.wait_for_timeout(3000)
                
                file_info = extract_file_info(page)
                
                progress.update(init_task, description="🔍 Looking for download button...")
                download_button = find_download_button(page)
                
                if not download_button:
                    console.print("[red]❌ Download button not found[/red]")
                    return None
                
                progress.update(init_task, description="🎯 Getting download URL...")
                console.print("[cyan]🖱️ Clicking download button...[/cyan]")
                
                # Get download URL
                with page.expect_download(timeout=30000) as download_info:
                    download_button.click()
                    console.print("[cyan]⏳ Waiting for download to start...[/cyan]")
                
                download = download_info.value
                progress.remove_task(init_task)
                
                # Get dl URL and file info
                download_url = download.url
                file_name = file_info.get('name', download.suggested_filename)
                
                # Handle "Multiple files" case - usually a zip 
                if file_name == "Multiple files":
                    file_name = download.suggested_filename
                    if not file_name.endswith('.zip'):
                        file_name = f"{file_name}.zip"
                
                console.print(f"[green]✅ Download URL obtained![/green]")
                console.print(f"[cyan]📄 Filename: {file_name}[/cyan]")
                console.print("[cyan]🔗 Direct URL:[/cyan]")
                console.print(f"[bright_blue]{download_url}[/bright_blue]")
                
                # Calculate expected size
                expected_size_bytes = 0
                try:
                    size_text = file_info.get('size', '')
                    if 'GB' in size_text:
                        size_gb = float(size_text.replace('GB', '').strip())
                        expected_size_bytes = int(size_gb * 1024 * 1024 * 1024)
                    elif 'MB' in size_text:
                        size_mb = float(size_text.replace('MB', '').strip())
                        expected_size_bytes = int(size_mb * 1024 * 1024)
                    elif 'KB' in size_text:
                        size_kb = float(size_text.replace('KB', '').strip())
                        expected_size_bytes = int(size_kb * 1024)
                except:
                    expected_size_bytes = 0
                
                # Cancel Playwright download since using aria2c
                download.cancel()
                
                # Create out directory
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, file_name)
                download_file_path = output_path
                
                # Close browser before starting download
                browser_instance.close()
                browser_instance = None
                
                # End the progress context before starting aria2c
                progress.remove_task(init_task)
                
                # Download using aria2c (outside the Progress context)
                success = download_with_aria2c(download_url, output_path, file_name, expected_size_bytes)
                
                if success and os.path.exists(output_path):
                    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    show_download_success(output_path, file_name, file_size_mb, download_url)
                    download_file_path = None  # Clear so it won't be deleted on exit
                    return output_path
                else:
                    console.print("[red]❌ File download failed[/red]")
                    return None
                    
            except PlaywrightTimeout:
                console.print("[red]❌ Timeout while getting download URL[/red]")
                return None
            except KeyboardInterrupt:
                raise  # Let signal handler deal with it
            except Exception as e:
                console.print(f"[red]❌ Error: {e}[/red]")
                return None
            finally:
                if browser_instance:
                    browser_instance.close()
                    browser_instance = None

def show_download_success(file_path, file_name, file_size_mb, download_url):
    """Display success message with file information"""
    success_text = Text()
    success_text.append("🎉 SUCCESS! ", style="bold green")
    success_text.append("Your file has been downloaded successfully!", style="green")
    
    result_table = Table(box=box.ROUNDED)
    result_table.add_column("", style="cyan", no_wrap=True)
    result_table.add_column("", style="bright_white")
    
    result_table.add_row("📁 File Name", file_name)
    result_table.add_row("📊 File Size", f"{file_size_mb:.2f} MB")
    result_table.add_row("📂 Saved To", file_path)
    result_table.add_row("🔗 Download URL", f"{download_url[:50]}..." if len(download_url) > 50 else download_url)
    result_table.add_row("✅ Status", "[green]Download Complete[/green]")
    
    console.print("\n")
    console.print(Panel(
        result_table,
        title=success_text,
        border_style="green",
        padding=(1, 2)
    ))

def show_usage():
    """Display usage information"""
    console.print(Panel.fit(
        "[bold cyan]Transfer.it CLI Downloader (with aria2c)[/bold cyan]\n\n"
        "[yellow]Usage:[/yellow] python3 transfer-it-downloader.py <transfer_url> [output_directory]\n\n"
        "[yellow]Examples:[/yellow]\n"
        "  python3 transfer-it-downloader.py https://transfer.it/t/Zg1eX5g1WLJS\n"
        "  python3 transfer-it-downloader.py https://transfer.it/t/Zg1eX5g1WLJS ./my-downloads\n\n"
        "[yellow]Requirements:[/yellow]\n"
        "  • aria2c must be installed on your system\n"
        "  • playwright (pip install playwright)\n"
        "  • rich (pip install rich)\n"
        "  • psutil (pip install psutil) - for better process management\n\n"
        "[dim]Default output directory: ./downloads[/dim]",
        border_style="blue"
    ))

def main():
    """Main entry point"""
    console.print("\n[bold magenta]🚀 Transfer.it CLI Downloader (powered by aria2c)[/bold magenta]\n")
    
    if len(sys.argv) < 2:
        show_usage()
        sys.exit(1)
    
    transfer_url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./downloads"
    
    if not transfer_url.startswith('http'):
        console.print("[red]❌ Error: Please provide a valid transfer.it URL[/red]")
        show_usage()
        sys.exit(1)
    
    if 'transfer.it/t/' not in transfer_url:
        console.print("[red]❌ Error: This doesn't appear to be a valid transfer.it link[/red]")
        console.print("[dim]Valid links look like: https://transfer.it/t/XXXXXXXXX[/dim]")
        sys.exit(1)
    
    try:
        result = download_from_transfer_it(transfer_url, output_dir)
        
        if result:
            console.print(f"\n[green]✨ File saved to: {result}[/green]")
            sys.exit(0)
        else:
            console.print("\n[red]❌ Download failed. Please try again.[/red]")
            sys.exit(1)
    except KeyboardInterrupt:
        # Signal handler will take care of cleanup
        pass
    except Exception as e:
        console.print(f"\n[red]❌ Unexpected error: {e}[/red]")
        full_cleanup(remove_partial=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
