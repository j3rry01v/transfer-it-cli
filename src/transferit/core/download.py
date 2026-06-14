import sys
import os
import time
import signal
import atexit
import termios
import subprocess
import shutil
import argparse
from pathlib import Path
from urllib.parse import unquote
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

from ..cli_common import print_json
from ..config import load_config, prompt_yes_no, resolve_download_backend

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None
    PlaywrightTimeout = TimeoutError

try:
    from ..mega.client import Transferit
except ImportError:
    Transferit = None

try:
    from ..mega.actions.download import terminate_active_aria2c_process as terminate_mega_aria2c_process
except ImportError:
    terminate_mega_aria2c_process = None

def reexec_with_packaged_python_if_available():
    current = os.path.realpath(sys.executable)
    for candidate in ("/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
        if os.path.exists(candidate) and os.path.realpath(candidate) != current:
            os.execv(candidate, [candidate] + sys.argv)
    return False

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

def kill_aria2c_processes(quiet=False):
    """Kill all aria2c processes"""
    try:
        # Method 1: Using psutil (most reliable)
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if 'aria2c' in proc.info['name'] or (proc.info['cmdline'] and any('aria2c' in arg for arg in proc.info['cmdline'])):
                        if not quiet:
                            console.print(f"[yellow]Stopping stray aria2c process (PID: {proc.info['pid']})...[/yellow]")
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
        if not quiet:
            console.print(f"[yellow]Warning: Could not stop aria2c processes: {e}[/yellow]")

def cleanup_partial_download(quiet=False):
    """Remove partial download files"""
    global download_file_path
    if download_file_path and os.path.exists(download_file_path):
        try:
            file_size_mb = os.path.getsize(download_file_path) / (1024 * 1024)
            if not quiet:
                console.print(f"[yellow]Removing partial download ({file_size_mb:.2f} MB): {download_file_path}[/yellow]")
            os.remove(download_file_path)
            
            # Also remove aria2 control file if exists
            control_file = f"{download_file_path}.aria2"
            if os.path.exists(control_file):
                os.remove(control_file)
                
        except Exception as e:
            if not quiet:
                console.print(f"[yellow]Warning: Could not remove partial file: {e}[/yellow]")

def check_aria2c():
    """Check if aria2c is installed"""
    return shutil.which("aria2c") is not None

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
                TextColumn("Closing browser helper processes..."),
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

def full_cleanup(remove_partial=True, quiet=False):
    """Complete cleanup of all resources"""
    global browser_instance, aria2c_process
    
    if not quiet:
        console.print("\n[yellow]Shutting down active download cleanly...[/yellow]")
    
    # Restore terminal
    if not quiet and original_terminal_settings:
        console.print("[dim]Restoring terminal input settings...[/dim]")
    restore_terminal_input()
    
    # Kill aria2c process if running
    if aria2c_process:
        try:
            if not quiet:
                console.print("[yellow]Stopping aria2c download process...[/yellow]")
            aria2c_process.terminate()
            try:
                aria2c_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                aria2c_process.kill()
                aria2c_process.wait()
        except:
            pass
        aria2c_process = None

    if terminate_mega_aria2c_process is not None:
        try:
            stopped = terminate_mega_aria2c_process()
            if stopped and not quiet:
                console.print("[yellow]Stopped MEGA aria2c download process.[/yellow]")
        except Exception as exc:
            if not quiet:
                console.print(f"[yellow]Warning: Could not stop MEGA aria2c process: {exc}[/yellow]")
     
    # Kill any remaining aria2c processes
    kill_aria2c_processes(quiet=quiet)
    
    # Clean up partial downloads if requested
    if remove_partial:
        cleanup_partial_download(quiet=quiet)
    
    # Close browser
    if browser_instance:
        try:
            if not quiet:
                console.print("[yellow]Closing browser session...[/yellow]")
            browser_instance.close()
        except:
            pass
        browser_instance = None
    
    # Kill browser processes
    if not quiet:
        console.print("[dim]Checking for leftover browser helper processes...[/dim]")
    kill_existing_browsers(show_message=False)

def signal_handler(signum, frame):
    """Handle various signals gracefully"""
    signal_name = signal.Signals(signum).name
    if signum == signal.SIGINT:
        reason = "cancel request (Ctrl+C)"
    elif signum == signal.SIGTSTP:
        reason = "suspend request (Ctrl+Z)"
    elif signum == signal.SIGTERM:
        reason = "termination request"
    elif signum == signal.SIGHUP:
        reason = "terminal closed"
    else:
        reason = signal_name
    console.print(f"\n[yellow]Download interrupted by {reason}.[/yellow]")
    full_cleanup(remove_partial=True)
    console.print("[red]Download stopped.[/red]")
    sys.exit(130 if signum == signal.SIGINT else 1)

# Register signal handlers for various interruption scenarios
signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # Termination
signal.signal(signal.SIGTSTP, signal_handler)  # Ctrl+Z
signal.signal(signal.SIGHUP, signal_handler)   # Terminal closed

# Cleanup on normal exit
atexit.register(lambda: full_cleanup(remove_partial=False, quiet=True))

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
    
    # Fallback to role-based selector
    try:
        btn = page.get_by_role("button", name="Download all", exact=True)
        if btn.is_visible():
            console.print("[green]✅ Download button found[/green]")
            return btn
    except:
        pass
    
    return None

def parse_aria2c_output(line):
    """Parse aria2c output to extract progress information"""
    try:
        # Parse lines like: [#2c1434 68MiB/10GiB(0%) CN:16 DL:9.7MiB ETA:17m56s]
        if '[#' in line and 'CN:' in line:
            parts = line.strip().split()
            for part in parts:
                if '/' in part and ('MiB' in part or 'GiB' in part or 'KiB' in part):
                    # Find downloaded/total
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
                    
                    # Find speed
                    speed = 0
                    for p in parts:
                        if p.startswith('DL:'):
                            speed_str = p.replace('DL:', '')
                            speed = to_bytes(speed_str)
                    
                    # Find ETA
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
    
    console.print("\n[cyan]Browser backend is using aria2c as the download accelerator.[/cyan]")
    console.print("[dim]aria2c runs as a separate process; press Ctrl+C to stop it and remove the partial file.[/dim]")
    console.print(f"[cyan]Starting aria2c download...[/cyan]")
    console.print(f"[dim]Output: {output_path}[/dim]")
    console.print(f"[dim]Press Ctrl+C to cancel download and cleanup[/dim]\n")
    
    # aria2c command with all optimizations
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
        # Start aria2c process
        aria2c_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Create progress bar
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
            
            if aria2c_process.returncode == 0 or download_completed:
                if expected_size_bytes > 0:
                    progress.update(task, completed=expected_size_bytes)
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
        raise  # Re-raise to trigger cleanup
    except Exception as e:
        console.print(f"\n[red]❌ Error during download: {e}[/red]")
        if aria2c_process:
            aria2c_process.terminate()
        return False
    finally:
        aria2c_process = None

def humanise_bytes(num_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes or 0)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

def download_from_transfer_it_mega(transfer_url, output_dir, password=None, force=False, quiet=False, simple_mode=False, aria2c_enabled=True):
    if Transferit is None:
        reexec_with_packaged_python_if_available()
        raise RuntimeError("MEGA backend dependencies are missing. Run: python3 -m pip install -r requirements.txt")

    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if quiet:
        with Transferit() as tx:
            return tx.download(transfer_url, output_root, password=password, force=force, aria2c=aria2c_enabled)

    if simple_mode:
        print(f"Downloading with MEGA backend: {transfer_url}")
        print(f"Output directory: {output_root}")
    else:
        console.print(f"[cyan]⚡ Using MEGA backend[/cyan]")
        console.print(f"[dim]Output directory: {output_root}[/dim]")
    if aria2c_enabled:
        if shutil.which("aria2c"):
            if simple_mode:
                print("aria2c enabled for encrypted blob download + local decryption")
            else:
                console.print("[cyan]aria2c enabled for encrypted blob download + local decryption[/cyan]")
        else:
            msg = (
                "aria2c not found. Install for faster downloads:\n"
                "  macOS:    brew install aria2\n"
                "  Ubuntu:   sudo apt-get install aria2\n"
                "  Fedora:   sudo dnf install aria2\n"
                "Continuing with built-in streaming decryption."
            )
            print(msg) if simple_mode else console.print(f"[yellow]{msg}[/yellow]")
            aria2c_enabled = False

    if simple_mode:
        state = {"single": False, "current": None, "file_started": time.monotonic()}

        def on_start(files, total):
            state["single"] = len(files) == 1
            label = files[0].name if state["single"] and files else f"{len(files)} file(s)"
            print(f"Downloading {label} ({humanise_bytes(total)})")

        def on_file_start(node, out_path):
            state["current"] = node
            state["file_started"] = time.monotonic()
            if not state["single"]:
                print(f"Starting: {node.name or node.handle}")

        def on_file_progress(node, done, total):
            elapsed = max(time.monotonic() - state["file_started"], 0.001)
            speed = done / elapsed
            percent = (done / total * 100) if total else 0
            print(
                f"\rProgress: {humanise_bytes(done)} / {humanise_bytes(total)} "
                f"({percent:.1f}%) | Speed: {humanise_bytes(speed)}/s",
                end="",
                flush=True,
            )

        def on_file_done(node, out_path):
            elapsed = max(time.monotonic() - state["file_started"], 0.001)
            speed = (node.size or 0) / elapsed
            print(
                f"\rProgress: {humanise_bytes(node.size)} / {humanise_bytes(node.size)} "
                f"(100.0%) | Speed: {humanise_bytes(speed)}/s"
            )
            print(f"Saved: {out_path}")

        def on_skip(node, out_path):
            print(f"Skipped existing file: {out_path} (use --force to overwrite)")

        with Transferit() as tx:
            return tx.download(
                transfer_url,
                output_root,
                password=password,
                force=force,
                aria2c=aria2c_enabled,
                on_start=on_start,
                on_file_start=on_file_start,
                on_file_progress=on_file_progress,
                on_file_done=on_file_done,
                on_skip=on_skip,
            )

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
        refresh_per_second=2,
    ) as progress:
        overall = progress.add_task("📥 Preparing download...", total=1)
        state = {"single": False, "current": None}

        def on_start(files, total):
            state["single"] = len(files) == 1
            label = files[0].name if state["single"] and files else f"{len(files)} file(s)"
            progress.update(overall, description=f"📥 Downloading {label}", total=total or 1)

        def on_file_start(node, out_path):
            if state["single"]:
                return
            state["current"] = progress.add_task(node.name or node.handle, total=node.size or 1)

        def on_file_progress(node, done, total):
            if state["single"]:
                progress.update(overall, completed=done, total=total or 1)
            elif state["current"] is not None:
                progress.update(state["current"], completed=done, total=total or 1)

        def on_file_done(node, out_path):
            if state["single"]:
                progress.update(overall, completed=node.size or 1)
                return
            current = state.get("current")
            if current is not None:
                progress.update(current, completed=node.size or 1)
                progress.remove_task(current)
                state["current"] = None
            progress.advance(overall, node.size or 0)

        def on_skip(node, out_path):
            progress.console.print(f"[yellow]skip[/yellow] {out_path} (use --force to overwrite)")
            if state["single"]:
                progress.update(overall, completed=node.size or 0)
            else:
                progress.advance(overall, node.size or 0)

        with Transferit() as tx:
            result = tx.download(
                transfer_url,
                output_root,
                password=password,
                force=force,
                aria2c=aria2c_enabled,
                on_start=on_start,
                on_file_start=on_file_start,
                on_file_progress=on_file_progress,
                on_file_done=on_file_done,
                on_skip=on_skip,
            )

    return result

