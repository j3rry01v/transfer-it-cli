
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
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

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
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return None
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    
    print(f"Uploading {file_name} ({file_size_mb:.2f} MB)...")
    
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
    if not os.path.exists(file_path):
        console.print(f"[red]❌ Error: File not found: {file_path}[/red]")
        return None
    
    file_name, file_size_mb = show_file_info(file_path)

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
        "[yellow]Usage:[/yellow] python3 transfer-it-uploader.py [--simple] <file_path>\n\n"
        "[yellow]Options:[/yellow]\n"
        "  --simple    Use simple text output (recommended for servers/tmux)\n\n"
        "[yellow]Examples:[/yellow]\n"
        "  python3 transfer-it-uploader.py /path/to/your/file.mp3\n"
        "  python3 transfer-it-uploader.py --simple /path/to/your/file.mp3",
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

def main():
    global display_conflict_detected
    
    # Check for simple mode flag (now only enabled explicitly since Rich works on servers)
    simple_mode = '--simple' in sys.argv
    
    # Try to show title with Rich, fall back to simple if display conflict
    if simple_mode:
        print("Transfer.it CLI Uploader")
    else:
        try:
            console.print("\n[bold magenta]🚀 Transfer.it CLI Uploader[/bold magenta]\n")
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
                print("Transfer.it CLI Uploader")
                simple_mode = True  # Force simple mode for the rest of the session
            else:
                raise e
    
    # Filter out flags from arguments
    file_args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
    
    if len(file_args) < 1:
        if simple_mode or display_conflict_detected:
            print("Usage: python3 transfer-it-uploader.py [--simple] <file_path>")
            print("Options:")
            print("  --simple    Use simple text output (optional)")
            print("Example: python3 transfer-it-uploader.py /path/to/file.mp3")
        else:
            try:
                show_usage()
            except Exception as e:
                error_msg = str(e).lower()
                if "only one live display may be active at once" in error_msg or "display" in error_msg:
                    print("Usage: python3 transfer-it-uploader.py [--simple] <file_path>")
                    print("Options:")
                    print("  --simple    Use simple text output (recommended for your environment)")
                    print("Example: python3 transfer-it-uploader.py /path/to/file.mp3")
                else:
                    raise e
        sys.exit(1)
    
    file_path = file_args[0]
    
    if not os.path.exists(file_path):
        if simple_mode or display_conflict_detected:
            print(f"Error: File not found: {file_path}")
        else:
            try:
                console.print(f"[red]❌ Error: File not found: {file_path}[/red]")
            except Exception as e:
                error_msg = str(e).lower()
                if "only one live display may be active at once" in error_msg or "display" in error_msg:
                    print(f"Error: File not found: {file_path}")
                else:
                    raise e
        sys.exit(1)
    
    # Choose upload method based on mode
    if simple_mode:
        share_link = upload_to_transfer_it_simple(file_path)
        
        if share_link:
            print("\n" + "="*50)
            print("SUCCESS!")
            print("="*50)
            print(f"Your file has been uploaded successfully.")
            print(f"Share link: {share_link}")
        else:
            print("\nUpload failed. Please try again.")
            sys.exit(1)
    else:
        share_link = upload_to_transfer_it(file_path)
        
        if share_link:
            show_success(share_link, os.path.basename(file_path))
        else:
            console.print("\n[red]❌ Upload failed. Please try again.[/red]")
            console.print("\n[yellow]💡 If you continue having issues, try:[/yellow]")
            console.print("[dim]   python3 transfer-it-uploader.py --simple <file_path>[/dim]")
            sys.exit(1)

if __name__ == "__main__":
    main()
