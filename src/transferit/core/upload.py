
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import time
import signal
import atexit
import termios
import tty
import subprocess
import argparse
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

from ..cli_common import humanise_duration, parse_expiry, parse_schedule, print_json
from ..config import load_config, prompt_yes_no, resolve_upload_backend

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from ..mega.client import Transferit
except ImportError:
    Transferit = None

def reexec_with_packaged_python_if_available():
    current = os.path.realpath(sys.executable)
    for candidate in ("/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
        if os.path.exists(candidate) and os.path.realpath(candidate) != current:
            os.execv(candidate, [candidate] + sys.argv)
    return False

# Detect if running in a server environment
IS_SERVER = not sys.stdout.isatty() or os.getenv('SSH_CONNECTION') or os.getenv('SSH_CLIENT')

if IS_SERVER:
    # Rich console optimized for server environments
    console = Console(force_terminal=True, width=80)
else:
    console = Console()
browser_instance = None
original_terminal_settings = None
display_conflict_detected = False
MEGA_UPLOAD_STALL_TIMEOUT = int(os.environ.get("TRANSFER_IT_MEGA_UPLOAD_STALL_TIMEOUT", "180"))

class MegaUploadStalled(TimeoutError):
    pass

def arm_mega_upload_stall_alarm():
    if hasattr(signal, "SIGALRM"):
        signal.alarm(MEGA_UPLOAD_STALL_TIMEOUT)

def clear_mega_upload_stall_alarm(previous_handler=None):
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
        if previous_handler is not None:
            signal.signal(signal.SIGALRM, previous_handler)

def mega_upload_stall_handler(signum, frame):
    raise MegaUploadStalled(f"MEGA upload stalled for {MEGA_UPLOAD_STALL_TIMEOUT} seconds without progress")

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
            # TODO: WINDOWS OS CLEANUP
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
    console.print("\n[yellow]⚠️ Upload cancelled by user[/yellow]")
    cleanup_browser()
    sys.exit(0)

# Register cleanup handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(cleanup_browser)

def show_file_info_simple(file_path):
    """Simple file info display without Rich UI"""
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    
    print("📁 File Information")
    print("=" * 50)
    print(f"File Name: {file_name}")
    print(f"File Size: {file_size_mb:.2f} MB")
    print(f"File Path: {file_path}")
    print("=" * 50)
    
    return file_name, file_size_mb

def show_file_info(file_path):
    """Rich file info display with fallback to simple mode"""
    global display_conflict_detected
    
    if display_conflict_detected:
        # Already detected conflict, use simple mode directly
        return show_file_info_simple(file_path)
    
    try:
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        table = Table(box=box.ROUNDED)
        table.add_column("Property", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")
        
        table.add_row("File Name", file_name)
        table.add_row("File Size", f"{file_size_mb:.2f} MB")
        table.add_row("File Path", file_path)
        
        console.print(Panel(table, title="📁 File Information", border_style="blue"))
        return file_name, file_size_mb
    except Exception as e:
        error_msg = str(e).lower()
        if "only one live display may be active at once" in error_msg or "display" in error_msg:
            display_conflict_detected = True
            print("\n" + "="*60)
            print("⚠️  DISPLAY CONFLICT DETECTED")
            print("="*60)
            print("The Rich UI is conflicting with your terminal environment.")
            print("This can happen when:")
            print("• Multiple terminal sessions are active")
            print("• Running inside screen/tmux with complex display setup")
            print("• Terminal doesn't fully support Rich's display management")
            print()
            print("🔄 Automatically switching to simple mode...")
            print("="*60)
            print()
            # Fall back to simple display
            return show_file_info_simple(file_path)
        else:
            # Re-raise other errors
            raise e

def upload_to_transfer_it_simple(file_path):
    """Simple upload function without Rich UI - for server environments"""
    if sync_playwright is None:
        reexec_with_packaged_python_if_available()
        print("Error: Playwright is not installed. Run: python3 -m pip install -r requirements.txt")
        return None

    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return None
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    
    print(f"Uploading {file_name} ({file_size_mb:.2f} MB)...")
    print("Using browser upload session...")
    
    with sync_playwright() as p:
        # Launch browser with server friendly settings
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-gpu',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-extensions',
                '--disable-plugins'
            ]
        )
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='en-US',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9'
            }
        )
        page = context.new_page()
        
        try:
            print("Opening transfer.it...")
            page.goto("https://transfer.it", wait_until="domcontentloaded")
            
            print("Waiting for page to load...")
            page.wait_for_timeout(5000)
            
            print("Selecting file...")
            file_input = page.locator('input[type="file"][name="select-file"]').first
            file_input.set_input_files(file_path)
            
            page.wait_for_timeout(2000)
            
            print("Clicking Transfer button...")
            transfer_button = page.locator('button.js-get-link-button:has-text("Transfer")')
            
            page.wait_for_function(
                """() => {
                    const btn = document.querySelector('button.js-get-link-button');
                    return btn && !btn.classList.contains('disabled');
                }""",
                timeout=10000
            )
            
            transfer_button.click()
            
            print("Starting upload...")
            
            page.wait_for_selector('.js-transfer-section:not(.hidden)', timeout=5000)
            
            # Monitor progress until completion
            last_progress = ""
            start_time = time.time()
            
            while True:
                try:
                    completed_section = page.locator('section.transferring-box.completed')
                    if completed_section.count() > 0:
                        print("\nUpload completed!")
                        break

                    if page.locator('h4:has-text("Completed!")').is_visible():
                        print("\nUpload completed!")
                        break
                    
                    # Get progress information
                    uploaded_elem = page.locator('.status-info.transfer span.uploaded').first
                    size_elem = page.locator('.status-info.transfer span.size').first
                    speed_elem = page.locator('.status-info.transfer span.speed').first
                    time_elem = page.locator('.status-info.time span.left').first
                    
                    if uploaded_elem.is_visible():
                        uploaded = uploaded_elem.text_content()
                        total_size = size_elem.text_content()
                        speed = speed_elem.text_content()
                        time_left = time_elem.text_content()
                        
                        # Create progress string
                        progress_str = f"Progress: {uploaded} / {total_size} | Speed: {speed} | ETA: {time_left}"
                        
                        # Only print if progress changed
                        if progress_str != last_progress:
                            print(f"\r{progress_str}", end='', flush=True)
                            last_progress = progress_str
                    
                except:
                    # If we can't read progress, just wait
                    pass
                
                # Small delay to avoid too frequent checks
                page.wait_for_timeout(1000)
                
                # Timeout after 30 minutes
                if time.time() - start_time > 1800:
                    print("\nUpload timeout - taking too long")
                    return None
            
            print("\nGetting share link...")
            page.wait_for_timeout(2000)
            
            # Try to find the share link in various ways
            share_link = None
            
            # Method 1: Look for input fields with the link
            link_selectors = [
                'input[name="lrb-link"]',
                'input[type="text"][readonly]',
                'input[readonly]',
                'input[type="text"]',
                'input[value*="transfer.it/t/"]'
            ]
            
            for selector in link_selectors:
                try:
                    elem = page.locator(selector).first
                    if elem.is_visible():
                        link_value = elem.input_value() or elem.get_attribute('value')
                        if link_value and 'transfer.it/t/' in link_value:
                            share_link = link_value
                            break
                except:
                    continue
            
            # Method 2: Click copy button and try to capture link
            if not share_link:
                try:
                    copy_link_button = page.locator('button.js-copy-link:not(.disabled)').first
                    if copy_link_button.is_visible():
                        copy_link_button.click()
                        page.wait_for_timeout(2000)
                        
                        # Try again to find the link after clicking copy
                        for selector in link_selectors:
                            try:
                                elem = page.locator(selector).first
                                if elem.is_visible():
                                    link_value = elem.input_value() or elem.get_attribute('value')
                                    if link_value and 'transfer.it/t/' in link_value:
                                        share_link = link_value
                                        break
                            except:
                                continue
                except:
                    pass
            
            # Method 3: Try opening link in new tab
            if not share_link:
                try:
                    def handle_page(new_page):
                        nonlocal share_link
                        new_page.wait_for_load_state()
                        share_link = new_page.url
                        new_page.close()
                    
                    context.on("page", handle_page)
                    
                    open_link_button = page.locator('button.js-show-content:has-text("Open link")')
                    if open_link_button.is_visible():
                        open_link_button.click()
                        page.wait_for_timeout(3000)
                        
                        if share_link and 'transfer.it/t/' in share_link:
                            return share_link
                except:
                    pass
            
            if share_link and 'transfer.it/t/' in share_link:
                return share_link
            else:
                print("Could not capture share link")
                page.screenshot(path="transfer_it_error.png")
                return None
            
        except Exception as e:
            print(f"\nError: {e}")
            try:
                page.screenshot(path="transfer_it_error.png")
                print("Screenshot saved as transfer_it_error.png")
            except:
                pass
            return None
            
        finally:
            browser.close()

