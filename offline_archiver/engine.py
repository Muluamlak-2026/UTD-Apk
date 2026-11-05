from __future__ import annotations

import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .store import Library


MAX_RESOURCE_BYTES = 30 * 1024 * 1024
ASSET_TYPES = ("image/", "font/", "audio/", "video/")
ASSET_MIMES = {"text/css", "application/pdf", "application/wasm", "text/javascript", "application/javascript", "application/x-javascript", "text/ecmascript", "application/ecmascript"}


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

    def _wait_if_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(.2)

    def _launch(self, p):
        return p.chromium.launch_persistent_context(
            str(self.profile_dir), channel="msedge", headless=False, accept_downloads=True,
            viewport={"width": 1440, "height": 900}, service_workers="allow",
            args=["--disable-features=msEdgeSidebarV2"],
        )

    def open_login(self, url: str):
        url = canonical(url)
        if not url:
            raise ValueError("Enter a valid HTTP or HTTPS URL.")
        with sync_playwright() as p:
            context = self._launch(p)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.emit("status", "Sign in manually in Edge. MFA/CAPTCHA remain interactive. Stop when finished.")
            while not self.stop_event.wait(.3):
                pass
            context.close()
        self.emit("status", "Browser profile saved locally.")

    def crawl(self, start_url: str, max_pages: int = 100, max_depth: int = 3,
              delay: float = 1.5, include_external: bool = False):
        start_url = canonical(start_url)
        if not start_url:
            raise ValueError("Enter a valid HTTP or HTTPS URL.")
        host = urlparse(start_url).netloc
        self.library.add_site(start_url)
        self.library.enqueue(start_url, 0)
        # Continue from the persisted queue after a normal stop or application restart.
        queue = deque((r["url"], r["depth"]) for r in self.library.pending_queue())
        done = 0
        with sync_playwright() as p:
            context = self._launch(p)
            page = context.pages[0] if context.pages else context.new_page()
            while queue and done < max_pages and not self.stop_event.is_set():
                self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                url, depth = queue.popleft()
                self.library.set_queue_state(url, "running")
                done += 1
                self.emit("progress", done, max_pages, url)
                resources = {}
                asset_issues = []
                def on_response(response):
                    try:
                        req = response.request
                        if req.method != "GET":
                            return
                        if response.status >= 400:
                            if req.resource_type in ("script", "stylesheet", "image", "font", "media"):
                                asset_issues.append({"url": response.url, "type": req.resource_type,
                                                     "status": response.status, "error": "HTTP response error"})
                            return
                        mime = response.headers.get("content-type", "").split(";")[0].lower()
                        if not (mime.startswith(ASSET_TYPES) or mime in ASSET_MIMES):
                            return
                        body = response.body()
                        if not body or len(body) > MAX_RESOURCE_BYTES:
                            return
                        resources[canonical(response.url)] = self.library.save_asset(response.url, body, mime)
                    except Exception:
                        pass
                def on_request_failed(request):
                    try:
                        if request.resource_type in ("script", "stylesheet", "image", "font", "media"):
                            asset_issues.append({"url": request.url, "type": request.resource_type,
                                                 "status": 0, "error": request.failure or "Request failed"})
                    except Exception:
                        pass
                page.on("response", on_response)
                page.on("requestfailed", on_request_failed)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12000)
                    except PlaywrightTimeoutError:
                        pass
                    # Trigger lazy-loaded images and infinite-scroll content, but bound the scroll work.
                    page.evaluate("""async () => {
                      const step = Math.max(500, Math.floor(innerHeight * .75));
                      let previous = -1;
                      for (let i=0; i<80; i++) {
                        const height = document.documentElement.scrollHeight;
                        if (height === previous && i > 2) break;
                        previous = height;
                        for (let y=0; y<height; y+=step) {
                          window.scrollTo(0,y);
                          await new Promise(r=>setTimeout(r,90));
                        }
                        await new Promise(r=>setTimeout(r,250));
                      }
                      window.scrollTo(0,0);
                    }""")
                    page.wait_for_timeout(500)
                    final_url = canonical(page.url) or url
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    links = []
                    for a in soup.select("a[href]"):
                        raw = a.get("href", "")
                        target = canonical(urljoin(final_url, raw))
                        if not target:
                            continue
                        if not include_external and urlparse(target).netloc != host:
                            continue
                        if depth < max_depth and self.library.enqueue(target, depth + 1):
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
                    # Record missing/broken browser-loaded dependencies instead of silently
                    # declaring a page fully archived. This is especially important for calculators.
                    for issue in asset_issues:
                        self.library.save_asset_issue(final_url, issue["url"], issue["type"],
                                                      issue["error"], issue["status"])
                    saved = self.library.save_page(
                        url=final_url, requested_url=url, title=soup.title.get_text(" ", strip=True)
                        if soup.title else final_url, html=str(soup),
                        text=soup.get_text(" ", strip=True),
                        status=response.status if response else 0, depth=depth, links=links,
                        incomplete=bool(asset_issues),
                    )
                    self.library.set_queue_state(url, "saved")
                    self.emit("page", saved, soup.title.get_text(" ", strip=True) if soup.title else final_url, final_url)
                    self.emit("status", f"Saved {done} pages in this run; {len(queue)} queued.")
                except Exception as exc:
                    self.library.save_failure(url, str(exc), depth)
                    self.emit("failure", url, str(exc))
                finally:
                    try:
                        page.remove_listener("response", on_response)
                        page.remove_listener("requestfailed", on_request_failed)
                    except Exception:
                        pass
                # Delay is interruptible so Stop and Pause respond promptly.
                remaining = max(.2, min(delay, 30))
                while remaining > 0 and not self.stop_event.is_set():
                    self._wait_if_paused()
                    interval = min(.2, remaining)
                    self.stop_event.wait(interval)
                    remaining -= interval
            context.close()
        self.emit("status", f"Run ended. Processed {done} pages; {len(queue)} remain queued for resume.")
