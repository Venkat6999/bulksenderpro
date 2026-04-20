"""
Debug script to check what file inputs are available on WhatsApp Web
Run this after starting the server and connecting WhatsApp
"""

import time
from playwright.sync_api import sync_playwright

def debug_whatsapp_inputs():
    with sync_playwright() as p:
        # Connect to existing browser or create new one
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        print("Opening WhatsApp Web...")
        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        
        print("\nWait for WhatsApp to load and scan QR if needed...")
        input("Press ENTER when WhatsApp is ready and you're in a chat...")
        
        # Click attach button
        print("\nClicking attach button...")
        attach_btn = page.wait_for_selector('[data-icon="clip"]', timeout=10000)
        attach_btn.click()
        time.sleep(2)
        
        # Log all file inputs
        print("\n" + "="*80)
        print("FILE INPUTS FOUND:")
        print("="*80)
        
        inputs_info = page.evaluate("""
            () => {
                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                return inputs.map((input, idx) => ({
                    index: idx,
                    accept: input.accept,
                    id: input.id,
                    className: input.className,
                    visible: input.offsetParent !== null,
                    parentText: input.parentElement ? input.parentElement.textContent.substring(0, 100) : 'no parent'
                }));
            }
        """)
        
        for info in inputs_info:
            print(f"\nInput #{info['index']}:")
            print(f"  accept: {info['accept']}")
            print(f"  id: {info['id']}")
            print(f"  visible: {info['visible']}")
            print(f"  parent text: {info['parentText']}")
        
        print("\n" + "="*80)
        print("MENU ITEMS FOUND:")
        print("="*80)
        
        menu_items = page.evaluate("""
            () => {
                const items = document.querySelectorAll('[role="menuitem"]');
                return Array.from(items).map((item, idx) => ({
                    index: idx,
                    text: item.textContent.substring(0, 100)
                }));
            }
        """)
        
        for item in menu_items:
            print(f"Menu #{item['index']}: {item['text']}")
        
        print("\n" + "="*80)
        print("ALL SPANS WITH 'PHOTOS' OR 'VIDEO':")
        print("="*80)
        
        spans = page.evaluate("""
            () => {
                const allSpans = Array.from(document.querySelectorAll('span'));
                return allSpans
                    .filter(span => span.textContent.includes('Photos') || span.textContent.includes('Video'))
                    .map((span, idx) => ({
                        index: idx,
                        text: span.textContent.substring(0, 100),
                        parentTag: span.parentElement ? span.parentElement.tagName : 'none',
                        parentRole: span.parentElement ? span.parentElement.getAttribute('role') : 'none'
                    }));
            }
        """)
        
        for span in spans:
            print(f"Span #{span['index']}: {span['text']}")
            print(f"  parent: {span['parentTag']} (role: {span['parentRole']})")
        
        print("\n" + "="*80)
        
        input("\nPress ENTER to close browser...")
        browser.close()

if __name__ == "__main__":
    debug_whatsapp_inputs()