def show_mega_download_summary(result, elapsed):
    written = [p for p in result.paths if p not in result.skipped]
    rate = (result.total_bytes / elapsed / 1e6) if elapsed else 0
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(overflow="fold")
    table.add_row("source", result.xh)
    table.add_row("destination", result.output_dir)
    table.add_row("files", f"{len(written)} written" + (f", [yellow]{len(result.skipped)} skipped[/yellow]" if result.skipped else ""))
    table.add_row("size", f"{humanise_bytes(result.total_bytes)} [dim]({result.total_bytes:,} bytes)[/dim]")
    table.add_row("elapsed", f"{elapsed:.1f}s [dim]({rate:.2f} MB/s)[/dim]")
    console.print(Panel(table, title="transfer.it", title_align="right", border_style="green", box=box.ROUNDED))

def show_mega_download_summary_simple(result, elapsed):
    written = [p for p in result.paths if p not in result.skipped]
    rate = (result.total_bytes / elapsed / 1e6) if elapsed else 0
    print()
    print("=" * 50)
    print("SUCCESS!")
    print("=" * 50)
    print("Your download completed successfully.")
    print(f"Files written: {len(written)}")
    if result.skipped:
        print(f"Files skipped: {len(result.skipped)}")
    print(f"Size: {humanise_bytes(result.total_bytes)}")
    print(f"Elapsed: {elapsed:.1f}s ({rate:.2f} MB/s)")
    print(f"Saved to: {result.output_dir}")