def upload_to_transfer_it(file_path):
    """Upload with Rich UI - falls back to simple mode if display conflicts occur"""
    global display_conflict_detected
    
    if display_conflict_detected:
        # Already detected conflict, use simple mode directly
        return upload_to_transfer_it_simple(file_path)
    
    try:
        return _upload_to_transfer_it_rich(file_path)
    except Exception as e:
        error_msg = str(e).lower()
        if "only one live display may be active at once" in error_msg or "display" in error_msg:
            display_conflict_detected = True
            print("\n" + "="*60)
            print("⚠️  DISPLAY CONFLICT DETECTED")
            print("="*60)
            print("The Rich UI is conflicting with your terminal environment.")
            print("This can happen when:")
            print("• Multiple terminal sessions are active")
            print("• Running inside screen/tmux with complex display setup")
            print("• Terminal doesn't fully support Rich's display management")
            print()
            print("🔄 Automatically switching to simple mode...")
            print("="*60)
            print()
            return upload_to_transfer_it_simple(file_path)
        else:
            # Re raise other errors
            raise e

def _upload_to_transfer_it_rich(file_path):
    if sync_playwright is None:
        reexec_with_packaged_python_if_available()
        console.print("[red]❌ Playwright is not installed. Run: python3 -m pip install -r requirements.txt[/red]")
        return None

    if not os.path.exists(file_path):
        console.print(f"[red]❌ Error: File not found: {file_path}[/red]")
        return None
    
    file_name, file_size_mb = show_file_info(file_path)
    console.print("[cyan]🌐 Using browser upload session[/cyan]")

    kill_existing_browsers(show_message=True)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        
        init_task = progress.add_task("🌐 Starting browser upload session...", total=None)
        
        with sync_playwright() as p:
            global browser_instance
            browser_instance = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--single-process',
                    '--disable-gpu',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-extensions',
                    '--disable-plugins'
                ]
            )
            browser = browser_instance
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9'
                }
            )
            page = context.new_page()
            
            try:
                progress.update(init_task, description="🌐 Opening transfer.it...")
                page.goto("https://transfer.it", wait_until="domcontentloaded")
                
                progress.update(init_task, description="⏳ Loading page...")
                page.wait_for_timeout(5000)
                
                progress.update(init_task, description="📎 Selecting file...")
                file_input = page.locator('input[type="file"][name="select-file"]').first
                file_input.set_input_files(file_path)
                
                page.wait_for_timeout(2000)
                
                progress.update(init_task, description="🔄 Preparing transfer...")
                transfer_button = page.locator('button.js-get-link-button:has-text("Transfer")')
                
                page.wait_for_function(
                    """() => {
                        const btn = document.querySelector('button.js-get-link-button');
                        return btn && !btn.classList.contains('disabled');
                    }""",
                    timeout=10000
                )
                
                transfer_button.click()
                progress.update(init_task, description="⏳ Preparing upload...")
                
                page.wait_for_timeout(3000)
                progress.remove_task(init_task)
                
                setup_terminal_for_progress()
                
                try:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        TextColumn("ETA:"),
                        TimeRemainingColumn(),
                        console=console
                    ) as upload_progress:
                        
                        upload_task = upload_progress.add_task("🚀 Initializing transfer...", total=100)
                        
                        start_time = time.time()
                        last_activity_time = time.time()
                        upload_started = False
                        
                        while True:
                            try:
                                if (page.locator('h4:has-text("Completed!")').is_visible() or 
                                    page.locator('section.transferring-box.completed').count() > 0 or
                                    page.locator('.js-copy-link:not(.disabled)').is_visible()):
                                    upload_progress.update(upload_task, completed=100, description="✅ Transfer complete!")
                                    break
                            
                                try:
                                    uploaded_elem = page.locator('.status-info.transfer span.uploaded').first
                                    if uploaded_elem.is_visible():
                                        # Update status to show transfer is active
                                        if not upload_started:
                                            upload_started = True
                                        
                                        uploaded_text = uploaded_elem.text_content()
                                        size_elem = page.locator('.status-info.transfer span.size').first
                                        total_size_text = size_elem.text_content() if size_elem.is_visible() else "unknown"
                                        
                                        def parse_size_to_mb(size_text):
                                            """Convert size text to MB for calculation"""
                                            size_text = size_text.strip()
                                            if 'GB' in size_text:
                                                return float(size_text.replace('GB', '').strip()) * 1024
                                            elif 'MB' in size_text:
                                                return float(size_text.replace('MB', '').strip())
                                            elif 'KB' in size_text:
                                                return float(size_text.replace('KB', '').strip()) / 1024
                                            else:
                                                return 0
                                        
                                        try:
                                            uploaded_mb = parse_size_to_mb(uploaded_text)
                                            total_mb = parse_size_to_mb(total_size_text)
                                            
                                            if total_mb > 0:
                                                progress_percent = min((uploaded_mb / total_mb) * 100, 100)
                                            else:
                                                progress_percent = 0
                                            
                                            speed_elem = page.locator('.status-info.transfer span.speed').first
                                            speed_text = speed_elem.text_content() if speed_elem.is_visible() else ""
                                            
                                            description = f"📤 {uploaded_text} / {total_size_text}"
                                            if speed_text:
                                                description += f" • {speed_text}"
                                            
                                            upload_progress.update(upload_task, completed=progress_percent, description=description)
                                            last_activity_time = time.time()
                                        except Exception as parse_error:
                                            upload_progress.update(upload_task, description=f"📤 {uploaded_text} / {total_size_text}")
                                            last_activity_time = time.time()
                                except:
                                    pass
                                
                            except Exception as e:
                                console.print(f"[yellow]⚠️ Progress monitoring: {e}[/yellow]")
                            
                            # Only timeout if completely stalled (no activity for 15 minutes)
                            stall_timeout = 900  # 15 minutes without any activity
                            if time.time() - last_activity_time > stall_timeout:
                                console.print(f"[red]❌ Upload appears stalled - no activity for {stall_timeout//60} minutes[/red]")
                                console.print("[dim]This indicate a network issue or browser problem[/dim]")
                                return None
                            
                            page.wait_for_timeout(1000)
                
                finally:
                    # Always restore terminal input
                    restore_terminal_input()
            
                console.print("\n🔗 [bold cyan]Extracting share link...[/bold cyan]")
                page.wait_for_timeout(2000)
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                    transient=True
                ) as link_progress:
                    
                    link_task = link_progress.add_task("🔍 Finding share link...", total=None)
                    
                    try:
                        page.wait_for_selector('button.js-copy-link:not(.disabled)', timeout=10000)
                    except:
                        link_progress.update(link_task, description="🔄 Trying alternative methods...")
                    
                    share_link = None
                    
                    link_selectors = [
                        'input[name="lrb-link"]',
                        'input[type="text"][readonly]',
                        'input[readonly]',
                        'input[type="text"]',
                        'text=https://transfer.it/t/',
                        '[data-clipboard-text*="transfer.it/t/"]',
                        'input[value*="transfer.it/t/"]',
                        '.link-input',
                        '[data-clipboard-text]'
                    ]
                    
                    for selector in link_selectors:
                        try:
                            if selector.startswith('text='):
                                elem = page.locator(selector).first
                                if elem.is_visible():
                                    link_text = elem.text_content()
                                    if link_text and 'transfer.it/t/' in link_text:
                                        import re
                                        match = re.search(r'https://transfer\.it/t/[a-zA-Z0-9]+', link_text)
                                        if match:
                                            share_link = match.group(0)
                                            link_progress.update(link_task, description="✅ Link found in text!")
                                            return share_link
                            else:
                                elem = page.locator(selector).first
                                if elem.is_visible():
                                    link_value = (elem.input_value() if elem.get_attribute('type') == 'text' or elem.get_attribute('readonly') is not None 
                                                else elem.get_attribute('value') or elem.get_attribute('data-clipboard-text') or elem.text_content())
                                    
                                    if link_value and 'transfer.it/t/' in link_value:
                                        import re
                                        match = re.search(r'https://transfer\.it/t/[a-zA-Z0-9]+', link_value)
                                        if match:
                                            share_link = match.group(0)
                                            link_progress.update(link_task, description="✅ Link extracted!")
                                            return share_link
                        except Exception as e:
                            continue
                    
                    try:
                        # First, click the "Copy link" button in the "Completed!" modal
                        copy_button_modal = page.locator('button.js-copy-link').first
                        if copy_button_modal.is_visible():
                            link_progress.update(link_task, description="🔗 Clicking Copy link...")
                            copy_button_modal.click()
                            
                            # Wait for the page to transition to "Your link is ready!" section
                            page.wait_for_selector('.js-link-ready-section:not(.hidden)', timeout=10000)
                            page.wait_for_timeout(2000)
                            
                            # After clicking, the link might be available in various places
                            post_copy_selectors = [
                                '[data-clipboard-text]',
                                'input[readonly]', 
                                'input[type="text"]',
                                'input[value*="transfer.it/t/"]',
                                # Check if a new page or element appears with the link
                                'text=https://transfer.it/t/',
                                '.link-input'
                            ]
                            
                            for selector in post_copy_selectors:
                                try:
                                    if selector.startswith('text='):
                                        elem = page.locator(selector).first
                                        if elem.is_visible():
                                            link_text = elem.text_content()
                                            if link_text and 'transfer.it/t/' in link_text:
                                                import re
                                                match = re.search(r'https://transfer\.it/t/[a-zA-Z0-9]+', link_text)
                                                if match:
                                                    link_progress.update(link_task, description="✅ Link found after copy!")
                                                    return match.group(0)
                                    else:
                                        elem = page.locator(selector).first
                                        if elem.is_visible():
                                            link_value = elem.get_attribute('data-clipboard-text') or elem.input_value() or elem.get_attribute('value')
                                            if link_value and 'transfer.it/t/' in link_value:
                                                link_progress.update(link_task, description="✅ Link extracted from clipboard!")
                                                return link_value
                                except:
                                    continue
                        
                        # Also try the generic "Copy" button
                        copy_button_generic = page.locator('button:has-text("Copy")').first
                        if copy_button_generic.is_visible():
                            copy_button_generic.click()
                            page.wait_for_timeout(1000)
                            
                            for selector in ['[data-clipboard-text]', 'input[readonly]', 'input[type="text"]']:
                                try:
                                    elem = page.locator(selector).first
                                    if elem.is_visible():
                                        link_value = elem.get_attribute('data-clipboard-text') or elem.input_value()
                                        if link_value and 'transfer.it/t/' in link_value:
                                            link_progress.update(link_task, description="✅ Link copied!")
                                            return link_value
                                except:
                                    continue
                    except:
                        pass
                    
                    def handle_page(new_page):
                        nonlocal share_link
                        try:
                            new_page.wait_for_load_state()
                            share_link = new_page.url
                            new_page.close()
                        except:
                            pass
                    
                    try:
                        context.on("page", handle_page)
                        
                        open_link_button = page.locator('button.js-show-content:has-text("Open link")')
                        if open_link_button.is_visible():
                            open_link_button.click()
                            page.wait_for_timeout(3000)
                            
                            if share_link and 'transfer.it/t/' in share_link:
                                link_progress.update(link_task, description="✅ Link captured!")
                                return share_link
                                
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Error getting share link: {e}[/yellow]")
                    
                    console.print("[red]❌ Could not extract share link automatically[/red]")
                    page.screenshot(path="transfer_it_debug.png")
                    console.print("[dim]Debug screenshot saved as transfer_it_debug.png[/dim]")
                    return None
            
            except Exception as e:
                error_msg = str(e).lower()
                # Re-raise display conflicts so the wrapper can handle them
                if "only one live display may be active at once" in error_msg or "display" in error_msg:
                    raise e
                
                console.print(f"[red]❌ Error: {e}[/red]")
                try:
                    page.screenshot(path="transfer_it_error.png")
                    console.print("[dim]Screenshot saved as transfer_it_error.png[/dim]")
                except:
                    pass
                return None
                
            finally:
                cleanup_browser()
                browser_instance = None

