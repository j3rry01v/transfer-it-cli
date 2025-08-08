
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

def show_file_info(file_path):
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

def upload_to_transfer_it(file_path):
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
                args=['--disable-blink-features=AutomationControlled']
            )
            browser = browser_instance
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
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
                        upload_timeout = 300
                        start_time = time.time()
                        upload_started = False
                        
                        while time.time() - start_time < upload_timeout:
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
                                        except Exception as parse_error:
                                            upload_progress.update(upload_task, description=f"📤 {uploaded_text} / {total_size_text}")
                                except:
                                    pass
                                
                            except Exception as e:
                                console.print(f"[yellow]⚠️ Progress monitoring: {e}[/yellow]")
                            
                            page.wait_for_timeout(1000)
                        
                        if time.time() - start_time >= upload_timeout:
                            console.print("[red]❌ Upload timed out after 5 minutes[/red]")
                            return None
                
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
                    
                    try:
                        link_input = page.locator('input[readonly]').first
                        if link_input.is_visible():
                            share_link = link_input.input_value()
                            if share_link and 'transfer.it/t/' in share_link:
                                link_progress.update(link_task, description="✅ Link found!")
                                return share_link
                    except:
                        pass
                    
                    try:
                        copy_link_button = page.locator('button.js-copy-link:not(.disabled)').first
                        if copy_link_button.is_visible():
                            copy_link_button.click()
                            page.wait_for_timeout(1000)
                        
                        link_selectors = [
                            'input[readonly]',
                            '.link-input',
                            '[data-clipboard-text]',
                            'input[value*="transfer.it/t/"]'
                        ]
                        
                        for selector in link_selectors:
                            try:
                                elem = page.locator(selector).first
                                if elem.is_visible():
                                    link_value = elem.input_value() or elem.get_attribute('value') or elem.get_attribute('data-clipboard-text')
                                    if link_value and 'transfer.it/t/' in link_value:
                                        link_progress.update(link_task, description="✅ Link extracted!")
                                        return link_value
                            except:
                                continue
                        
                        def handle_page(new_page):
                            nonlocal share_link
                            try:
                                new_page.wait_for_load_state()
                                share_link = new_page.url
                                new_page.close()
                            except:
                                pass
                        
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
        "[yellow]Usage:[/yellow] python3 transfer-it-uploader.py <file_path>\n\n"
        "[yellow]Example:[/yellow]\n"
        "  python3 transfer-it-uploader.py /path/to/your/file.mp3",
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
    console.print("\n[bold magenta]🚀 Transfer.it CLI Uploader[/bold magenta]\n")
    
    if len(sys.argv) < 2:
        show_usage()
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    if not os.path.exists(file_path):
        console.print(f"[red]❌ Error: File not found: {file_path}[/red]")
        sys.exit(1)
    
    share_link = upload_to_transfer_it(file_path)
    
    if share_link:
        show_success(share_link, os.path.basename(file_path))
    else:
        console.print("\n[red]❌ Upload failed. Please try again.[/red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