def download_mega_with_retries(transfer_url, output_dir, attempts, password=None, force=False, quiet=False, simple_mode=False, aria2c_enabled=True):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if simple_mode and not quiet:
                print(f"Download attempt {attempt}/{attempts} using MEGA backend")
            elif not quiet:
                console.print(f"[cyan]Download attempt {attempt}/{attempts} using MEGA backend[/cyan]")
            return download_from_transfer_it_mega(transfer_url, output_dir, password=password, force=force, quiet=quiet, simple_mode=simple_mode, aria2c_enabled=aria2c_enabled)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_error = exc
            if simple_mode and not quiet:
                print(f"Download attempt {attempt} failed on MEGA backend: {exc}")
            elif not quiet:
                console.print(f"[yellow]⚠️ Download attempt {attempt} failed on MEGA backend: {exc}[/yellow]")
            if attempt < attempts:
                time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"MEGA download failed after {attempts} attempt(s): {last_error}")

def get_download_info(url: str):
    """
    Automates capturing the download URL, original filename, and the descriptive title.
    Returns: A tuple (download_url, original_filename, descriptive_title, file_info), or None on failure.
    """
    global browser_instance
    if sync_playwright is None:
        reexec_with_packaged_python_if_available()
        console.print("[red]❌ Playwright is not installed. Run: python3 -m pip install -r requirements.txt[/red]")
        return None
    
    with sync_playwright() as p:
        browser_instance = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser_instance.new_context(
            locale='en-US',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9'
            }
        )
        page = context.new_page()
        
        try:
            console.print(f"[cyan]🌐 Using browser session[/cyan]")
            console.print(f"[cyan]Navigating to {url}...[/cyan]")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            
            # Check for cookie banner
            try:
                console.print("[dim]Checking for cookie banner...[/dim]")
                accept_button = page.get_by_role("button", name="Accept all")
                accept_button.click(timeout=10000)
                console.print("[dim]Cookie banner accepted.[/dim]")
            except PlaywrightTimeout:
                console.print("[dim]Cookie banner not found, continuing...[/dim]")
            
            # Wait for page to stabilize
            page.wait_for_timeout(3000)
            
            # Extract file information
            file_info = extract_file_info(page)
            
            # Find and click download button
            download_button = find_download_button(page)
            
            if not download_button:
                console.print("[red]❌ Download button not found[/red]")
                return None
            
            console.print("[cyan]🖱️ Clicking download button...[/cyan]")
            console.print("[cyan]⏳ Waiting for download to start...[/cyan]")
            
            with page.expect_download(timeout=30000) as download_info:
                download_button.click()
            
            download = download_info.value
            download_url = download.url
            original_filename = unquote(download_url.split('/')[-1].split('?')[0])
            
            console.print(f"[green]✅ Download URL obtained![/green]")
            
            # Cancel the download as we'll use aria2c
            download.cancel()
            
            # Get descriptive title
            descriptive_title = file_info.get('name', '')
            
            return download_url, original_filename, descriptive_title, file_info
            
        except PlaywrightTimeout as e:
            console.print(f"[red]❌ Timeout error: {e}[/red]")
            return None
        except Exception as e:
            console.print(f"[red]❌ An error occurred: {e}[/red]")
            return None
        finally:
            console.print("[dim]Closing browser.[/dim]")
            if browser_instance:
                browser_instance.close()
                browser_instance = None

