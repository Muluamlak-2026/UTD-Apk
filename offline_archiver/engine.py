from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .store import Library
from .playwright_compat import ensure_cdp_download_compat


MAX_RESOURCE_BYTES = 30 * 1024 * 1024
ASSET_TYPES = ("image/", "font/", "audio/", "video/")
ASSET_MIMES = {"text/css", "text/javascript", "application/javascript", "application/x-javascript", "application/pdf", "application/wasm"}

async def accept_cookie_consent(page):
    """Honor the user's explicit request to accept all cookies during capture.

    Only click clearly-labelled accept-all controls; never click reject, manage,
    or accept-selected controls. The browser context retains the resulting choice
    for subsequent pages in the same crawl.
    """
    script = r"""() => {
      const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const accept = /^(accept all cookies|accept all|allow all cookies|allow all)$/i;
      const controls = [...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')];
      const target = controls.find(el => visible(el) && accept.test((el.innerText || el.value || el.getAttribute('aria-label') || '').trim()));
      if (!target) return false;
      target.click();
      return true;
    }"""
    for _ in range(6):
        try:
            if await page.evaluate(script):
                await page.wait_for_timeout(350)
                return True
        except Exception:
            return False
        await page.wait_for_timeout(400)
    return False



def canonical(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path).geturl()


