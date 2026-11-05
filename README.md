# Offline Archiver for Windows

> **Canonical repository:** [Muluamlak-2026/UTD-Apk](https://github.com/Muluamlak-2026/UTD-Apk). All future source changes, Windows builds, installers, and release artifacts for this application must be made from `UTD-Apk`. `Site-Archive-Browser` is not the active build repository.

A Windows desktop offline website archiver with a persistent **embedded Chromium browser tab**, a manually controlled sign-in workflow, a conservative crawl worker, a six-state queue ledger, searchable local archive, integrity checks, diagnostics and portable export.

## Main application tabs

- **Browser & Sign-in:** embedded Chromium browser with its own persistent profile, address bar, back/forward/reload and manual sign-in. Cookies observed in this browser profile are synchronized to the capture worker for the selected site. No credentials are submitted by automation.
- **Dashboard:** downloaded, pending, failed, passed, redownloaded, incomplete/JS, attempted and asset counters; page/depth/request-delay settings; start/resume, pause and safe stop.
- **Queue & Six-list ledger:** downloaded pages remain in the library; pending, failed, passed and running URLs remain visible in the queue ledger. Add URLs, pass/skip with a reason, restore passed URLs, retry failures and force-redownload a selected URL.
- **Search & Offline library:** FTS5 full-text search, saved-page listing and opening local HTML snapshots in the built-in Offline Reader.
- **Diagnostics & Export:** SHA-256 integrity verification, diagnostic report, archive folder access and portable ZIP export. Page rows record a completeness classification and notes; incomplete captures can be re-queued.

## Browser and session behavior

The embedded browser uses a separate persistent profile under `%LOCALAPPDATA%\\OfflineArchiver\\embedded-browser`. It does not attach to or control your ordinary Edge profile. The capture worker receives a snapshot of matching cookies from the embedded browser; browser localStorage, sessionStorage, service-worker state and every site-specific token are not guaranteed to transfer. Sites relying on those mechanisms may require additional integration and may not archive completely.

The app does not add Chromium's `--no-sandbox` flag or disable the browser sandbox. It does not need to open a separate interactive Edge window for manual login. The crawl worker uses Playwright in a separate browser context so its page capture can be isolated from the visible browser tab; it receives the captured site cookies. The visible embedded browser remains the place to sign in and inspect pages.

## Account and request safety

- Only archive content you are authorized to access and retain; follow provider terms and applicable law.
- Login is manual. The app does not automate passwords, CAPTCHA, MFA or paywall bypass.
- Requests are sequential, with a default five-second delay and a bounded page/depth run.
- HTTP 401, 403 and 429 responses stop the current run and are not automatically retried. Review your session and contact the provider for account-access issues; do not repeatedly test a suspended credential.
- Never share the browser profile or archive ZIP; they may contain session cookies and sensitive content.

## Capture coverage and known limits

The crawler saves rendered HTML snapshots and observed GET-loaded image, font, audio/video, CSS, JavaScript, PDF and WASM resources within configured size limits. It discovers same-site links recursively (including subdomains of the seed host); link depth `0` means unlimited, while the per-run page cap remains active. It triggers bounded lazy loading and preserves current form field values and canvas-rendered charts as a snapshot. Pages are classified as complete or partial with notes for detected iframes, POST forms, fetch/XHR endpoints, external scripts and uncaptured remote assets. A saved canvas is a static image, not a live calculator. Client-side calculators may work if all dependencies are local, but server/API-backed calculators, cross-origin frames, service workers, streaming media, dynamic state and interactions are not guaranteed offline. A page being saved does not mean every control will work offline.

The completeness classifier is heuristic: it identifies common signs of missing dependencies but cannot prove that every control works offline. The incomplete-page retry action re-queues pages marked partial/incomplete. Do not treat this as certification of calculator functionality.

## Install and test

1. Download the latest successful Windows x64 installer from the GitHub Actions artifact `OfflineArchiver-Windows-x64`.
2. Install and launch on Windows 10/11.
3. Open **Browser & Sign-in**, navigate to the authorized site and sign in manually.
4. Return to **Dashboard**, enter the starting URL and use conservative settings for initial tests (one page at a time).
5. Inspect **Queue & Six-list ledger**, then verify pages in **Search & Offline library**.
6. Use **Diagnostics & Export** to verify the archive and create a portable ZIP.

## Build from source

- Windows 10/11, Python 3.11+
- `py -3.11 -m venv .venv`
- `.venv\\Scripts\\activate`
- `pip install -r requirements-dev.txt`
- `pip install -e .`
- `python -m playwright install msedge`
- `python -m offline_archiver`

The build packages the Qt WebEngine runtime for the embedded browser. This increases installer size compared with a QtWidgets-only build, but is required for the in-app Chromium tab. Do not remove QtWebEngine modules as a size optimization.

## Data location

Default data folder: `%LOCALAPPDATA%\\OfflineArchiver`. It contains the embedded browser profile, library database, page snapshots and assets. Back it up before major updates. Keep sufficient disk space; archive content size is independent of installer size.
