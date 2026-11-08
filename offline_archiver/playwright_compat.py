"""Patch the pinned Playwright CDP default context for Qt WebEngine.

Qt WebEngine does not implement Browser.setDownloadBehavior. Playwright 1.48.0
supports the internal-browser-default acceptDownloads sentinel, which skips that
unsupported CDP call while leaving downloads under the embedded browser's control.
"""
from pathlib import Path


_NEEDLE = "const persistent = { noDefaultViewport: true };"
_PATCHED = 'const persistent = { noDefaultViewport: true, acceptDownloads: "internal-browser-default" };'


def ensure_cdp_download_compat(driver_file: Path | None = None) -> bool:
    if driver_file is None:
        import playwright
        driver_file = (
            Path(playwright.__file__).resolve().parent
            / "driver" / "package" / "lib" / "server" / "chromium" / "chromium.js"
        )
    driver_file = Path(driver_file)
    if not driver_file.is_file():
        raise RuntimeError(f"Playwright Chromium driver not found: {driver_file}")
    source = driver_file.read_text(encoding="utf-8")
    if _PATCHED in source:
        return False
    if _NEEDLE not in source:
        raise RuntimeError(
            "Playwright CDP patch target not found. The pinned Playwright version "
            "or packaged driver has changed; refusing to start the crawler."
        )
    driver_file.write_text(source.replace(_NEEDLE, _PATCHED, 1), encoding="utf-8")
    verified = driver_file.read_text(encoding="utf-8")
    if _PATCHED not in verified:
        raise RuntimeError("Playwright CDP compatibility patch verification failed.")
    return True
