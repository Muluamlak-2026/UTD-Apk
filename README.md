# Offline Archiver for Windows

A Windows desktop snapshot tool for web pages that the signed-in user is authorized to access and retain. It uses a visible Microsoft Edge session with a persistent local profile, a bounded same-host crawler, SQLite indexing, local asset storage, full-text search, retryable crawl queue, integrity verification, and portable ZIP export.

## Current capabilities
- Visible Edge sign-in session; sign in manually and complete MFA/CAPTCHA yourself.
- Persistent local browser profile so normal sessions may remain available between runs.
- Recursive same-host page discovery with configurable page cap, depth, and request delay.
- Captures page DOM snapshots and eligible GET-loaded images, fonts, audio/video, CSS, PDF and WASM resources (individual resource cap 30 MB).
- Scrolls pages to trigger common lazy-loaded content; captures src, srcset, poster and link references when the resource was observed.
- SQLite page index and FTS5 content search.
- Persistent crawl queue across restarts, pause/resume, stop, mark queued URLs as passed, restore passed items, retry failed items.
- Portable ZIP with an index page, local HTML snapshots and captured assets.
- SHA-256 integrity check for indexed page and asset files.

## Important limitations
This is a testable first release, not a certified full-site mirroring engine. A snapshot cannot guarantee offline operation of interactive applications, JavaScript APIs, POST-driven content, authenticated API calls, service workers, cross-origin embedded frames, streaming media, or content that only appears after a specific user interaction. CSS files are saved, but complex CSS-relative asset dependencies may need further rewriting. Infinite-scroll pages are bounded by the scroll limit. The current UI's pass action applies to queued URLs; the page table lists saved pages, not every pending queue item. Site-specific validation is required.

## Safety and permitted use
- Archive only content you are authorized to access and retain, and follow the website's terms and applicable law.
- The application does not bypass paywalls, DRM, MFA, CAPTCHA, authentication, or other access controls.
- Authentication is manual in the visible browser. Do not share the browser profile or archive ZIP; they may contain session state and sensitive medical content.
- Do not use this tool to redistribute copyrighted subscription content. Use it only where your subscription terms and applicable law permit local retention.

## Install and test
1. Download the Windows x64 installer from the GitHub Actions artifact named `OfflineArchiver-Windows-x64`.
2. Install and launch. Microsoft Edge must be installed on Windows.
3. Enter a site URL and select **Open sign-in browser**. Complete sign-in manually, then stop the browser task.
4. Enter an authorized topic URL and select **Start / Resume**.
5. Test pause/resume, stop, retry failed, restore passed, search, double-click offline page opening, integrity verification and portable ZIP export.
6. Extract the ZIP and open `index.html` while offline. Verify the pages and media you need.

## Build from source
- Windows 10/11, Python 3.11+
- `py -3.11 -m venv .venv`
- `.venv\\Scripts\\activate`
- `pip install -r requirements-dev.txt`
- `pip install -e .`
- `python -m playwright install msedge`
- `python -m offline_archiver`

GitHub Actions compiles, runs tests, packages the application and creates an Inno Setup installer on pushes to main and version tags. A successful CI build confirms compilation, automated tests and packaging; it does not replace real-world site testing.

## Data location
Default data folder: `%LOCALAPPDATA%\\OfflineArchiver`. The browser profile, cookies, database and archive content are stored there. Keep sufficient disk space and back up the library folder before major changes.