def download_with_browser_native(transfer_url, output_dir):
    """Download through Playwright without aria2c."""
    global browser_instance, download_file_path
    if sync_playwright is None:
        reexec_with_packaged_python_if_available()
        console.print("[red]❌ Playwright is not installed. Run: python3 -m pip install -r requirements.txt[/red]")
        return None

    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser_instance = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser_instance.new_context(
            accept_downloads=True,
            locale='en-US',
            extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'}
        )
        page = context.new_page()
        try:
            console.print(f"[cyan]🌐 Using browser download session[/cyan]")
            console.print(f"[cyan]Navigating to {transfer_url}...[/cyan]")
            page.goto(transfer_url, timeout=60000, wait_until="domcontentloaded")
            try:
                page.get_by_role("button", name="Accept all").click(timeout=10000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(3000)
            file_info = extract_file_info(page)
            download_button = find_download_button(page)
            if not download_button:
                console.print("[red]❌ Download button not found[/red]")
                return None

            console.print("[cyan]📥 Starting browser download...[/cyan]")
            with page.expect_download(timeout=30000) as download_info:
                download_button.click()
            download = download_info.value

            suggested = download.suggested_filename or unquote(download.url.split('/')[-1].split('?')[0]) or "transfer-it-download"
            descriptive_title = file_info.get('name', '')
            if descriptive_title and descriptive_title != "Multiple files":
                safe_title = descriptive_title.replace('/', '-').replace('\\', '-').replace(':', ' -')
                file_name = f"{safe_title}.zip" if suggested.endswith('.zip') else safe_title
            else:
                file_name = suggested
                if descriptive_title == "Multiple files" and not file_name.endswith('.zip'):
                    file_name = f"{file_name}.zip"

            output_path = output_root / file_name
            download_file_path = str(output_path)
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task(f"Saving {file_name}...", total=None)
                download.save_as(str(output_path))
                progress.update(task, description="✅ Browser download complete")

            download_file_path = None
            file_size_mb = output_path.stat().st_size / (1024 * 1024) if output_path.exists() else 0
            show_download_success(str(output_path), file_name, file_size_mb, download.url)
            return str(output_path)
        except Exception as e:
            console.print(f"[red]❌ Browser download failed: {e}[/red]")
            return None
        finally:
            if browser_instance:
                browser_instance.close()
                browser_instance = None

def download_from_transfer_it(transfer_url, output_dir="./downloads", aria2c_enabled=True):
    """Main function to download from transfer.it"""
    global download_file_path
    
    if not transfer_url or 'transfer.it/t/' not in transfer_url:
        console.print("[red]❌ Invalid transfer.it URL[/red]")
        return None
    
    if not aria2c_enabled:
        console.print("[yellow]aria2c disabled; using browser download session.[/yellow]")
        show_transfer_info(transfer_url)
        kill_existing_browsers(show_message=True)
        return download_with_browser_native(transfer_url, output_dir)

    # Check if aria2c is installed
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
    
    # Get download info
    download_info = get_download_info(transfer_url)
    
    if not download_info:
        console.print("[red]❌ Could not retrieve download information from the URL.[/red]")
        return None
    
    download_url, original_filename, descriptive_title, file_info = download_info
    
    # Determine final filename
    if descriptive_title and descriptive_title != "Multiple files":
        sanitized_title = descriptive_title.replace('/', '-').replace('\\', '-').replace(':', ' -')
        if original_filename.endswith('.zip'):
            file_name = f"{sanitized_title}.zip"
        else:
            file_name = sanitized_title
    else:
        file_name = original_filename
        # Handle "Multiple files" case - usually a zip
        if descriptive_title == "Multiple files" and not file_name.endswith('.zip'):
            file_name = f"{file_name}.zip"
    
    console.print(f"[cyan]📄 Final Filename: {file_name}[/cyan]")
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
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, file_name)
    download_file_path = output_path
    
    # Download with aria2c
    success = download_with_aria2c(download_url, output_path, file_name, expected_size_bytes)
    
    if success and os.path.exists(output_path):
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        show_download_success(output_path, file_name, file_size_mb, download_url)
        download_file_path = None  # Clear so it won't be deleted on exit
        return output_path
    else:
        console.print("[red]❌ File download failed[/red]")
        return None

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
        "[bold cyan]Transfer.it CLI Downloader[/bold cyan]\n\n"
        "[yellow]Usage:[/yellow] transferit download [options] <transfer_url> [output_directory]\n\n"
        "[yellow]Options:[/yellow]\n"
        "  --backend mega|browser   Download backend (default: mega)\n"
        "  --simple                 Use simple text output\n"
        "  --aria2c / --no-aria2c   Enable/disable aria2c for all backends\n"
        "  -o, --output-dir PATH    Destination folder\n"
        "  -p, --password PASSWORD  Password for protected transfers\n"
        "  -f, --force              Overwrite existing files in MEGA mode\n"
        "  --json                   Print machine-readable JSON in MEGA mode\n"
        "  --no-fallback            Do not ask to retry with browser mode\n\n"
        "[yellow]Examples:[/yellow]\n"
        "  transferit download https://transfer.it/t/abc123def456\n"
        "  transferit download --backend browser --aria2c https://transfer.it/t/abc123def456 ./my-downloads\n\n"
        "[yellow]Requirements:[/yellow]\n"
        "  • Python 3.11+\n"
        "  • aria2c is optional and only used by browser backend\n\n"
        "[dim]Default output directory comes from config, initially ~/Downloads[/dim]",
        border_style="blue"
    ))

