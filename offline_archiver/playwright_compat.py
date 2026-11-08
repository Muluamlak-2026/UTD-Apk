"""Work around Qt WebEngine's unsupported Browser.setDownloadBehavior CDP command.

Playwright 1.48 initializes the default context created by connect_over_cdp() with
acceptDownloads='accept'. Qt WebEngine rejects the resulting
Browser.setDownloadBehavior command. Set the existing CDP persistent context to
Playwright's internal-browser-default policy before
validateBrowserContextOptions() runs. This leaves download handling with the
embedded browser and does not launch or authenticate a second browser.
"""
from pathlib import Path
import re


_POLICY = "internal-browser-default"
_OBJECT_PATCH = 'const persistent = { noDefaultViewport: true, acceptDownloads: "internal-browser-default" };'
_ASSIGNMENT_PATCH = 'persistent.acceptDownloads = "internal-browser-default";'


def ensure_cdp_download_compat(driver_file: Path | None = None) -> bool:
    """Patch the installed Playwright Chromium CDP bootstrap, idempotently.

    The primary anchor is the actual connectOverCDP persistent-context object.
    A fallback handles compiled layouts where object formatting differs but the
    validation call remains. Unknown layouts fail closed with a useful diagnostic.
    """
    if driver_file is None:
        import playwright
        driver_file = (
            Path(playwright.__file__).resolve().parent
            / "driver" / "package" / "lib" / "server" / "chromium" / "chromium.js"
        )

    driver_file = Path(driver_file)
    if not driver_file.is_file():
        raise RuntimeError(f"Playwright Chromium CDP driver not found: {driver_file}")

    source = driver_file.read_text(encoding="utf-8")
    if _ASSIGNMENT_PATCH in source or _OBJECT_PATCH in source:
        return False

    # Playwright v1.48 chromium.ts _connectOverCDPInternal() creates this
    # persistent context and validates it immediately afterwards.
    object_pattern = re.compile(
        r"const\s+persistent\s*=\s*\{\s*noDefaultViewport\s*:\s*true\s*\}\s*;"
    )
    patched, count = object_pattern.subn(_OBJECT_PATCH, source, count=1)
    if count == 0:
        # Inject before validation so the policy is set before Playwright applies
        # its normal Chromium default ('accept') to this CDP default context.
        validation_pattern = re.compile(
            r"(?m)^(?P<indent>\s*)validateBrowserContextOptions\(persistent,\s*browserOptions\);"
        )
        def inject(match):
            indent = match.group("indent")
            return f"{indent}{_ASSIGNMENT_PATCH}\n{match.group(0)}"
        patched, count = validation_pattern.subn(inject, source, count=1)

    if count != 1:
        raise RuntimeError(
            "Qt WebEngine CDP compatibility patch could not locate Playwright's "
            "connectOverCDP persistent-context initialization. No source was changed. "
            f"Driver: {driver_file}"
        )

    driver_file.write_text(patched, encoding="utf-8")
    verified = driver_file.read_text(encoding="utf-8")
    if _POLICY not in verified:
        raise RuntimeError("Qt WebEngine CDP compatibility patch verification failed.")
    return True