def show_usage():
    console.print(Panel.fit(
        "[bold cyan]Transfer.it CLI Uploader[/bold cyan]\n\n"
        "[yellow]Usage:[/yellow] transferit upload [options] <file_or_folder>\n\n"
        "[yellow]Options:[/yellow]\n"
        "  --backend mega|browser   Upload backend (default: mega)\n"
        "  --simple                 Use simple text output (recommended for servers/tmux)\n"
        "  --no-fallback            Do not ask to retry with browser mode\n\n"
        "[yellow]Examples:[/yellow]\n"
        "  transferit upload /path/to/your/file.mp3\n"
        "  transferit upload /path/to/folder\n"
        "  transferit upload --backend browser /path/to/your/file.mp3",
        border_style="blue"
    ))

def show_success(share_link, file_name):
    success_text = Text()
    success_text.append("🎉 SUCCESS! ", style="bold green")
    success_text.append("Your file has been uploaded successfully!", style="green")
    
    result_table = Table(box=box.ROUNDED)
    result_table.add_column("", style="cyan", no_wrap=True)
    result_table.add_column("", style="bright_white")
    
    result_table.add_row("📁 File", file_name)
    result_table.add_row("🔗 Share Link", f"[link={share_link}]{share_link}[/link]")
    result_table.add_row("📋 Status", "[green]✅ Ready to share[/green]")
    
    console.print("\n")
    console.print(Panel(
        result_table,
        title=success_text,
        border_style="green",
        padding=(1, 2)
    ))