def parse_args(argv):
    parser = argparse.ArgumentParser(prog="transferit download", description="Download transfer.it links")
    parser.add_argument("transfer_url", nargs="?", help="transfer.it URL or 12-character handle")
    parser.add_argument("positional_output_dir", nargs="?", help="Output directory (kept for old CLI compatibility)")
    parser.add_argument("--simple", action="store_true", help="Use simple text output")
    parser.add_argument("--backend", choices=["mega", "browser"], help="Download backend (default from config: mega)")
    parser.add_argument("-o", "--output-dir", help="Output directory")
    parser.add_argument("-p", "--password", help="Password for protected transfers")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite existing files in MEGA mode")
    parser.add_argument("--no-fallback", action="store_true", help="Do not ask to retry with browser mode if MEGA fails")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON in MEGA mode")
    aria = parser.add_mutually_exclusive_group()
    aria.add_argument("--aria2c", dest="aria2c", action="store_true", help="Use aria2c for faster downloads (MEGA: encrypted blob + local decrypt; browser: accelerator)")
    aria.add_argument("--no-aria2c", dest="aria2c", action="store_false", help="Disable aria2c")
    parser.set_defaults(aria2c=None)
    return parser.parse_args(argv)

def main(argv=None):
    """Main entry point"""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    simple_mode = args.simple and not args.json
    backend = resolve_download_backend(args.backend, config)
    output_dir = args.output_dir or args.positional_output_dir or config.get("download_dir")
    retry_count = int(config.get("retry_count", 3))
    aria2c_enabled = config.get("aria2c_enabled", True) if args.aria2c is None else args.aria2c

    if (backend == "mega" and Transferit is None) or (backend == "browser" and sync_playwright is None):
        reexec_with_packaged_python_if_available()

    if args.json and backend != "mega":
        console.print("[red]--json is only supported by the MEGA backend[/red]")
        sys.exit(2)

    if simple_mode:
        print(f"transferit download (backend={backend})")
    elif not args.json:
        console.print(f"\n[bold magenta]transferit download[/bold magenta] [dim]backend={backend}[/dim]\n")

    if not args.transfer_url:
        if simple_mode:
            print("Usage: transferit download [--backend mega|browser] [--simple] <transfer_url> [output_directory]")
            print("Options:")
            print("  --backend    Download backend: mega (default) or browser")
            print("  --simple     Use simple text output")
            print("  --aria2c     Use aria2c for faster downloads")
            print("  --no-aria2c  Disable aria2c")
        else:
            show_usage()
        sys.exit(1)

    transfer_url = args.transfer_url

    if backend == "browser":
        if not transfer_url.startswith('http'):
            console.print("[red]❌ Browser backend needs a full transfer.it URL, not a bare handle[/red]")
            show_usage()
            sys.exit(1)
        if 'transfer.it/t/' not in transfer_url:
            console.print("[red]❌ Error: This doesn't appear to be a valid transfer.it link[/red]")
            console.print("[dim]Valid links look like: https://transfer.it/t/XXXXXXXXX[/dim]")
            sys.exit(1)

    try:
        started = time.monotonic()
        if backend == "mega":
            try:
                result = download_mega_with_retries(
                    transfer_url,
                    output_dir,
                    retry_count,
                    password=args.password,
                    force=args.force,
                    quiet=args.json,
                    simple_mode=simple_mode,
                    aria2c_enabled=aria2c_enabled,
                )
            except Exception as exc:
                if args.json:
                    raise
                print(f"MEGA backend failed: {exc}") if simple_mode else console.print(f"[red]❌ MEGA backend failed: {exc}[/red]")
                should_fallback = False
                if not args.no_fallback and config.get("prompt_browser_fallback", True):
                    should_fallback = prompt_yes_no("MEGA backend failed. Retry using browser mode?", default=False)
                if not should_fallback:
                    sys.exit(1)
                result = download_from_transfer_it(transfer_url, output_dir, aria2c_enabled=aria2c_enabled)
        else:
            result = download_from_transfer_it(transfer_url, output_dir, aria2c_enabled=aria2c_enabled)
        
        if args.json and result:
            print_json(result.to_json_dict())
            sys.exit(0)
        if backend == "mega" and result:
            if simple_mode:
                show_mega_download_summary_simple(result, time.monotonic() - started)
            else:
                show_mega_download_summary(result, time.monotonic() - started)
                console.print(f"\n[green]✨ Files saved to: {result.output_dir}[/green]")
            sys.exit(0)
        if result:
            print(f"File saved to: {result}") if simple_mode else console.print(f"\n[green]✨ File saved to: {result}[/green]")
            sys.exit(0)
        else:
            print("Download failed. Please try again.") if simple_mode else console.print("\n[red]❌ Download failed. Please try again.[/red]")
            sys.exit(1)
    except KeyboardInterrupt:
        # Signal handler will take care of cleanup
        pass
    except Exception as e:
        print(f"Unexpected error: {e}") if simple_mode else console.print(f"\n[red]❌ Unexpected error: {e}[/red]")
        import traceback
        traceback.print_exc()
        full_cleanup(remove_partial=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
