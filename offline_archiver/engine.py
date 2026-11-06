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


MAX_RESOURCE_BYTES = 30 * 1024 * 1024
ASSET_TYPES = ("image/", "font/", "audio/", "video/")
ASSET_MIMES = {"text/css", "text/javascript", "application/javascript", "application/x-javascript", "application/pdf", "application/wasm"}


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
              delay: float = 5.0, include_external: bool = False, cookies=None):
        start_url = canonical(start_url)
        if not start_url:
            raise ValueError("Enter a valid HTTP or HTTPS URL.")
        seed_host = (urlparse(start_url).hostname or "").lower().removeprefix("www.")
        self.library.add_site(start_url)
        self.library.enqueue(start_url, 0)
        # Continue from the persisted queue after a normal stop or application restart.
        queue = deque((r["url"], r["depth"]) for r in self.library.pending_queue())
        done = 0
        async with async_playwright() as p:
            context = await self._launch(p)
            if cookies:
                try:
                    await context.add_cookies(cookies)
                    self.emit("status", "Imported " + str(len(cookies)) + " session cookies from the embedded browser profile.")
                except Exception as exc:
                    self.emit("status", "Some browser cookies could not be synchronized: " + str(exc))
            page = context.pages[0] if context.pages else await context.new_page()
            while queue and done < max_pages and not self.stop_event.is_set():
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                url, depth = queue.popleft()
                self.library.set_queue_state(url, "running")
                done += 1
                self.emit("progress", done, max_pages, url)
                resources = {}
                observed_responses = []
                def on_response(response):
                    # Keep callbacks lightweight; fetch response bodies later on this same async loop.
                    observed_responses.append(response)
                page.on("response", on_response)
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    status = response.status if response else 0
                    if status in (401, 403, 429):
                        if status == 429:
                            reason = "HTTP 429 rate limit: crawl paused safely. The remaining queue is preserved; wait for the provider's retry window, then use Start / Resume."
                        elif status == 401:
                            reason = "HTTP 401 authentication required: crawl paused safely. Sign in again in the embedded browser, then use Start / Resume."
                        else:
                            reason = "HTTP 403 access denied: crawl paused safely. Check the account's access/permissions in the embedded browser; no automatic retry was attempted. The remaining queue is preserved."
                        self.library.save_failure(url, reason, depth)
                        self.halt_reason = reason
                        self.emit("status", reason)
                        self.stop_event.set()
                        break
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                    except PlaywrightTimeoutError:
                        pass
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
                        if (max_depth <= 0 or depth < max_depth) and self.library.enqueue(target, depth + 1):
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
                except Exception as exc:
                    self.library.save_failure(url, str(exc), depth)
                    self.emit("failure", url, str(exc))
                finally:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                # Delay is interruptible so Stop and Pause respond promptly.
                remaining = max(1.0, min(delay, 120))
                while remaining > 0 and not self.stop_event.is_set():
                    await self._wait_if_paused()
                    interval = min(.2, remaining)
                    self.stop_event.wait(interval)
                    remaining -= interval
            await context.close()
        if self.halt_reason:
            self.emit("status", self.halt_reason + f" Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
        elif self.stop_event.is_set():
            self.emit("status", f"Stopped safely. Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
        else:
            self.emit("status", f"Run limit reached or queue empty. Processed {done} pages; {len(self.library.pending_queue())} pending pages remain available to resume.")