def humanise_bytes(num_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

def build_upload_kwargs(args):
    try:
        expiry = parse_expiry(args.expiry)
        schedule = parse_schedule(args.schedule)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc

    recipients = list(args.recipients or []) or None
    if schedule is not None and not recipients:
        raise argparse.ArgumentTypeError("--schedule requires at least one --recipient")

    return {
        "title": args.title,
        "message": args.message,
        "password": args.password,
        "sender": args.sender,
        "expiry": expiry,
        "notify_expiry": args.notify_expiry,
        "max_downloads": args.max_downloads,
        "recipients": recipients,
        "schedule": schedule,
        "concurrency": args.concurrency,
        "parallel": args.parallel,
        "exclude": list(args.excludes or []) or None,
    }

def has_mega_only_upload_options(args):
    return any([
        args.title,
        args.message,
        args.password,
        args.sender,
        args.expiry,
        args.notify_expiry,
        args.max_downloads,
        args.recipients,
        args.schedule,
        args.excludes,
        args.concurrency != 8,
        args.parallel is not None,
        args.json,
    ])

def show_mega_upload_summary(result, source, elapsed, kwargs):
    size = result.total_bytes
    rate = (size / elapsed / 1e6) if elapsed else 0
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(overflow="fold")
    table.add_row("title", f"[bold]{result.title}[/bold]")
    table.add_row("source", str(source))
    table.add_row(
        "content",
        f"{result.file_count} file{'s' if result.file_count != 1 else ''}"
        + (f", {result.folder_count} folder(s)" if result.folder_count else ""),
    )
    table.add_row("size", f"{humanise_bytes(size)} [dim]({size:,} bytes)[/dim]")
    table.add_row("elapsed", f"{elapsed:.1f}s [dim]({rate:.2f} MB/s)[/dim]")
    if kwargs.get("sender"):
        table.add_row("sender", kwargs["sender"])
    if kwargs.get("expiry"):
        table.add_row("expiry", f"{humanise_duration(kwargs['expiry'])} [dim]({kwargs['expiry']}s)[/dim]")
    if kwargs.get("password"):
        table.add_row("password", "[green]set[/green]")
    if kwargs.get("message"):
        message = kwargs["message"]
        table.add_row("message", message if len(message) < 60 else message[:57] + "...")
    if kwargs.get("max_downloads"):
        table.add_row("max downloads", str(kwargs["max_downloads"]))
    if kwargs.get("recipients"):
        table.add_row("recipients", ", ".join(kwargs["recipients"]))
        if kwargs.get("schedule") is not None:
            table.add_row("scheduled", time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(kwargs["schedule"])))
    table.add_row("share", f"[link={result.url}]{result.url}[/link]")
    console.print(Panel(table, title="transfer.it", title_align="right", border_style="green", box=box.ROUNDED))

