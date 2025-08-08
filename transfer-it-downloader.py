#!/usr/bin/env python3
"""
Transfer.it CLI Downloader
Download files from transfer.it links with progress tracking and browser automation
"""

import sys
import os
import time
import signal
import atexit
import termios
import tty
import subprocess
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

console = Console()
browser_instance = None
original_terminal_settings = None

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

def kill_existing_browsers(show_message=False):
    """
    Terminate existing browser processes to prevent resource leaks.
    Playwright browsers can sometimes persist after script interruption (Ctrl+C),
    leading to memory accumulation and system resource exhaustion. This function
    ensures a clean environment by terminating any orphaned browser processes
    before launching new instances.
    """
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
            # Silent cleanup
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

def cleanup_browser():
    """Clean up browser instance on exit"""
    global browser_instance
    restore_terminal_input()
    if browser_instance:
        try:
            browser_instance.close()
        except:
            pass
    kill_existing_browsers(show_message=False)

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    console.print("\n[yellow]⚠️ Download cancelled by user[/yellow]")
    cleanup_browser()
    sys.exit(0)

# Register cleanup handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(cleanup_browser)

def show_transfer_info(transfer_url):
    """Display transfer link information"""
    table = Table(box=box.ROUNDED)
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")
    
    table.add_row("Transfer Link", transfer_url)
    table.add_row("Status", "🔍 Analyzing...")
    
    console.print(Panel(table, title="📁 Transfer Information", border_style="blue"))

