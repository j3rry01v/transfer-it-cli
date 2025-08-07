#!/usr/bin/env python3
import sys
import os
import time
from playwright.sync_api import sync_playwright

def upload_to_transfer_it(file_path):
    
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return None
    
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    
    print(f"Uploading {file_name} ({file_size_mb:.2f} MB)...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
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
            
            page.wait_for_timeout(3000)
            
            upload_timeout = 300
            start_time = time.time()
            last_progress = ""
            
            while time.time() - start_time < upload_timeout:
                try:
                    if (page.locator('h4:has-text("Completed!")').is_visible() or 
                        page.locator('section.transferring-box.completed').count() > 0 or
                        page.locator('.js-copy-link:not(.disabled)').is_visible()):
                        print("\nUpload completed!")
                        break
                    
                    try:
                        uploaded_elem = page.locator('.status-info.transfer span.uploaded').first
                        if uploaded_elem.is_visible():
                            uploaded = uploaded_elem.text_content()
                            size_elem = page.locator('.status-info.transfer span.size').first
                            total_size = size_elem.text_content() if size_elem.is_visible() else "unknown"
                            
                            progress_str = f"Progress: {uploaded} / {total_size}"
                            
                            try:
                                speed_elem = page.locator('.status-info.transfer span.speed').first
                                if speed_elem.is_visible():
                                    speed = speed_elem.text_content()
                                    progress_str += f" | Speed: {speed}"
                                
                                time_elem = page.locator('.status-info.time span.left').first
                                if time_elem.is_visible():
                                    time_left = time_elem.text_content()
                                    progress_str += f" | ETA: {time_left}"
                            except:
                                pass
                            
                            if progress_str != last_progress:
                                print(f"\r{progress_str}", end='', flush=True)
                                last_progress = progress_str
                    except:
                        if not last_progress:
                            print("Upload in progress...", end='', flush=True)
                            last_progress = "uploading"
                    
                except Exception as e:
                    print(f"\nProgress monitoring error: {e}")
                
                page.wait_for_timeout(1000)
            
            if time.time() - start_time >= upload_timeout:
                print(f"\nUpload timed out after {upload_timeout} seconds")
                return None
            
            print("\nGetting share link...")
            page.wait_for_timeout(2000)
            
            try:
                page.wait_for_selector('button.js-copy-link:not(.disabled)', timeout=10000)
            except:
                print("Copy link button not found, trying alternative methods...")
            
            share_link = None
            
            try:
                link_input = page.locator('input[readonly]').first
                if link_input.is_visible():
                    share_link = link_input.input_value()
                    if share_link and 'transfer.it/t/' in share_link:
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
                    return share_link
                    
            except Exception as e:
                print(f"Error getting share link: {e}")
            
            print("Could not extract share link automatically")
            page.screenshot(path="transfer_it_debug.png")
            print("Debug screenshot saved as transfer_it_debug.png")
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

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 transfer-it-uploader.py <file_path>")
        print("\nExample:")
        print("  python3 transfer-it-uploader.py /path/to/your/file.mp3")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    share_link = upload_to_transfer_it(file_path)
    
    if share_link:
        print("\n" + "="*50)
        print("SUCCESS!")
        print("="*50)
        print(f"Your file has been uploaded successfully.")
        print(f"Share link: {share_link}")
    else:
        print("\nUpload failed. Please try again.")
        sys.exit(1)

if __name__ == "__main__":
    main()