def show_path_info(path):
    p = Path(path).expanduser().resolve()
    if p.is_file():
        table = Table(box=box.ROUNDED)
        table.add_column("Property", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")
        table.add_row("Type", "File")
        table.add_row("Name", p.name)
        table.add_row("Size", humanise_bytes(p.stat().st_size))
        table.add_row("Path", str(p))
        console.print(Panel(table, title="📁 Upload Information", border_style="blue"))
        return

    file_count = 0
    folder_count = 0
    total = 0
    for root, dirs, files in os.walk(p):
        folder_count += len(dirs)
        for name in files:
            fp = Path(root) / name
            if fp.is_file():
                file_count += 1
                total += fp.stat().st_size

    table = Table(box=box.ROUNDED)
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")
    table.add_row("Type", "Folder")
    table.add_row("Name", p.name)
    table.add_row("Files", str(file_count))
    table.add_row("Folders", str(folder_count))
    table.add_row("Total Size", humanise_bytes(total))
    table.add_row("Path", str(p))
    console.print(Panel(table, title="📁 Upload Information", border_style="blue"))

def upload_to_transfer_it_mega(path, simple_mode=False, upload_kwargs=None, quiet=False):
    upload_kwargs = upload_kwargs or {}
    if Transferit is None:
        reexec_with_packaged_python_if_available()
        raise RuntimeError("MEGA backend dependencies are missing. Run: python3 -m pip install -r requirements.txt")

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {p}")

    if quiet:
        with Transferit() as tx:
            return tx.upload(p, **upload_kwargs)

    if simple_mode:
        print(f"Uploading with MEGA backend: {p}")

        def on_progress(sent, total):
            arm_mega_upload_stall_alarm()
            percent = (sent / total * 100) if total else 0
            print(f"\rProgress: {humanise_bytes(sent)} / {humanise_bytes(total)} ({percent:.1f}%)", end="", flush=True)

        previous_handler = signal.getsignal(signal.SIGALRM) if hasattr(signal, "SIGALRM") else None
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, mega_upload_stall_handler)
        arm_mega_upload_stall_alarm()
        try:
            with Transferit() as tx:
                result = tx.upload(p, on_progress=on_progress, **upload_kwargs)
        finally:
            clear_mega_upload_stall_alarm(previous_handler)
        print()
        return result

    show_path_info(p)
    console.print("[cyan]⚡ Using MEGA backend[/cyan]")

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
        task_id = progress.add_task(f"📤 Uploading {p.name}", total=1)

        def on_start(total_bytes, file_count):
            arm_mega_upload_stall_alarm()
            label = f"📤 Uploading {file_count} file(s) from {p.name}"
            progress.update(task_id, description=label, total=total_bytes or 1)

        active_tasks = {}

        def on_file_start(idx, file_path, fsize):
            if not p.is_dir():
                return
            try:
                label = file_path.relative_to(p).as_posix()
            except ValueError:
                label = file_path.name
            active_tasks[idx] = progress.add_task(f"[dim]{label}[/dim]", total=fsize or 1)

        def on_file_progress(idx, file_path, sent, fsize):
            tid = active_tasks.get(idx)
            if tid is not None:
                progress.update(tid, completed=sent, total=fsize or 1)

        def on_file_done(idx, file_path, fsize):
            tid = active_tasks.pop(idx, None)
            if tid is not None:
                progress.update(tid, completed=fsize or 1, total=fsize or 1)
                progress.remove_task(tid)

        def on_progress(sent, total):
            arm_mega_upload_stall_alarm()
            progress.update(task_id, completed=sent, total=total or 1)

        previous_handler = signal.getsignal(signal.SIGALRM) if hasattr(signal, "SIGALRM") else None
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, mega_upload_stall_handler)
        arm_mega_upload_stall_alarm()
        try:
            with Transferit() as tx:
                result = tx.upload(
                    p,
                    on_start=on_start,
                    on_progress=on_progress,
                    on_file_start=on_file_start if p.is_dir() else None,
                    on_file_progress=on_file_progress if p.is_dir() else None,
                    on_file_done=on_file_done if p.is_dir() else None,
                    **upload_kwargs,
                )
        finally:
            clear_mega_upload_stall_alarm(previous_handler)

        progress.update(task_id, completed=result.total_bytes or 1, total=result.total_bytes or 1, description="✅ Upload complete")

    return result