class Archiver:
    """Visible persistent Edge capture. Authentication remains entirely user-controlled."""

    def __init__(self, data_dir: Path, emit, stop_event: threading.Event, pause_event: threading.Event):
        self.data_dir = data_dir
        self.emit = emit
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.profile_dir = data_dir / "browser-profile"
        self.library = Library(data_dir / "library")
        self.halt_reason = None

    async def _wait_if_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            await asyncio.sleep(.2)

    async def _launch(self, p):
        return await p.chromium.launch_persistent_context(
            str(self.profile_dir), channel="msedge", headless=True, accept_downloads=True,
            viewport={"width": 1440, "height": 900}, service_workers="allow",
            args=["--disable-features=msEdgeSidebarV2"],
        )

    async def open_login(self, url: str):
        url = canonical(url)
        if not url:
            raise ValueError("Enter a valid HTTP or HTTPS URL.")
        async with async_playwright() as p:
            context = await self._launch(p)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.emit("status", "Sign in manually in Edge. MFA/CAPTCHA remain interactive. Stop when finished.")
            while not self.stop_event.wait(.3):
                pass
            await context.close()
        self.emit("status", "Browser profile saved locally.")

    async def crawl(self, start_url: str, max_pages: int = 100, max_depth: int = 3,
              delay: float = 5.0, include_external: bool = False, cookies=None,
              options_provider=None, cookies_provider=None):
        start_url = canonical(start_url)
        if not start_url:
            raise ValueError("Enter a valid HTTP or HTTPS URL.")
        seed_host = (urlparse(start_url).hostname or "").lower().removeprefix("www.")
        self.library.add_site(start_url)
        self.library.enqueue(start_url, 0)
        # Continue from the persisted queue after a normal stop or application restart.
        queue = deque((r["url"], r["depth"]) for r in self.library.pending_queue())
        done = 0

        def live_options():
            # Read a thread-safe snapshot supplied by the desktop UI. Changes apply
            # between pages without restarting the persisted crawl queue.
            try:
                current = options_provider() if options_provider else {}
            except Exception:
                current = {}
            return {
                "max_pages": max(1, int(current.get("max_pages", max_pages))),
                "max_depth": int(current.get("max_depth", max_depth)),
                "delay": max(0.2, min(120.0, float(current.get("delay", delay)))),
            }

        async def attach_visible_browser():
            # Qt WebEngine rejects Browser.setDownloadBehavior. Ensure Playwright's
            # CDP default context keeps the browser's native download policy.
            ensure_cdp_download_compat()
            # Attach Playwright to Qt WebEngine's existing Chromium process over
            # localhost CDP. This is the same visible page/profile the user signed
            # into; no headless browser, cloned cookies, or second login is used.
            connected_manager = await async_playwright().start()
            try:
                connected_browser = await connected_manager.chromium.connect_over_cdp(
                    "http://127.0.0.1:9222", timeout=15000
                )
                if not connected_browser.contexts:
                    raise RuntimeError("Visible browser has no active Chromium context.")
                connected_context = connected_browser.contexts[0]
                if not connected_context.pages:
                    raise RuntimeError("Visible browser has no active page. Open the site in Browser Sign-in first.")
                connected_page = next(
                    (candidate for candidate in connected_context.pages
                     if candidate.url.startswith(("http://", "https://"))),
                    connected_context.pages[0],
                )
                return connected_manager, connected_browser, connected_context, connected_page
            except Exception:
                await connected_manager.stop()
                raise

        manager, browser, context, page = await attach_visible_browser()
        try:
            self.emit("status", "Connected to the authenticated visible browser. Capturing in its existing session.")
            while queue and not self.stop_event.is_set():
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                settings = live_options()
                if done >= settings["max_pages"]:
                    break
                url, depth = queue.popleft()
                done += 1
                self.emit("progress", done, settings["max_pages"], url)
                for attempt in range(1, 4):
                    self.library.set_queue_state(url, "running")
                    resources = {}
                    observed_responses = []
                    def on_response(response):
                        # Keep callbacks lightweight; fetch response bodies later on this same async loop.
                        observed_responses.append(response)
                    page.on("response", on_response)
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        # User-requested consent handling: click only an explicit
                        # "Accept All" control before the snapshot is captured.
                        await accept_cookie_consent(page)
                        status = response.status if response else 0
                        if status == 403:
                            # A 403 for one URL is not proof that the user's embedded
                            # session has expired. Record this URL as inaccessible and
                            # continue the already-persisted queue; never try to bypass it.
                            reason = (
                                "HTTP 403 for this page; it was recorded in Failed and skipped. "
                                "The embedded browser session is not assumed to be logged out; "
                                "the remaining authorized queue will continue."
                            )
                            self.library.save_failure(url, reason, depth)
                            self.emit("status", reason)
                            break
                        if status in (401, 429):
                            if status == 429:
                                reason = "HTTP 429 rate limit: crawl paused safely. The remaining queue is preserved; wait for the provider's retry window, then use Start / Resume."
                            else:
                                reason = "HTTP 401 authentication required: crawl paused safely. Sign in again in the embedded browser, then use Start / Resume."
                            self.library.save_failure(url, reason, depth)
                            self.halt_reason = reason
                            self.emit("status", reason)
                            self.stop_event.set()
                            break
                        try:
                            await page.wait_for_load_state("networkidle", timeout=12000)
                        except PlaywrightTimeoutError:
                            pass
                        # Some consent managers render after the initial DOM/network settle.
                        await accept_cookie_consent(page)
                        # Trigger lazy-loaded images and infinite-scroll content, but bound the scroll work.
                        await page.evaluate("""async () => {
                          const step = Math.max(500, Math.floor(innerHeight * .75));
                          let previous = -1;
                          for (let i=0; i<80; i++) {
                            const height = document.documentElement.scrollHeight;
                            if (height === previous && i > 2) break;
                            previous = height;
                            for (let y=0; y<height; y+=step) {
                              window.scrollTo(0,y);
                              await new Promise(r=>setTimeout(r,120));
                            }
                            await new Promise(r=>setTimeout(r,250));
                          }
                          window.scrollTo(0,0);
                        }""")
                        await page.wait_for_timeout(500)
                        asset_bodies = {}
                        asset_capture_errors = 0
                        for asset_response in observed_responses:
                            try:
                                req = asset_response.request
                                if req.method != "GET" or asset_response.status >= 400:
                                    continue
                                mime = asset_response.headers.get("content-type", "").split(";")[0].lower()
                                if not (mime.startswith(ASSET_TYPES) or mime in ASSET_MIMES):
                                    continue
                                body = await asset_response.body()
                                if body and len(body) <= MAX_RESOURCE_BYTES:
                                    asset_bodies[canonical(asset_response.url)] = (body, mime)
                            except Exception:
                                # Chromium may discard response bodies after navigation; aggregate rather than flood the UI log.
                                asset_capture_errors += 1
                        for asset_url, (body, mime) in asset_bodies.items():
                            resources[asset_url] = self.library.save_asset(asset_url, body, mime)

                        # Crawler 2.2-style dependency localization: CSS is rewritten
                        # after the complete response set is known, so captured fonts,
                        # background images and imported stylesheets resolve offline.
                        css_url_pattern = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<url>[^)'"]+)(?P=q)\s*\)""", re.I)
                        import_pattern = re.compile(r"""@import\s+(['"])(.*?)\1""", re.I)
                        for asset_url, (body, mime) in asset_bodies.items():
                            if mime.split(";", 1)[0].lower() != "text/css":
                                continue
                            try:
                                css_text = body.decode("utf-8", "replace")

                                def rewrite_css_url(match):
                                    raw = match.group("url").strip()
                                    if raw.startswith(("data:", "blob:", "#")):
                                        return match.group(0)
                                    target = canonical(urljoin(asset_url, raw))
                                    local = resources.get(target)
                                    return f'url("{Path(local).name}")' if local else match.group(0)

                                def rewrite_css_import(match):
                                    target = canonical(urljoin(asset_url, match.group(2).strip()))
                                    local = resources.get(target)
                                    return f'@import "{Path(local).name}"' if local else match.group(0)

                                css_text = css_url_pattern.sub(rewrite_css_url, css_text)
                                css_text = import_pattern.sub(rewrite_css_import, css_text)
                                resources[asset_url] = self.library.save_asset(
                                    asset_url, css_text.encode("utf-8"), mime
                                )
                            except Exception:
                                # Keep the original CSS asset if a stylesheet cannot be rewritten.
                                pass

                        final_url = canonical(page.url) or url
                        live_state = await page.evaluate("""() => ({
                          fields: [...document.querySelectorAll('input,textarea,select')].map((e,i)=>({i,value:e.value,checked:!!e.checked,type:e.type,tag:e.tagName})),
                          canvases: [...document.querySelectorAll('canvas')].map((c,i)=>({i,data:(()=>{try{return c.toDataURL('image/png')}catch(e){return null}})()})),
                          frames: [...document.querySelectorAll('iframe')].map(f=>f.src || ''),
                          scripts: [...document.scripts].map(s=>s.src || ''),
                          forms: [...document.forms].map(f=>({action:f.action,method:f.method})),
                          api: performance.getEntriesByType('resource').filter(e=>e.initiatorType==='fetch'||e.initiatorType==='xmlhttprequest').map(e=>e.name)
                        })""")
                        html = await page.content()
                        soup = BeautifulSoup(html, "html.parser")
                        fields = soup.select("input,textarea,select")
                        for state in live_state.get("fields", []):
                            if state["i"] >= len(fields): continue
                            element = fields[state["i"]]
                            if state["tag"] == "INPUT" and state["type"] in ("checkbox","radio"):
                                if state["checked"]: element["checked"] = "checked"
                                else: element.attrs.pop("checked", None)
                            elif state["tag"] == "TEXTAREA":
                                element.clear()
                                element.append(state["value"] or "")
                            elif state["tag"] == "SELECT":
                                for option in element.select("option"):
                                    if option.get("value") == state["value"]: option["selected"] = "selected"
                                    else: option.attrs.pop("selected", None)
                            else:
                                element["value"] = state["value"] or ""
                        for state in live_state.get("canvases", []):
                            if state.get("data") and state["i"] < len(soup.select("canvas")):
                                image = soup.new_tag("img", src=state["data"], alt="Captured chart")
                                soup.select("canvas")[state["i"]].replace_with(image)
                        links = []
                        excluded = re.compile(r"/(logout|log-out|signout|sign-out|login|signin|register|password|oauth|authorize)(/|$)", re.I)
                        for a in soup.select("a[href]"):
                            raw = a.get("href", "")
                            target = canonical(urljoin(final_url, raw))
                            if not target: continue
                            parsed = urlparse(target)
                            target_host = (parsed.hostname or "").lower().removeprefix("www.")
                            same_site = target_host == seed_host or target_host.endswith("." + seed_host)
                            if not include_external and not same_site: continue
                            if excluded.search(parsed.path): continue
                            if parsed.path.lower().endswith((".pdf", ".zip", ".exe", ".dmg")): continue
                            current_depth = live_options()["max_depth"]
                            if (current_depth <= 0 or depth < current_depth) and self.library.enqueue(target, depth + 1):
                                queue.append((target, depth + 1))
                            a["href"] = target
                            links.append(target)
                        # Rewrite captured resources to local paths. Keep unknowns absolute so the
                        # archive never silently points to a fabricated local filename.
                        for el in soup.select("[src],link[href],source[src],video[poster],audio[src]"):
                            for attr in ("src", "href", "poster"):
                                value = el.get(attr)
                                if value:
                                    absolute = canonical(urljoin(final_url, value))
                                    if absolute in resources:
                                        el[attr] = resources[absolute]
                        for el in soup.select("[srcset]"):
                            rewritten = []
                            for item in el.get("srcset", "").split(","):
                                bits = item.strip().split()
                                if not bits:
                                    continue
                                absolute = canonical(urljoin(final_url, bits[0]))
                                bits[0] = resources.get(absolute, absolute)
                                rewritten.append(" ".join(bits))
                            el["srcset"] = ", ".join(rewritten)
                        # Add an archive provenance marker and preserve original URL.
                        if soup.html:
                            soup.html["data-archive-source"] = final_url
                        notes = []
                        if asset_capture_errors:
                            notes.append(str(asset_capture_errors) + " browser response body/bodies were unavailable during capture; related resources may be missing")
                        if live_state.get("frames"): notes.append(str(len(live_state["frames"])) + " iframe(s) may need separate capture")
                        if live_state.get("api"): notes.append(str(len(live_state["api"])) + " fetch/XHR endpoint(s) detected; server-dependent features need offline validation")
                        if any(form.get("method", "get").lower() != "get" for form in live_state.get("forms", [])):
                            notes.append("POST form/API behavior may require the original server")
                        external_scripts = [x for x in live_state.get("scripts", []) if x and (urlparse(x).hostname or "").lower().removeprefix("www.") != seed_host]
                        if external_scripts: notes.append(str(len(external_scripts)) + " external script(s) detected; verify dependencies")
                        missing_assets = [el.get("src") or el.get("href","") for el in soup.select("script[src],img[src],link[rel=stylesheet][href]") if (el.get("src") or el.get("href","")).startswith(("http://","https://"))]
                        if missing_assets: notes.append(str(len(missing_assets)) + " remote resources remain uncaptured")
                        completeness = "complete" if not notes else "partial"
                        saved = self.library.save_page(
                            url=final_url, requested_url=url, title=soup.title.get_text(" ", strip=True)
                            if soup.title else final_url, html=str(soup),
                            text=soup.get_text(" ", strip=True),
                            status=response.status if response else 0, depth=depth, links=links,
                            completeness=completeness, completeness_notes="; ".join(notes),
                        )
                        self.library.set_queue_state(url, "saved")
                        self.emit("page", saved, soup.title.get_text(" ", strip=True) if soup.title else final_url, final_url)
                        self.emit("status", f"Saved {done} pages in this run; {len(queue)} queued.")
                        break
                    except Exception as exc:
                        error_text = str(exc)
                        lower_error = error_text.lower()
                        driver_lost = any(marker in lower_error for marker in (
                            "connection closed while reading from the driver",
                            "browser has been closed",
                            "target page, context or browser has been closed",
                            "playwright connection closed",
                            "transport is closed",
                            "browser has been closed",
                        ))
                        if attempt < 3:
                            self.library.set_queue_state(
                                url, "running", error_text[:2000], increment=True
                            )
                            self.emit("status", f"Page attempt {attempt}/3 failed; preparing automatic retry: {error_text[:180]}")
                            if driver_lost:
                                # A disconnected Playwright transport cannot be reused.
                                # Recreate both the Playwright manager and browser context.
                                try:
                                    try:
                                        await manager.stop()
                                    except Exception:
                                        pass
                                    manager, browser, context, page = await attach_visible_browser()
                                    self.emit("status", "Reattached to the visible browser; resuming the interrupted page.")
                                except Exception as reconnect_error:
                                    self.library.set_queue_state(
                                        url, "pending",
                                        "Automatic reconnect failed; page preserved: " + str(reconnect_error)[:1500]
                                    )
                                    self.halt_reason = "Browser reconnect failed; current page and queue were preserved for Start / Resume."
                                    self.emit("status", self.halt_reason)
                                    self.stop_event.set()
                                    break
                            else:
                                await asyncio.sleep(min(8, 1.5 * (2 ** (attempt - 1))))
                            continue
                        self.library.save_failure(url, error_text, depth)
                        self.emit("failure", url, f"Failed after 3 attempts: {error_text}")
                    finally:
                        try:
                            page.remove_listener("response", on_response)
                        except Exception:
                            pass
                # Delay is interruptible so Stop and Pause respond promptly.
                elapsed = 0.0
                while not self.stop_event.is_set():
                    await self._wait_if_paused()
                    current_delay = live_options()["delay"]
                    if elapsed >= current_delay:
                        break
                    interval = min(.2, current_delay - elapsed)
                    self.stop_event.wait(interval)
                    elapsed += interval
        finally:
            try:
                await manager.stop()
            except Exception:
                pass
        if self.halt_reason:
            self.emit("status", self.halt_reason + f" Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
        elif self.stop_event.is_set():
            self.emit("status", f"Stopped safely. Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
        else:
            self.emit("status", f"Run limit reached or queue empty. Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
