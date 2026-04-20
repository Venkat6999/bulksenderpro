import os
import time
import base64
import logging
import threading
import queue
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    log.warning("Playwright not installed. Run: playwright install chromium")

try:
    import qrcode
    from io import BytesIO
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False


class WhatsAppClient:
    WA_URL = "https://web.whatsapp.com"
    POLL_INTERVAL = 2

    def __init__(
        self,
        session_path: str = "./sessions",
        on_qr: Optional[Callable] = None,
        on_authenticated: Optional[Callable] = None,
        on_ready: Optional[Callable] = None,
        on_auth_failure: Optional[Callable] = None,
        on_disconnected: Optional[Callable] = None,
        is_docker: bool = False,
    ):
        self.session_path = Path(session_path)
        self.session_path.mkdir(parents=True, exist_ok=True)

        self._on_qr = on_qr or (lambda x: None)
        self._on_authenticated = on_authenticated or (lambda: None)
        self._on_ready = on_ready or (lambda: None)
        self._on_auth_failure = on_auth_failure or (lambda m: None)
        self._on_disconnected = on_disconnected or (lambda r: None)

        self.is_docker = is_docker
        self.is_ready = False
        self.last_qr = None
        self._stop = False
        self._page: Optional[Page] = None
        self._context: Optional[BrowserContext] = None
        self._browser: Optional[Browser] = None
        self._playwright: Optional[Playwright] = None
        self._thread: Optional[threading.Thread] = None
        self._last_qr_text: Optional[str] = None
        self._task_queue = queue.Queue()

    def _dispatch(self, func, *args, **kwargs):
        if not self._thread or threading.current_thread() == self._thread:
            return func(*args, **kwargs)
        future = Future()
        self._task_queue.put((func, args, kwargs, future))
        return future.result()

    def initialize(self):
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="wa-client")
        self._thread.start()

    def logout(self):
        return self._dispatch(self._logout)

    def _logout(self):
        self._stop = True
        self.is_ready = False
        try:
            if self._page and not self._page.is_closed():
                self._page.evaluate("""
                    async () => {
                        const menuBtn = document.querySelector('[data-testid="menu"]');
                        if (menuBtn) menuBtn.click();
                    }
                """)
                time.sleep(0.5)
                self._page.evaluate("""
                    async () => {
                        const items = document.querySelectorAll('[data-testid="menu-item"]');
                        for (const item of items) {
                            if (item.textContent.includes('Log out')) {
                                item.click();
                                break;
                            }
                        }
                    }
                """)
                time.sleep(1)
        except Exception as e:
            log.warning(f"Logout page interaction failed: {e}")

        self._cleanup()

        import shutil
        try:
            shutil.rmtree(str(self.session_path / "Default"), ignore_errors=True)
        except Exception:
            pass

        self._on_disconnected("LOGOUT")

    def is_registered(self, chat_id: str) -> bool:
        return self._dispatch(self._is_registered, chat_id)

    def _is_registered(self, chat_id: str) -> bool:
        if not self.is_ready or not self._page:
            raise RuntimeError("Client not ready")
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    try {{
                        const wid = window.WWebJS?.utils?.getWid("{chat_id}");
                        if (!wid) return true;
                        const contact = await window.WWebJS.getContact(wid);
                        return !!contact;
                    }} catch(e) {{
                        return true;
                    }}
                }}
            """)
            return bool(result)
        except Exception:
            return True

    def send_message(self, chat_id: str, message: str):
        return self._dispatch(self._send_message, chat_id, message)

    def _send_message(self, chat_id: str, message: str):
        if not self.is_ready or not self._page:
            raise RuntimeError("Client not ready")

        phone = chat_id.replace("@c.us", "")
        url = f"https://web.whatsapp.com/send?phone={phone}"

        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)

            box_selector = 'div[title="Type a message"], div[contenteditable="true"][data-tab="10"], [title="Type a message"]'
            invalid_selector = '[data-testid="popup-contents"]'

            loc = self._page.wait_for_selector(f"{box_selector}, {invalid_selector}", timeout=30000)

            if self._page.query_selector(invalid_selector):
                ok_btn = self._page.query_selector('button[data-testid="popup-controls-ok"], button:has-text("OK")')
                if ok_btn:
                    ok_btn.click(force=True)
                raise RuntimeError("Invalid/Unregistered number")

            if loc:
                loc.click()
                time.sleep(0.2)
                loc.fill(message)
                loc.press("Space")
                self._page.keyboard.press("Backspace")
                time.sleep(0.5)
                loc.press("Enter")
                time.sleep(0.5)

                send_btn = self._page.query_selector('[data-testid="send"], [data-icon="send"], button[aria-label="Send"]')
                if send_btn:
                    send_btn.click(force=True)

                time.sleep(0.5)
            else:
                raise RuntimeError("Chat box not found")
        except Exception as e:
            log.error(f"Send message error to {phone}: {e}")
            raise

    def send_media(self, chat_id: str, mimetype: str, data: str, filename: str, caption: str = ""):
        return self._dispatch(self._send_media, chat_id, mimetype, data, filename, caption)

    def _send_media(self, chat_id: str, mimetype: str, data: str, filename: str, caption: str = ""):
        if not self.is_ready or not self._page:
            raise RuntimeError("Client not ready")

        phone = chat_id.replace("@c.us", "")
        url = f"https://web.whatsapp.com/send?phone={phone}"

        log.info(f"Sending media to {phone}: {mimetype} {filename}")

        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)

            box_selector = 'div[title="Type a message"], div[contenteditable="true"][data-tab="10"], [title="Type a message"]'
            invalid_selector = '[data-testid="popup-contents"]'

            self._page.wait_for_selector(f"{box_selector}, {invalid_selector}", timeout=30000)

            if self._page.query_selector(invalid_selector):
                ok_btn = self._page.query_selector('button[data-testid="popup-controls-ok"], button:has-text("OK")')
                if ok_btn:
                    ok_btn.click(force=True)
                raise RuntimeError("Invalid/Unregistered number")

            raw_bytes = base64.b64decode(data)
            safe_mimetype, safe_filename, safe_bytes = self._normalize_media_for_whatsapp(
                mimetype, filename, raw_bytes
            )
            
            log.info(f"Final media format: {safe_mimetype} {safe_filename} ({len(safe_bytes)} bytes)")

            import tempfile
            suffix = self._ext_from_mime(safe_mimetype)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(safe_bytes)
                tmp_path = tmp.name

            try:
                attach_btn_selector = (
                    '[data-testid="attach-menu-plus"], '
                    '[data-icon="clip"], '
                    '[data-icon="plus"], '
                    '[title="Attach"], '
                    '[aria-label="Attach"]'
                )
                attach_btn = self._page.wait_for_selector(attach_btn_selector, timeout=10000)
                if not attach_btn:
                    raise RuntimeError("Attach button not found")

                log.info("Clicking attach button...")
                attach_btn.click(force=True)
                time.sleep(2.0)

                # Try clicking attach button again to ensure menu is open
                try:
                    attach_btn.click(force=True)
                    time.sleep(0.5)
                except:
                    pass

                # Click "Photos & Videos" option from the attach menu
                # This is CRITICAL - without this, sticker uploader gets used
                photos_videos_selector = (
                    '[aria-label="Photos & Videos"], '
                    'span:has-text("Photos & Videos"), '
                    'div[role="menuitem"]:has-text("Photos & Videos")'
                )
                
                photos_clicked = False
                try:
                    # Try direct selector first
                    photos_btn = self._page.query_selector(photos_videos_selector)
                    if photos_btn:
                        log.info("Clicking Photos & Videos menu item (direct selector)...")
                        photos_btn.click(force=True)
                        photos_clicked = True
                        time.sleep(2.0)
                    else:
                        # Fallback: try to find by iterating menu items
                        log.info("Using fallback to find Photos & Videos...")
                        result = self._page.evaluate("""
                            () => {
                                const allSpans = Array.from(document.querySelectorAll('span'));
                                for (const span of allSpans) {
                                    if (span.textContent.includes('Photos') && span.textContent.includes('Videos')) {
                                        const clickable = span.closest('[role="menuitem"], button, div[tabindex="0"]') || span.parentElement;
                                        if (clickable) {
                                            clickable.click();
                                            return true;
                                        }
                                    }
                                }
                                
                                // Also try looking for the actual menu structure
                                const menuItems = document.querySelectorAll('[role="menuitem"]');
                                for (const item of menuItems) {
                                    if (item.textContent.includes('Photos')) {
                                        item.click();
                                        return true;
                                    }
                                }
                                
                                // Last resort: look for any element with camera/photo icon
                                const allDivs = Array.from(document.querySelectorAll('div'));
                                for (const div of allDivs) {
                                    if (div.textContent.includes('Photos') && div.children.length < 5) {
                                        div.click();
                                        return true;
                                    }
                                }
                                
                                return false;
                            }
                        """)
                        
                        if result:
                            log.info("Photos & Videos clicked via fallback")
                            photos_clicked = True
                        else:
                            log.warning("Could not find Photos & Videos button in menu")
                        
                        time.sleep(2.0)
                except Exception as e:
                    log.warning(f"Could not click Photos & Videos button: {e}")
                    time.sleep(2.0)

                file_input = self._get_media_file_input(safe_mimetype)
                if not file_input:
                    log.warning("Smart file input detection failed, trying fallback...")
                    # Fallback: try to get ANY visible file input that accepts images
                    try:
                        file_input = self._page.evaluate_handle("""
                            () => {
                                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                                console.log('Fallback: checking', inputs.length, 'inputs');
                                
                                // Find first input that accepts images
                                for (const input of inputs) {
                                    const accept = (input.accept || '').toLowerCase();
                                    if (accept.includes('image') || accept.includes('*')) {
                                        console.log('Fallback: using input with accept:', input.accept);
                                        return input;
                                    }
                                }
                                
                                // Last resort: return first input
                                if (inputs.length > 0) {
                                    console.log('Fallback: using first input');
                                    return inputs[0];
                                }
                                
                                return null;
                            }
                        """)
                        file_input = file_input.as_element() if file_input else None
                    except Exception as e:
                        log.error(f"Fallback file input detection failed: {e}")
                    
                    if not file_input:
                        raise RuntimeError("Media upload input not found")

                log.info("Setting file input...")
                file_input.set_input_files(tmp_path)
                time.sleep(2.5)

                if caption:
                    caption_box = self._find_caption_box()
                    if caption_box:
                        caption_box.click(force=True)
                        time.sleep(0.2)
                        try:
                            caption_box.fill(caption)
                        except Exception:
                            self._page.keyboard.type(caption, delay=15)
                        caption_box.press("Space")
                        self._page.keyboard.press("Backspace")
                        time.sleep(0.3)

                if not self._click_send_button():
                    self._page.keyboard.press("Enter")

                time.sleep(3.0)

            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        except Exception as e:
            log.error(f"Send media error to {phone}: {e}")
            raise

    def _get_media_file_input(self, mimetype: str):
        """
        Target the MEDIA/GALLERY uploader only, not sticker uploader.
        Media uploader accepts both image/* and video/mp4.
        Sticker uploader only accepts image/* (specifically webp).
        """
        try:
            handle = self._page.evaluate_handle("""
                (mime) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                    if (!inputs.length) return null;

                    const isImage = mime.startsWith('image/');
                    const isVideo = mime.startsWith('video/');

                    // Log all inputs for debugging
                    console.log('File inputs found:', inputs.length);
                    inputs.forEach((input, idx) => {
                        console.log(`Input ${idx}:`, input.accept);
                    });

                    // PRIORITY 1: Find the media/gallery input that accepts both images AND videos
                    for (const input of inputs) {
                        const accept = (input.accept || '').toLowerCase().replace(/\\s+/g, '');
                        
                        // Best match: accepts both image and video (media/gallery uploader)
                        if (accept.includes('image') && accept.includes('video')) {
                            console.log('Selected: Media uploader (image + video)');
                            return input;
                        }
                    }
                    
                    // PRIORITY 2: For images, find input that accepts common image formats but NOT webp-only
                    if (isImage) {
                        for (const input of inputs) {
                            const accept = (input.accept || '').toLowerCase().replace(/\\s+/g, '');
                            // Must accept png/jpg/jpeg and must NOT be webp-only
                            if ((accept.includes('png') || accept.includes('jpeg') || accept.includes('jpg')) && 
                                !(accept === 'image/webp' || accept === '.webp')) {
                                console.log('Selected: Image uploader (png/jpg)');
                                return input;
                            }
                        }
                    }
                    
                    // PRIORITY 3: For videos, find video acceptor
                    if (isVideo) {
                        for (const input of inputs) {
                            const accept = (input.accept || '').toLowerCase().replace(/\\s+/g, '');
                            if (accept.includes('video') || accept.includes('mp4')) {
                                console.log('Selected: Video uploader');
                                return input;
                            }
                        }
                    }
                    
                    console.log('No suitable input found');
                    return null;
                }
            """, mimetype)
            return handle.as_element() if handle else None
        except Exception as e:
            log.warning(f"Media input detection failed: {e}")
            return None

    def _find_caption_box(self):
        selectors = [
            '[data-testid="media-caption-input"]',
            'div[contenteditable="true"][data-tab="10"]',
            'div[contenteditable="true"][data-tab="1"]',
            'div[contenteditable="true"][role="textbox"]',
        ]
        for sel in selectors:
            try:
                el = self._page.query_selector(sel)
                if el:
                    return el
            except Exception:
                pass
        return None

    def _click_send_button(self) -> bool:
        selectors = [
            '[data-testid="send"]',
            '[data-testid="compose-btn-send"]',
            '[data-icon="send"]',
            '[data-icon*="send"]',
            'button[aria-label="Send"]',
            'span[data-icon="wds-ic-send-filled"]',
        ]
        for sel in selectors:
            try:
                btn = self._page.query_selector(sel)
                if btn:
                    btn.click(force=True)
                    return True
            except Exception:
                pass

        try:
            return bool(self._page.evaluate("""
                () => {
                    const selectors = [
                        '[data-testid="send"]',
                        '[data-testid="compose-btn-send"]',
                        '[data-icon="send"]',
                        '[data-icon*="send"]',
                        'button[aria-label="Send"]',
                        'span[data-icon="wds-ic-send-filled"]'
                    ];

                    let btn = null;
                    for (const sel of selectors) {
                        const found = document.querySelector(sel);
                        if (found) {
                            btn = found;
                            break;
                        }
                    }
                    if (!btn) return false;

                    let target = btn;
                    while (
                        target &&
                        target.tagName !== 'BUTTON' &&
                        target.getAttribute('role') !== 'button' &&
                        target.parentElement
                    ) {
                        target = target.parentElement;
                    }
                    if (!target) target = btn;

                    ['mousedown', 'mouseup', 'click'].forEach(evtType => {
                        target.dispatchEvent(new MouseEvent(evtType, {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }));
                    });
                    return true;
                }
            """))
        except Exception:
            return False

    def _normalize_media_for_whatsapp(self, mimetype: str, filename: str, raw_bytes: bytes):
        """
        Convert WEBP to JPG so WhatsApp doesn't treat it like sticker media.
        Also flatten transparency.
        """
        lower_name = (filename or "").lower()
        lower_type = (mimetype or "").lower()

        try:
            from PIL import Image
            from io import BytesIO
        except Exception:
            return mimetype, filename, raw_bytes

        if not lower_type.startswith("image/") and not lower_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
            return mimetype, filename, raw_bytes

        try:
            img = Image.open(BytesIO(raw_bytes))
            log.info(f"Media normalization: {mimetype} {filename} (PIL format: {img.format}, mode: {img.mode})")

            # ALWAYS convert WEBP to JPG - this is critical to avoid sticker behavior
            if lower_type == "image/webp" or lower_name.endswith(".webp") or (img.format or "").upper() == "WEBP":
                log.info("Converting WEBP to JPEG to prevent sticker behavior")
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                out = BytesIO()
                bg.save(out, format="JPEG", quality=92)
                new_name = os.path.splitext(filename or "image")[0] + ".jpg"
                return "image/jpeg", new_name, out.getvalue()

            # Handle PNG and JPEG
            if lower_type in ("image/png", "image/jpeg", "image/jpg"):
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg

                out = BytesIO()
                if lower_type == "image/png":
                    img.save(out, format="PNG")
                    log.info(f"Keeping as PNG: {filename}")
                    return "image/png", os.path.splitext(filename or "image")[0] + ".png", out.getvalue()
                else:
                    img = img.convert("RGB")
                    img.save(out, format="JPEG", quality=92)
                    log.info(f"Converting to JPEG: {filename}")
                    return "image/jpeg", os.path.splitext(filename or "image")[0] + ".jpg", out.getvalue()

            return mimetype, filename, raw_bytes

        except Exception as e:
            log.warning(f"Media normalization failed: {e}")
            return mimetype, filename, raw_bytes

    def _run(self):
        try:
            self._playwright = sync_playwright().start()

            launch_opts = {
                "headless": self.is_docker,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-extensions",
                ],
            }

            browser_type = self._playwright.chromium
            user_data_dir = str(self.session_path)

            launch_opts.update({
                "viewport": {"width": 1280, "height": 800},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "device_scale_factor": 1,
            })

            self._context = browser_type.launch_persistent_context(
                user_data_dir,
                **launch_opts
            )
            
            # STEALTH: Robust evasion for headless detection
            self._context.add_init_script("""
                // Hide webdriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                
                // Mock languages
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                
                // Mock plugins
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                
                // Mock WebGL vendor/renderer
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
                    return getParameter.apply(this, arguments);
                };
            """)
            
            self._page = self._context.new_page()
            self._page.on("console", lambda m: None)
            
            log.info(f"Opening {self.WA_URL} ...")
            self._page.goto(self.WA_URL, wait_until="domcontentloaded", timeout=60000)

            self._poll_loop()

        except Exception as e:
            log.error(f"Browser error: {e}")
            self._on_auth_failure(str(e))
        finally:
            self._cleanup()

    def _poll_loop(self):
        authenticated = False
        ready_emitted = False
        consecutive_errors = 0
        authenticated_since = None  # Track when we first became authenticated

        log.info("=== WhatsApp Client v1.2 Polling Loop Started ===")
        while not self._stop:
            try:
                if self._page.is_closed():
                    self._on_disconnected("PAGE_CLOSED")
                    break

                try:
                    func, args, kwargs, future = self._task_queue.get(timeout=self.POLL_INTERVAL)
                    try:
                        res = func(*args, **kwargs)
                        future.set_result(res)
                    except Exception as e:
                        future.set_exception(e)
                    self._task_queue.task_done()
                    continue
                except queue.Empty:
                    pass

                state = self._detect_state()

                if state == "qr":
                    qr_text = self._extract_qr_text()
                    data_url = None
                    authenticated_since = None  # Reset authentication timer
                    
                    if qr_text:
                        if qr_text != self._last_qr_text:
                            log.info(f"New QR text detected (starts with: {qr_text[:10]}...)")
                            self._last_qr_text = qr_text
                            data_url = self._qr_to_data_url(qr_text)
                        else:
                            # QR text hasn't changed, no need to re-emit
                            pass
                    else:
                        # No text found, try visual capture
                        log.info("QR text not found in DOM, attempting visual capture...")
                        try:
                            # Try multiple selectors for the canvas
                            qr_el = self._page.query_selector('canvas[aria-label="Scan me!"]') or \
                                    self._page.query_selector('div[data-ref] canvas') or \
                                    self._page.query_selector('canvas')
                            
                            if qr_el:
                                log.info("Capturing QR via element screenshot...")
                                screenshot_bytes = qr_el.screenshot()
                                b64 = base64.b64encode(screenshot_bytes).decode()
                                data_url = f"data:image/png;base64,{b64}"
                            else:
                                log.info("QR element not found specifically, performing center-page crop...")
                                # Last resort: Capture the middle area
                                screenshot_bytes = self._page.screenshot(clip={"x": 400, "y": 100, "width": 500, "height": 500})
                                b64 = base64.b64encode(screenshot_bytes).decode()
                                data_url = f"data:image/png;base64,{b64}"
                        except Exception as e:
                            log.error(f"Visual capture failed: {e}")

                    if data_url and data_url != self.last_qr:
                        self.last_qr = data_url
                        self._on_qr(data_url)
                        authenticated = False
                        ready_emitted = False

                elif state == "authenticated" and not authenticated:
                    authenticated = True
                    authenticated_since = time.time()  # Start the timer
                    self.last_qr = None
                    log.info("WhatsApp authenticated ✅")
                    self._on_authenticated()

                elif state == "authenticated" and authenticated and not ready_emitted:
                    # TIMEOUT FALLBACK: If we've been authenticated for 30+ seconds,
                    # assume we're ready even if chat list selector doesn't match
                    if authenticated_since and (time.time() - authenticated_since) > 30:
                        log.warning("⚠️ Timeout fallback: Forcing 'ready' state after 30s in authenticated")
                        ready_emitted = True
                        self.is_ready = True
                        self.last_qr = None
                        self._on_ready()
                    else:
                        # Still waiting, log progress every 10 seconds
                        if authenticated_since and int(time.time() - authenticated_since) % 10 == 0:
                            elapsed = int(time.time() - authenticated_since)
                            log.info(f"Still authenticating... ({elapsed}s elapsed, waiting for chat list)")

                elif state == "ready" and not ready_emitted:
                    ready_emitted = True
                    self.is_ready = True
                    self.last_qr = None
                    authenticated_since = None
                    self._on_ready()

                consecutive_errors = 0

            except Exception as e:
                consecutive_errors += 1
                log.warning(f"Poll error #{consecutive_errors}: {e}")
                if consecutive_errors >= 5:
                    self._on_disconnected("POLL_ERROR")
                    break
                time.sleep(self.POLL_INTERVAL)

    def _detect_state(self) -> str:
        try:
            result = self._page.evaluate("""
                () => {
                    // PRIORITY 1: Check for chat list (ready state) - most reliable indicator
                    const chatList = document.querySelector('[data-testid="chat-list"]')
                                  || document.querySelector('#pane-side')
                                  || document.querySelector('[role="grid"][aria-label="Chat list"]')
                                  || document.querySelector('div[aria-label="Chat list"]')
                                  || document.querySelector('[data-testid="chatlist"]');
                    
                    if (chatList) {
                        return 'ready';
                    }

                    // PRIORITY 2: Check if main conversation panels exist (also means ready)
                    const conversationPanel = document.querySelector('[data-testid="conversation-panel-wrapper"]')
                                           || document.querySelector('#main');
                    const noChatSelected = document.querySelector('[data-testid="conversation-screen"]')
                                        || document.querySelector('[data-testid="default-content"]');
                    
                    if (conversationPanel && noChatSelected) {
                        return 'ready';
                    }

                    // PRIORITY 3: Check for loading/syncing screen (authenticated but not ready)
                    const loadingScreen = document.querySelector('[data-testid="startup"]')
                                       || document.querySelector('progress')
                                       || document.querySelector('.landing-wrapper')
                                       || (document.body && document.body.innerText.includes('Loading your chats'))
                                       || (document.body && document.body.innerText.includes('Syncing'));
                    
                    if (loadingScreen) {
                        return 'authenticated';
                    }

                    // PRIORITY 4: Check for intro/welcome screen
                    const introTitle = document.querySelector('._amid')
                                    || document.querySelector('[data-testid="intro-title"]')
                                    || document.querySelector('[data-testid="intro-md-bubble-heading"]');
                    
                    if (introTitle) {
                        return 'authenticated';
                    }

                    // PRIORITY 5: Check for QR code (only if visible on screen)
                    const qrEl = document.querySelector('canvas[aria-label="Scan me!"]')
                              || document.querySelector('div[data-ref]')
                              || document.querySelector('canvas');
                    
                    const isQRVisible = qrEl && qrEl.offsetParent !== null;
                    
                    if (isQRVisible) {
                        return 'qr';
                    }

                    // PRIORITY 6: Check if WhatsApp Web app is loaded at all
                    const appMain = document.querySelector('#app')
                                 || document.querySelector('[data-testid="wa-web"]');
                    
                    if (appMain) {
                        return 'loading';
                    }
                    
                    // Default: still loading
                    return 'loading';
                }
            """)
            if result == 'unsupported':
                log.error("WA reported browser as unsupported. Check stealth scripts.")
            
            if result != 'loading':
                log.info(f"Detected state: {result}")
            return result or "loading"
        except Exception as e:
            log.warning(f"State detection error: {e}")
            return "loading"

    def _extract_qr_text(self) -> Optional[str]:
        try:
            qr_text = self._page.evaluate("""
                () => {
                    const qrDiv = document.querySelector('[data-ref]');
                    if (qrDiv) {
                        const ref = qrDiv.getAttribute('data-ref');
                        if (ref && ref.length > 20) return ref;
                    }
                    return null;
                }
            """)
            return qr_text
        except Exception:
            return None

    def _qr_to_data_url(self, qr_text: str) -> Optional[str]:
        if qr_text and qr_text.startswith("data:"):
            return qr_text

        if QRCODE_AVAILABLE and qr_text:
            try:
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=4,
                )
                qr.add_data(qr_text)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = BytesIO()
                img.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode()
                return f"data:image/png;base64,{b64}"
            except Exception as e:
                log.error(f"QR generation error: {e}")

        return None

    def _cleanup(self):
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self.is_ready = False

    @staticmethod
    def _ext_from_mime(mimetype: str) -> str:
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "application/pdf": ".pdf",
        }
        return mapping.get(mimetype, ".bin")