def upload_with_retries(path, attempts, simple_mode=False, upload_kwargs=None, quiet=False):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if not simple_mode and not quiet:
                console.print(f"[cyan]Upload attempt {attempt}/{attempts} using MEGA backend[/cyan]")
            return upload_to_transfer_it_mega(path, simple_mode=simple_mode, upload_kwargs=upload_kwargs, quiet=quiet)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_error = exc
            if simple_mode or quiet:
                print(f"Upload attempt {attempt} failed on MEGA backend: {exc}")
            else:
                console.print(f"[yellow]⚠️ Upload attempt {attempt} failed on MEGA backend: {exc}[/yellow]")
            if attempt < attempts:
                time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"MEGA upload failed after {attempts} attempt(s): {last_error}")

def parse_args(argv):
    parser = argparse.ArgumentParser(prog="transferit upload", description="Upload files or folders to transfer.it")
    parser.add_argument("path", nargs="?", help="File or folder to upload")
    parser.add_argument("--simple", action="store_true", help="Use simple text output")
    parser.add_argument("--backend", choices=["mega", "browser"], help="Upload backend (default from config: mega)")
    parser.add_argument("--no-fallback", action="store_true", help="Do not ask to retry with browser mode if MEGA fails")
    parser.add_argument("-n", "--name", "--title", dest="title", help="Title shown on the transfer page")
    parser.add_argument("-c", "--concurrency", type=int, default=8, help="Parallel connections per file in MEGA mode (default: 8)")
    parser.add_argument("-j", "--parallel", type=int, help="Files uploaded at the same time in MEGA mode")
    parser.add_argument("-m", "--message", help="Short note displayed on the transfer page")
    parser.add_argument("-p", "--password", help="Require this password to open the transfer")
    parser.add_argument("-s", "--sender", "--from", dest="sender", metavar="EMAIL", help="Sender email; required with password/message/expiry/recipient")
    parser.add_argument("-e", "--expiry", metavar="DURATION", help="Transfer expiry duration, e.g. 30m, 2h, 7d, 1w, 1y")
    notify = parser.add_mutually_exclusive_group()
    notify.add_argument("--notify-expiry", dest="notify_expiry", action="store_true", help="Email sender before expiry")
    notify.add_argument("--no-notify-expiry", dest="notify_expiry", action="store_false", help="Do not email sender before expiry")
    parser.set_defaults(notify_expiry=False)
    parser.add_argument("--max-downloads", type=int, metavar="N", help="Stop allowing downloads after N successful fetches")
    parser.add_argument("-r", "--recipient", dest="recipients", action="append", metavar="EMAIL", help="Email this recipient the link; repeat for multiple")
    parser.add_argument("--schedule", metavar="TIME", help="Delay recipient email until ISO 8601 time or Unix timestamp")
    parser.add_argument("-x", "--exclude", dest="excludes", action="append", metavar="PATTERN", help="Skip folder files matching this glob; repeat for multiple")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON in MEGA mode")
    return parser.parse_args(argv)