def download_from_transfer_it(transfer_url, output_dir="./downloads"):
    """Main function to download from transfer.it"""
    
    if not transfer_url or 'transfer.it/t/' not in transfer_url:
        console.print("[red]❌ Invalid transfer.it URL[/red]")
        return None
    
    show_transfer_info(transfer_url)
    
    # Clean up existing browser processes
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
                headless=True,  # Can be True or False
                args=['--disable-blink-features=AutomationControlled']
            )
            browser = browser_instance
            
            # Create context with download handling enabled
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                accept_downloads=True  # Important: Enable download handling
            )
            
            page = context.new_page()
            
            try:
                progress.update(init_task, description="🌐 Opening transfer.it link...")
                
                # Navigate to the transfer page
                page.goto(transfer_url, wait_until="networkidle", timeout=30000)
                progress.update(init_task, description="⏳ Waiting for page to load...")
                
                # Wait for the page to fully load
                page.wait_for_timeout(3000)
                
                # Variables to store file info
                file_name = None
                file_size = None
                
                # Try to extract file information from the page
                try:
                    # Look for file name in the ready-to-download section
                    file_name_elem = page.locator('.ready-to-download-box .link-info .title').first
                    if file_name_elem.is_visible():
                        file_name = file_name_elem.text_content().strip()
                        console.print(f"[cyan]📄 File: {file_name}[/cyan]")
                    
                    # Look for file size
                    file_size_elem = page.locator('.ready-to-download-box .it-grid-info .size').first
                    if file_size_elem.is_visible():
                        file_size_text = file_size_elem.text_content().strip()
                        console.print(f"[cyan]📊 Size: {file_size_text}[/cyan]")
                    
                    # Look for file count
                    file_count_elem = page.locator('.ready-to-download-box .it-grid-info .num').first
                    if file_count_elem.is_visible():
                        file_count = file_count_elem.text_content().strip()
                        console.print(f"[cyan]📁 Files: {file_count}[/cyan]")
                except:
                    pass
                
                progress.update(init_task, description="🔍 Looking for download button...")
                
                # Find the download button
                download_button = None
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
                            download_button = btn
                            console.print("[green]✅ Download button found[/green]")
                            break
                    except:
                        continue
                
                if not download_button:
                    console.print("[red]❌ Download button not found[/red]")
                    return None
                
                progress.update(init_task, description="🎯 Initiating download...")
                console.print("[cyan]🖱️ Clicking download button...[/cyan]")
                
                # Start waiting for the download before clicking the button
                with page.expect_download(timeout=30000) as download_info:
                    # Click the download button
                    download_button.click()
                    console.print("[cyan]⏳ Waiting for download to start...[/cyan]")
                
                # Get the download object
                download = download_info.value
                
                progress.remove_task(init_task)
                
                # Get download information
                download_url = download.url
                suggested_filename = download.suggested_filename
                
                if not file_name:
                    file_name = suggested_filename
                
                console.print(f"[green]✅ Download started![/green]")
                console.print(f"[cyan]📄 Filename: {file_name}[/cyan]")
                console.print(f"[cyan]🔗 Download URL: {download_url[:80]}...[/cyan]" if len(download_url) > 80 else f"[cyan]🔗 Download URL: {download_url}[/cyan]")
                
                # Create output directory if it doesn't exist
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, file_name)
                
                # Show download progress
                setup_terminal_for_progress()
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TextColumn("•"),
                    TimeRemainingColumn(),
                    console=console
                ) as download_progress:
                    task = download_progress.add_task(f"📥 Downloading {file_name}...", total=100)
                    
                    # Monitor download progress
                    start_time = time.time()
                    timeout = 300  # 5 minutes timeout
                    
                    while time.time() - start_time < timeout:
                        # Check if download is finished
                        if download.path():
                            # Download completed
                            download_progress.update(task, completed=100)
                            break
                        
                        # Update progress (estimate based on time)
                        elapsed = time.time() - start_time
                        estimated_progress = min((elapsed / 30) * 100, 99)  # Estimate based on typical download time
                        download_progress.update(task, completed=estimated_progress)
                        
                        time.sleep(0.5)
                    
                    if not download.path():
                        console.print("[red]❌ Download timeout[/red]")
                        return None
                
                restore_terminal_input()
                
                # Save the downloaded file to the specified location
                console.print(f"[cyan]💾 Saving file to {output_path}...[/cyan]")
                download.save_as(output_path)
                
                # Verify file exists
                if os.path.exists(output_path):
                    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    show_download_success(output_path, file_name, file_size_mb, download_url)
                    return output_path
                else:
                    console.print("[red]❌ File save failed[/red]")
                    return None
                    
            except PlaywrightTimeout:
                console.print("[red]❌ Download timeout - the download took too long[/red]")
                return None
            except Exception as e:
                console.print(f"[red]❌ Error: {e}[/red]")
                try:
                    page.screenshot(path="transfer_it_error.png")
                    console.print("[dim]Debug screenshot saved as transfer_it_error.png[/dim]")
                except:
                    pass
                return None
            finally:
                cleanup_browser()
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
        "[bold cyan]Transfer.it CLI Downloader[/bold cyan]\n\n"
        "[yellow]Usage:[/yellow] python3 transfer-it-downloader.py <transfer_url> [output_directory]\n\n"
        "[yellow]Examples:[/yellow]\n"
        "  python3 transfer-it-downloader.py https://transfer.it/t/Zg1eX5g1WLJS\n"
        "  python3 transfer-it-downloader.py https://transfer.it/t/Zg1eX5g1WLJS ./my-downloads\n\n"
        "[dim]Default output directory: ./downloads[/dim]",
        border_style="blue"
    ))

def main():
    """Main entry point"""
    console.print("\n[bold magenta]🚀 Transfer.it CLI Downloader[/bold magenta]\n")
    
    if len(sys.argv) < 2:
        show_usage()
        sys.exit(1)
    
    transfer_url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./downloads"
    
    # Validate URL format
    if not transfer_url.startswith('http'):
        console.print("[red]❌ Error: Please provide a valid transfer.it URL[/red]")
        show_usage()
        sys.exit(1)
    
    if 'transfer.it/t/' not in transfer_url:
        console.print("[red]❌ Error: This doesn't appear to be a valid transfer.it link[/red]")
        console.print("[dim]Valid links look like: https://transfer.it/t/XXXXXXXXX[/dim]")
        sys.exit(1)
    
    # Perform download
    result = download_from_transfer_it(transfer_url, output_dir)
    
    if result:
        console.print(f"\n[green]✨ File saved to: {result}[/green]")
        sys.exit(0)
    else:
        console.print("\n[red]❌ Download failed. Please try again.[/red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
