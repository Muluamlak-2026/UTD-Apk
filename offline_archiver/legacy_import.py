from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup


def _canonical(url: str) -> str:
    url, _ = urldefrag((url or "").strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower()).geturl()


def _safe_source(root: Path, relative: str) -> Path | None:
    # Manifest paths are untrusted input: never allow traversal outside the archive.
    candidate = (root / str(relative).replace("\\", "/")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def import_legacy_archive(source_root: Path, library) -> dict:
    """Idempotently import an Offline Site Saver folder without moving or deleting it.

    Saved HTML is copied into the current SQLite library. Same-host links found
    in imported pages are inserted into the persistent pending queue so a later
    crawl can continue beyond the pages already present on disk.
    """
    source_root = Path(source_root).resolve()
    manifest_path = source_root / "archive-manifest.json"
    if not manifest_path.is_file():
        return {"status": "not_found", "imported": 0, "queued": 0, "skipped": 0,
                "message": "No archive-manifest.json found; legacy folder was left untouched."}

    raw = manifest_path.read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()
    marker = source_root / ".offlinearchiver-import.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        previous = {}
    if previous.get("manifest_sha256") == fingerprint:
        return {"status": "unchanged", "imported": 0, "queued": 0, "skipped": 0,
                "message": "Legacy archive already reconciled; no duplicate import needed."}

    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        return {"status": "invalid_manifest", "imported": 0, "queued": 0, "skipped": 0,
                "message": f"Could not read legacy manifest: {exc}"}

    records = manifest.get("pages", []) if isinstance(manifest, dict) else []
    if not isinstance(records, list):
        records = []
    imported = queued = skipped = 0
    errors = []
    start_hosts = set()

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        url = _canonical(record.get("url") or record.get("finalUrl") or "")
        rel = record.get("file")
        if not url or not isinstance(rel, str):
            skipped += 1
            continue
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host:
            start_hosts.add(host)
        source = _safe_source(source_root, rel)
        if source is None:
            skipped += 1
            errors.append(f"Missing or unsafe page path: {rel}")
            continue

        with library.lock:
            exists = library.db.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()
        if exists:
            skipped += 1
            continue

        try:
            html = source.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else (record.get("title") or url)
            text = soup.get_text(" ", strip=True)
            links = []
            for anchor in soup.select("a[href]"):
                target = _canonical(urljoin(url, anchor.get("href", "")))
                if target and (urlparse(target).hostname or "").lower().removeprefix("www.") == host:
                    links.append(target)
            library.save_page(
                url, record.get("finalUrl") or url, str(title)[:1000], html, text[:2_000_000],
                200, 0, list(dict.fromkeys(links)),
                completeness="legacy-imported",
                completeness_notes="Imported from the existing Offline Site Saver archive; original folder retained.",
            )
            imported += 1
            for target in dict.fromkeys(links):
                if library.enqueue(target, 1):
                    queued += 1
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    # Preserve the old folder and write the marker only after a complete pass.
    # If interrupted, the next launch safely repeats; already imported URLs are skipped.
    marker.write_text(json.dumps({
        "format": "OfflineArchiverLegacyImport/1",
        "manifest_sha256": fingerprint,
        "imported": imported,
        "queued": queued,
    }, indent=2), encoding="utf-8")
    return {
        "status": "imported" if imported or queued else "checked",
        "imported": imported, "queued": queued, "skipped": skipped,
        "errors": errors[:100],
        "message": f"Legacy archive reconciled: {imported} pages imported, {queued} internal links queued, {skipped} already present or skipped.",
    }