def main(argv=None):
    global display_conflict_detected

    args = parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    simple_mode = args.simple
    backend = resolve_upload_backend(args.backend, config)
    retry_count = int(config.get("retry_count", 3))
    try:
        upload_kwargs = build_upload_kwargs(args)
    except argparse.ArgumentTypeError as exc:
        print(f"Error: {exc}") if simple_mode else console.print(f"[red]Error: {exc}[/red]")
        sys.exit(2)

    if args.concurrency < 1 or args.concurrency > 32:
        print("Error: --concurrency must be between 1 and 32") if simple_mode else console.print("[red]Error: --concurrency must be between 1 and 32[/red]")
        sys.exit(2)
    if args.parallel is not None and (args.parallel < 1 or args.parallel > 16):
        print("Error: --parallel must be between 1 and 16") if simple_mode else console.print("[red]Error: --parallel must be between 1 and 16[/red]")
        sys.exit(2)
    if args.max_downloads is not None and args.max_downloads < 1:
        print("Error: --max-downloads must be at least 1") if simple_mode else console.print("[red]Error: --max-downloads must be at least 1[/red]")
        sys.exit(2)

    if backend == "browser" and has_mega_only_upload_options(args):
        msg = "Browser upload does not support metadata/API options yet; use --backend mega for password, sender, expiry, recipients, exclude, concurrency, or --json."
        print(msg) if simple_mode else console.print(f"[red]{msg}[/red]")
        sys.exit(2)

    if backend == "mega" and (args.password or args.message or args.expiry) and not args.sender:
        hint = (
            "Error: --password, --message, and --expiry require --sender EMAIL.\n\n"
            "Example:\n"
            "  transferit upload file.zip --password secret --sender you@example.com\n\n"
            "The sender email is shown to recipients and is required by transfer.it\n"
            "when setting transfer metadata. Never sent in plain text."
        )
        print(hint) if simple_mode else console.print(f"[red]{hint}[/red]")
        sys.exit(2)

    if (backend == "mega" and Transferit is None) or (backend == "browser" and sync_playwright is None):
        reexec_with_packaged_python_if_available()

    if simple_mode and not args.json:
        print(f"transferit upload (backend={backend})")
    elif not args.json:
        try:
            console.print(f"\n[bold magenta]transferit upload[/bold magenta] [dim]backend={backend}[/dim]\n")
        except Exception as e:
            error_msg = str(e).lower()
            if "only one live display may be active at once" in error_msg or "display" in error_msg:
                display_conflict_detected = True
                print("\n" + "="*60)
                print("⚠️  DISPLAY CONFLICT DETECTED")
                print("="*60)
                print("The Rich UI is conflicting with your terminal environment.")
                print("This can happen when:")
                print("• Multiple terminal sessions are active")
                print("• Running inside screen/tmux with complex display setup")
                print("• Terminal doesn't fully support Rich's display management")
                print()
                print("🔄 Automatically switching to simple mode...")
                print("="*60)
                print()
                print("transferit upload")
                simple_mode = True  # Force simple mode for the rest of the session
            else:
                raise e

    if not args.path:
        if simple_mode or display_conflict_detected:
            print("Usage: transferit upload [--backend mega|browser] [--simple] <path>")
            print("Options:")
            print("  --backend    Upload backend: mega (default) or browser")
            print("  --simple     Use simple text output")
            print("Example: transferit upload /path/to/file-or-folder")
        else:
            try:
                show_usage()
            except Exception as e:
                error_msg = str(e).lower()
                if "only one live display may be active at once" in error_msg or "display" in error_msg:
                    print("Usage: transferit upload [--simple] <file_path>")
                    print("Options:")
                    print("  --simple    Use simple text output (recommended for your environment)")
                    print("Example: transferit upload /path/to/file.mp3")
                else:
                    raise e
        sys.exit(1)

    upload_path = args.path

    if not os.path.exists(upload_path):
        if simple_mode or display_conflict_detected:
            print(f"Error: Path not found: {upload_path}")
        else:
            try:
                console.print(f"[red]❌ Error: Path not found: {upload_path}[/red]")
            except Exception as e:
                error_msg = str(e).lower()
                if "only one live display may be active at once" in error_msg or "display" in error_msg:
                    print(f"Error: Path not found: {upload_path}")
                else:
                    raise e
        sys.exit(1)

    started = time.monotonic()
    result = None
    share_link = None
    if backend == "mega":
        try:
            result = upload_with_retries(
                upload_path,
                retry_count,
                simple_mode=simple_mode or display_conflict_detected,
                upload_kwargs=upload_kwargs,
                quiet=args.json,
            )
            share_link = result.url
        except Exception as exc:
            if simple_mode or display_conflict_detected:
                print(f"MEGA backend failed: {exc}")
            else:
                console.print(f"[red]❌ MEGA backend failed: {exc}[/red]")

            should_fallback = False
            if not args.no_fallback and config.get("prompt_browser_fallback", True):
                should_fallback = prompt_yes_no("MEGA backend failed. Retry using browser mode?", default=False)

            if should_fallback:
                if has_mega_only_upload_options(args):
                    msg = "Browser fallback would drop MEGA upload metadata/options, so fallback was not used. Retry without those options or use --backend mega."
                    print(msg) if simple_mode else console.print(f"[red]❌ {msg}[/red]")
                    sys.exit(1)
                if os.path.isdir(upload_path):
                    msg = "Browser fallback supports file uploads only. Use MEGA backend for folder uploads."
                    print(msg) if simple_mode else console.print(f"[red]❌ {msg}[/red]")
                    sys.exit(1)
                share_link = upload_to_transfer_it_simple(upload_path) if (simple_mode or display_conflict_detected) else upload_to_transfer_it(upload_path)
            else:
                sys.exit(1)
    else:
        if os.path.isdir(upload_path):
            msg = "Browser backend supports file uploads only. Use --backend mega for folder uploads."
            print(msg) if simple_mode else console.print(f"[red]❌ {msg}[/red]")
            sys.exit(1)
        share_link = upload_to_transfer_it_simple(upload_path) if (simple_mode or display_conflict_detected) else upload_to_transfer_it(upload_path)

    if result is not None and args.json:
        print_json(result.to_json_dict())
    elif result is not None and not (simple_mode or display_conflict_detected):
        show_mega_upload_summary(result, upload_path, time.monotonic() - started, upload_kwargs)
    elif share_link:
        if simple_mode or display_conflict_detected:
            print("\n" + "="*50)
            print("SUCCESS!")
            print("="*50)
            print("Your upload completed successfully.")
            print(f"Share link: {share_link}")
        else:
            show_success(share_link, os.path.basename(upload_path.rstrip(os.sep)))
    else:
        if simple_mode or display_conflict_detected:
            print("\nUpload failed. Please try again.")
        else:
            console.print("\n[red]❌ Upload failed. Please try again.[/red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
