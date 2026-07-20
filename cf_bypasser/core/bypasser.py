import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from camoufox.async_api import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

from cf_bypasser.utils.misc import md5_hash, get_browser_init_lock
from cf_bypasser.cache.cookie_cache import CookieCache
from cf_bypasser.utils.config import BrowserConfig, OPERATING_SYSTEMS

# Get addon path for Camoufox init script workaround
ADDON_PATH = get_addon_path()

# Project root (parent of the cf_bypasser package) and directory used to persist
# failed-attempt artifacts (HTML + screenshot) when SAVE_FAILED_INFO is enabled.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FAILED_INFO_DIR = os.path.join(PROJECT_ROOT, "failed_info")


class CamoufoxBypasser:
    """Camoufox bypasser with cookie caching and direct proxy support."""
    
    def __init__(self, max_retries: int = 5, log: bool = True, cache_file: str = "cf_cookie_cache.json"):
        self.max_retries = max_retries
        self.log = log
        self.cookie_cache = CookieCache(cache_file)

    def log_message(self, message: str) -> None:
        """Log message if logging is enabled."""
        if self.log:
            logging.info(message)

    async def dump_page_state(self, page, reason: str) -> None:
        """Log the current page state (title, url, full HTML) for debugging failures.

        Always logs regardless of self.log so failures are never silent.
        """
        try:
            url = None
            try:
                url = page.url
            except Exception:
                url = "<unknown>"

            title = None
            try:
                title = await page.title()
            except Exception as e:
                title = f"<error getting title: {e}>"

            html_content = None
            try:
                html_content = await page.content()
            except Exception as e:
                html_content = f"<error getting content: {e}>"

            content_length = len(html_content) if isinstance(html_content, str) else 0

            # List iframes present on the page — the click solver fails with
            # "Cloudflare iframes not found", so this is often the key signal.
            frame_info = "<unavailable>"
            try:
                frame_urls = []
                for frame in page.frames:
                    try:
                        frame_urls.append(frame.url)
                    except Exception:
                        frame_urls.append("<error getting frame url>")
                frame_info = f"{len(frame_urls)} frame(s): {frame_urls}"
            except Exception as e:
                frame_info = f"<error listing frames: {e}>"

            logging.error(
                "Page state dump (%s):\n"
                "  url: %s\n"
                "  title: %s\n"
                "  content length: %d chars\n"
                "  frames: %s\n"
                "  ---- full HTML start ----\n%s\n  ---- full HTML end ----",
                reason, url, title, content_length, frame_info, html_content,
            )

            # Optionally persist the HTML + a screenshot to disk for later inspection.
            if os.environ.get("SAVE_FAILED_INFO") == "true":
                await self.save_failed_info(page, url, html_content)
        except Exception as e:
            logging.error(f"Failed to dump page state ({reason}): {e}")

    async def save_failed_info(self, page, url: Optional[str], html_content: Optional[str]) -> None:
        """Persist the failed page's HTML and a screenshot under failed_info/.

        Folder is named "<YYYYmmddHHMMSSfff>_<sanitized_hostname>"; on name
        collision a numeric suffix (_1, _2, ...) is appended.
        """
        try:
            # Derive hostname from the target/page URL.
            hostname = ""
            try:
                hostname = urlparse(url or "").netloc or urlparse(page.url).netloc
            except Exception:
                hostname = ""
            # Replace every special char (including ".") with an underscore.
            safe_hostname = re.sub(r"[^0-9A-Za-z]", "_", hostname) or "unknown"

            # Timestamp with millisecond precision, e.g. 20260720144350123.
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S") + f"{datetime.now().microsecond // 1000:03d}"

            base_name = f"{timestamp}_{safe_hostname}"
            target_dir = os.path.join(FAILED_INFO_DIR, base_name)
            suffix = 1
            while os.path.exists(target_dir):
                target_dir = os.path.join(FAILED_INFO_DIR, f"{base_name}_{suffix}")
                suffix += 1
            os.makedirs(target_dir, exist_ok=True)

            # Write the HTML.
            try:
                html_path = os.path.join(target_dir, "failed.html")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content if isinstance(html_content, str) else "")
            except Exception as e:
                logging.error(f"Failed to write failed.html: {e}")

            # Write a full-page PNG screenshot (Playwright/Camoufox supports png & jpeg).
            try:
                screenshot_path = os.path.join(target_dir, "failed.png")
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception as e:
                logging.error(f"Failed to capture screenshot: {e}")

            logging.error(f"Saved failed info to folder: {os.path.basename(target_dir)}")
        except Exception as e:
            logging.error(f"Failed to save failed info: {e}")

    def parse_proxy(self, proxy: str) -> Optional[Dict[str, str]]:
        """Parse proxy URL and return proxy configuration."""
        try:
            parsed = urlparse(proxy)
            if not parsed.hostname or not parsed.port:
                self.log_message(f"Invalid proxy format: {proxy}")
                return None
            
            proxy_config = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            }
            
            if parsed.username and parsed.password:
                proxy_config["username"] = parsed.username
                proxy_config["password"] = parsed.password
            
            return proxy_config
        except Exception as e:
            self.log_message(f"Error parsing proxy {proxy}: {e}")
            return None

    async def setup_browser(self, proxy: Optional[str] = None, lang: str = "en-US", user_agent: Optional[str] = None) -> tuple:
        """Setup Camoufox browser with random OS and configuration. Returns (browser, context, page)."""
        # Clear expired cache entries
        self.cookie_cache.clear_expired()

        # Determine OS from user_agent if provided, otherwise random
        selected_os = None
        if user_agent:
            ua_lower = user_agent.lower()
            if "windows" in ua_lower:
                selected_os = "windows"
            elif "macintosh" in ua_lower or "mac os" in ua_lower:
                selected_os = "macos"
            elif "linux" in ua_lower or "x11" in ua_lower:
                selected_os = "linux"
        
        if not selected_os:
            selected_os = random.choice(OPERATING_SYSTEMS)
            
        self.log_message(f"Using OS: {selected_os}")
        
        # Generate random config for the selected OS
        random_config = BrowserConfig.generate_random_config(selected_os, lang=lang)
        
        # Override user agent if provided
        if user_agent:
            random_config['navigator.userAgent'] = user_agent
            self.log_message(f"Using provided User-Agent: {user_agent}")
        else:
            self.log_message(f"Generated config with UA: {random_config.get('navigator.userAgent', 'N/A')}")
            
        self.log_message(f"Screen resolution: {random_config['window.outerWidth']}x{random_config['window.outerHeight']}")

        # Setup proxy configuration if provided
        proxy_config = None
        if proxy:
            proxy_config = self.parse_proxy(proxy)
            if proxy_config:
                self.log_message(f"Using proxy: {proxy_config['server']}")
            else:
                self.log_message("Failed to parse proxy, continuing without proxy")

        # Use global lock to serialize browser initialization (browserforge is not thread-safe)
        async with get_browser_init_lock():
            camoufox = AsyncCamoufox(
                headless=True,
                geoip=True if proxy else False,
                humanize=False,
                os=selected_os,
                locale=lang if lang else "en-US",
                i_know_what_im_doing=True,
                config={'forceScopeAccess': True, **random_config},
                disable_coop=True,
                main_world_eval=True,
                addons=[os.path.abspath(ADDON_PATH)],
                block_images=False,
                block_webrtc=True,
                enable_cache=False,
            )
            browser = await camoufox.__aenter__()

        # Create context with proxy if provided
        context_options = {}
        if proxy_config:
            context_options["proxy"] = proxy_config

        context = await browser.new_context(**context_options)
        page = await context.new_page()
        
        return camoufox, browser, context, page

    async def is_bypassed(self, page) -> bool:
        """Check if Cloudflare challenge has been bypassed."""
        try:
            title = await page.title()
            if "just a moment" in title.lower():
                return False
            html_content = await page.content()
            if "please complete the captcha" in html_content.lower():
                return False
            return True
        except Exception as e:
            self.log_message(f"Error checking bypass status: {e}")
            return False
    
    async def determine_challenge_type(self, page) -> CaptchaType:
        """Determine the type of Cloudflare challenge present."""
        try:
            html_content = await page.content()
            title = await page.title()
            if "please complete the captcha" in html_content.lower():
                return CaptchaType.CLOUDFLARE_TURNSTILE
            elif "just a moment" in title.lower():
                return CaptchaType.CLOUDFLARE_INTERSTITIAL
            else:
                return None
        except Exception as e:
            self.log_message(f"Error determining challenge type: {e}")
            return None

    async def solve_cloudflare_challenge(self, url: str, page) -> bool:
        """Navigate to URL and solve Cloudflare challenge using playwright-captcha."""
        try:
            # Navigate to the target URL
            self.log_message(f"Navigating to {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as nav_err:
                self.log_message(f"Navigation warning: {nav_err}")
            # Wait for challenge scripts to load and execute
            await asyncio.sleep(5)
            try:
                nav_title = await page.title()
                self.log_message(f"Page loaded — title: {nav_title!r}, url: {page.url}")
            except Exception as e:
                self.log_message(f"Could not read page title after navigation: {e}")
            try:
                html_content = await page.content()
            except Exception:
                html_content = ""

            if "cloudflare" not in html_content.lower():
                self.log_message("No Cloudflare protection detected on the page -- either not protected or already bypassed")
                return True

            # Check if we need to solve a challenge
            if await self.is_bypassed(page):
                self.log_message("No Cloudflare challenge detected or already bypassed")
                return True

            self.log_message("Cloudflare challenge detected. Attempting to solve...")
            challenge_type = await self.determine_challenge_type(page)
            if not challenge_type:
                self.log_message("Could not determine challenge type")
                await self.dump_page_state(page, "could not determine challenge type")
                return False

            # Use ClickSolver to find and click the Cloudflare checkbox.
            # Don't pass expected_content_selector — it causes false negatives
            # when the target page doesn't have a matching element.
            captcha_container = page
            try:
                async with ClickSolver(framework=FrameworkType.CAMOUFOX, page=page, max_attempts=3, attempt_delay=3) as solver:
                    await solver.solve_captcha(
                        captcha_container=captcha_container,
                        captcha_type=challenge_type)
                    if await self.is_bypassed(page):
                        self.log_message("Cloudflare challenge solved successfully!")
                        return True
            except Exception as e:
                self.log_message(f"Click solver reported: {e}")
                # Dump the page state right when the solver fails — this captures
                # what the page actually looked like (e.g. why Cloudflare iframes
                # were not found) before further navigation changes it.
                await self.dump_page_state(page, f"click solver failed: {e}")

            # The click solver's internal verification can be too hasty —
            # the checkbox click may have worked but the page needs more
            # time to navigate past the challenge. Poll for resolution.
            self.log_message("Waiting for page to resolve after challenge interaction...")
            for i in range(self.max_retries):
                await asyncio.sleep(3)
                try:
                    if await self.is_bypassed(page):
                        self.log_message("Cloudflare challenge resolved after waiting")
                        return True
                except Exception:
                    # Page may be mid-navigation — keep polling
                    pass

            self.log_message("Failed to solve Cloudflare challenge")
            await self.dump_page_state(page, "failed to solve Cloudflare challenge after all retries")
            return False

        except Exception as e:
            self.log_message(f"Error solving Cloudflare challenge: {e}")
            await self.dump_page_state(page, f"exception while solving challenge: {e}")
            return False

    async def get_cookies_and_user_agent(self, context, page) -> Dict[str, Any]:
        """Get cookies and user agent after successful bypass."""
        try:
            cookies = await context.cookies()
            cookie_dict = {}
            for cookie in cookies:
                cookie_dict[cookie['name']] = cookie['value']
            
            # Get user agent from the page
            user_agent = await page.evaluate("navigator.userAgent")
            
            return {
                "cookies": cookie_dict,
                "user_agent": user_agent
            }
        except Exception as e:
            self.log_message(f"Error getting cookies and user agent: {e}")
            return None

    async def get_html_content_and_cookies(self, context, page) -> Dict[str, Any]:
        """Get HTML content, cookies, and user agent after successful bypass."""
        try:
            cookies = await context.cookies()
            cookie_dict = {}
            for cookie in cookies:
                cookie_dict[cookie['name']] = cookie['value']
            
            # Get user agent from the page
            user_agent = await page.evaluate("navigator.userAgent")
            
            # Get HTML content
            html_content = await page.content()
            
            # Get final URL (in case of redirects)
            final_url = page.url
            
            return {
                "cookies": cookie_dict,
                "user_agent": user_agent,
                "html": html_content,
                "url": final_url,
                "status_code": 200  # Assuming success if we got here
            }
        except Exception as e:
            self.log_message(f"Error getting HTML content and cookies: {e}")
            return None

    async def get_or_generate_cookies(self, url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get cached cookies or generate new ones."""
        hostname = urlparse(url).netloc
        cache_key = md5_hash(hostname + (proxy or ""))
        
        # Try to get cached cookies first
        cached = self.cookie_cache.get(cache_key)
        if cached:
            return {
                "cookies": cached.cookies,
                "user_agent": cached.user_agent
            }
        
        self.log_message(f"No cached cookies for {cache_key}, generating new ones...")
        
        # Create isolated browser instance
        camoufox = None
        browser = None
        context = None
        page = None
        
        try:
            # Setup browser and solve challenge
            camoufox, browser, context, page = await self.setup_browser(proxy)
            
            if await self.solve_cloudflare_challenge(url, page):
                data = await self.get_cookies_and_user_agent(context, page)
                if data:
                    # Cache the new cookies
                    self.cookie_cache.set(cache_key, data["cookies"], data["user_agent"])
                    return data
            
            return None
            
        except Exception as e:
            self.log_message(f"Error in get_or_generate_cookies: {e}")
            return None
        finally:
            await self.cleanup_browser(camoufox, browser, context, page)

    async def get_or_generate_html(self, url: str, proxy: Optional[str] = None, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """Get HTML content along with cookies (cached or fresh)."""
        hostname = urlparse(url).netloc
        cache_key = md5_hash(hostname + (proxy or ""))
        
        # For HTML endpoint, we need to setup browser and get fresh content
        # even if we have cached cookies, as HTML content may change
        self.log_message(f"Getting HTML content for {url}...")
        
        cached_cookies = None
        cached_ua = None
        
        if not bypass_cache:
            cached = self.cookie_cache.get(cache_key)
            if cached:
                cached_cookies = cached.cookies
                cached_ua = cached.user_agent
                self.log_message(f"Found cached cookies for {url}")

        # Create isolated browser instance
        camoufox = None
        browser = None
        context = None
        page = None
        
        try:
            # Setup browser and solve challenge
            camoufox, browser, context, page = await self.setup_browser(proxy, user_agent=cached_ua)
            
            if cached_cookies:
                self.log_message("Restoring cached cookies...")
                # Convert dict to list of cookie objects
                cookie_list = []
                for name, value in cached_cookies.items():
                    cookie_list.append({
                        'name': name,
                        'value': value,
                        'url': url  # Use the target URL for the cookie
                    })
                await context.add_cookies(cookie_list)
            
            if await self.solve_cloudflare_challenge(url, page):
                data = await self.get_html_content_and_cookies(context, page)
                if data:
                    # Cache the cookies for future use
                    self.cookie_cache.set(cache_key, data["cookies"], data["user_agent"])
                    return data
            
            return None
            
        except Exception as e:
            self.log_message(f"Error in get_or_generate_html: {e}")
            return None
        finally:
            await self.cleanup_browser(camoufox, browser, context, page)

    async def cleanup_browser(self, camoufox, browser, context, page) -> None:
        """Clean up browser resources."""
        try:
            # Close page first
            if page:
                try:
                    await page.close()
                except Exception as e:
                    self.log_message(f"Error closing page: {e}")
                
            # Close context second
            if context:
                try:
                    await context.close()
                except Exception as e:
                    self.log_message(f"Error closing context: {e}")
                
            # Close the AsyncCamoufox wrapper
            if camoufox:
                try:
                    await camoufox.__aexit__(None, None, None)
                except Exception as e:
                    self.log_message(f"Error closing camoufox: {e}")
                    
        except Exception as e:
            self.log_message(f"Error during cleanup: {e}")

    async def cleanup(self) -> None:
        """Backward compatibility method - no longer stores browser instances."""
        pass
