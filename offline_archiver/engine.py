from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .store import Library


def canonical(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path).geturl()


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)[:90].strip("._")
    return value or "resource"


class Archiver:
    """Visible, persistent Edge capture engine. No credential or access-control bypass."""

    def __init__(self, data_dir: Path, emit, stop_event: threading.Event, pause_event: threading.Event):
        self.data_dir = data_dir
        self.emit = emit
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.profile_dir = data_dir / "browser-profile"
        self.library = Library(data_dir / "library")
        self._lock = threading.Lock()

    def _wait_if_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(.2)

    def open_login(self, url: str):
        """Open a visible Edge session; user signs in manually and stops this task to save profile."""
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                str(self.profile_dir), channel="msedge", headless=False,
                accept_downloads=True, viewport={"width": 1440, "height": 900},
                args=["--disable-features=msEdgeSidebarV2"]
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.emit("status", "Browser open. Sign in manually; MFA/CAPTCHA remain interactive. Stop this task when finished.")
            while not self.stop_event.wait(.3):
                pass
            browser.close()
        self.emit("status", "Browser profile saved locally.")

    def crawl(self, start_url: str, max_pages: int = 100, max_depth: int = 3,
              delay: float = 1.5, include_external: bool = False):
        start_url = canonical(start_url)
        if not start_url:
            raise ValueError("Enter a valid http or https URL.")
        host = urlparse(start_url).netloc
        queue = deque([(start_url, 0)])
        queued = {start_url}
        visited = set()
        self.library.add_site(start_url)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir), channel="msedge", headless=False,
                accept_downloads=True, viewport={"width": 1440, "height": 900},
                service_workers="allow"
            )
            page = context.pages[0] if context.pages else context.new_page()
            while queue and len(visited) < max_pages and not self.stop_event.is_set():
                self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                url, depth = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                self.emit("progress", len(visited), max_pages, url)
                resources = {}
                def on_response(response):
                    try:
                        req = response.request
                        if req.method != "GET" or response.status >= 400:
                            return
                        ctype = response.headers.get("content-type", "").split(";")[0].lower()
                        if not (ctype.startswith("image/") or ctype in ("text/css", "font/woff", "font/woff2", "application/font-woff")):
                            return
                        body = response.body()
                        if not body or len(body) > 25 * 1024 * 1024:
                            return
                        rel = self.library.save_asset(response.url, body, ctype)
                        resources[response.url] = rel
                    except Exception:
                        return
                page.on("response", on_response)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    page.evaluate("""async () => {
                      for (let y=0; y<document.body.scrollHeight; y+=700) {
                        window.scrollTo(0,y); await new Promise(r=>setTimeout(r,120));
                      }
                      window.scrollTo(0,0);
                    }""")
                    page.wait_for_timeout(700)
                    final_url = canonical(page.url) or url
                    title = page.title() or final_url
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    links = []
                    for a in soup.select("a[href]"):
                        target = canonical(urljoin(final_url, a.get("href", "")))
                        if not target:
                            continue
                        if not include_external and urlparse(target).netloc != host:
                            continue
                        if depth < max_depth and target not in queued and len(queued) < max_pages * 5:
                            queued.add(target)
                            links.append(target)
                            queue.append((target, depth + 1))
                    # Keep absolute source URLs in HTML: browser offline replay can resolve them through saved asset aliases.
                    for el in soup.select("[src], link[ href ]"):
                        attr = "src" if el.has_attr("src") else "href"
                        original = canonical(urljoin(final_url, el.get(attr, "")))
                        if original in resources:
                            el[attr] = resources[original]
                    for el in soup.select("a[href]"):
                        el["href"] = canonical(urljoin(final_url, el.get("href", ""))) or el.get("href")
                    saved = self.library.save_page(
                        url=final_url, requested_url=url, title=title,
                        html=str(soup), text=soup.get_text(" ", strip=True),
                        status=response.status if response else 0, depth=depth,
                        links=links
                    )
                    self.emit("page", saved, title, final_url)
                    self.emit("status", f"Saved {len(visited)} pages; {len(queue)} queued.")
                except Exception as exc:
                    self.library.save_failure(url, str(exc), depth)
                    self.emit("failure", url, str(exc))
                finally:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                time.sleep(max(0.2, min(delay, 30)))
            context.close()
        self.emit("status", f"Crawl stopped. Visited {len(visited)} pages; {len(queue)} remain queued.")
