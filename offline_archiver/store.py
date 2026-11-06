from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin, urldefrag
import posixpath
import re

from bs4 import BeautifulSoup


class Library:
    """Crash-tolerant local archive index and persistent crawl queue."""

    def __init__(self, root: Path):
        self.root = root
        self.pages_dir = root / "pages"
        self.assets_dir = root / "assets"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "library.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sites(id INTEGER PRIMARY KEY, origin TEXT UNIQUE, created TEXT);
                CREATE TABLE IF NOT EXISTS pages(
                    id INTEGER PRIMARY KEY, url TEXT UNIQUE, requested_url TEXT, title TEXT,
                    html_path TEXT, text TEXT, status INTEGER, depth INTEGER, state TEXT,
                    error TEXT, saved_at TEXT, sha256 TEXT, completeness TEXT DEFAULT 'unknown', completeness_notes TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS links(source_id INTEGER, target_url TEXT);
                CREATE TABLE IF NOT EXISTS assets(url TEXT PRIMARY KEY, path TEXT, sha256 TEXT, mime TEXT, size INTEGER);
                CREATE TABLE IF NOT EXISTS failures(id INTEGER PRIMARY KEY, url TEXT, error TEXT, depth INTEGER, created TEXT);
                CREATE TABLE IF NOT EXISTS crawl_queue(
                    id INTEGER PRIMARY KEY, url TEXT UNIQUE, depth INTEGER, state TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0, error TEXT, updated TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, text);
            """)
            page_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(pages)")}
            if "completeness" not in page_columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN completeness TEXT DEFAULT 'unknown'")
            if "completeness_notes" not in page_columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN completeness_notes TEXT DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_queue_state ON crawl_queue(state,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_pages_state ON pages(state,id)")
            # A process crash can leave a queue item marked running; safely retry it.
            self.db.execute(
                "UPDATE crawl_queue SET state='pending',error='Recovered after interrupted run',updated=? WHERE state='running'",
                (self.now(),)
            )
            # Rebuild FTS from canonical page rows to repair old duplicate/misaligned rowids.
            self.db.execute("DELETE FROM pages_fts")
            self.db.execute("INSERT INTO pages_fts(rowid,title,text) SELECT id,title,text FROM pages")

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def add_site(self, url):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sites(origin,created) VALUES(?,?)", (origin, self.now()))

    def enqueue(self, url, depth):
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO crawl_queue(url,depth,state,updated) VALUES(?,?,'pending',?)",
                (url, depth, self.now()),
            )
            return cur.rowcount == 1

    def pending_queue(self, limit=10000):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT url,depth,attempts FROM crawl_queue WHERE state='pending' ORDER BY id LIMIT ?", (limit,)
            ).fetchall()]

    def set_queue_state(self, url, state, error=None, increment=False):
        if state not in {"pending", "running", "saved", "failed", "passed"}:
            raise ValueError("Invalid queue state")
        with self.lock, self.db:
            self.db.execute(
                "UPDATE crawl_queue SET state=?,error=?,attempts=attempts+?,updated=? WHERE url=?",
                (state, error, int(increment), self.now(), url),
            )

    def restore_passed(self):
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE crawl_queue SET state='pending',error=NULL,updated=? WHERE state='passed'",
                (self.now(),)
            )
            return cur.rowcount

    def retry_failed(self):
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE crawl_queue SET state='pending',error=NULL,updated=? WHERE state='failed'", (self.now(),)
            )
            return cur.rowcount

    def pass_url(self, url):
        self.set_queue_state(url, "passed")

    def save_asset(self, url, body, mime):
        digest = hashlib.sha256(body).hexdigest()
        ext = mimetypes.guess_extension(mime) or ""
        if ext == ".jpe":
            ext = ".jpg"
        name = digest + ext
        path = self.assets_dir / name
        if not path.exists():
            path.write_bytes(body)
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?)",
                (url, "assets/" + name, digest, mime, len(body)),
            )
        return "../assets/" + name

    def asset_map(self):
        with self.lock:
            return {r["url"]: r["path"] for r in self.db.execute("SELECT url,path FROM assets")}

    def save_page(self, url, requested_url, title, html, text, status, depth, links,
                  completeness="unknown", completeness_notes=""):
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()
            if row:
                pid = row["id"]
            else:
                cur = self.db.execute(
                    """INSERT INTO pages(url,requested_url,title,html_path,text,status,depth,state,saved_at,sha256)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (url, requested_url, title, "", text, status, depth, "saved", self.now(), digest),
                )
                pid = cur.lastrowid
            filename = f"{pid:08d}.html"
            (self.pages_dir / filename).write_text(html, encoding="utf-8")
            self.db.execute(
                """UPDATE pages SET requested_url=?,title=?,html_path=?,text=?,status=?,depth=?,
                   state='saved',error=NULL,saved_at=?,sha256=?,completeness=?,completeness_notes=? WHERE id=?""",
                (requested_url, title, "pages/" + filename, text, status, depth, self.now(), digest,
                 completeness, completeness_notes[:4000], pid),
            )
            self.db.execute("DELETE FROM links WHERE source_id=?", (pid,))
            self.db.executemany("INSERT INTO links VALUES(?,?)", [(pid, x) for x in links])
            self.db.execute("DELETE FROM pages_fts WHERE rowid=?", (pid,))
            self.db.execute("INSERT INTO pages_fts(rowid,title,text) VALUES(?,?,?)", (pid, title, text))
            self.db.execute(
                "UPDATE crawl_queue SET state='saved',error=NULL,updated=? WHERE url IN (?,?)",
                (self.now(), url, requested_url),
            )
            self.db.commit()
        return pid

    def save_failure(self, url, error, depth):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO failures(url,error,depth,created) VALUES(?,?,?,?)",
                (url, error[:2000], depth, self.now()),
            )
            self.db.execute(
                "UPDATE crawl_queue SET state='failed',error=?,attempts=attempts+1,updated=? WHERE url=?",
                (error[:2000], self.now(), url),
            )

    def search(self, query, limit=100):
        if not query.strip():
            return []
        with self.lock:
            try:
                rows = self.db.execute(
                    """SELECT p.id,p.title,p.url,p.html_path,p.saved_at FROM pages_fts f
                       JOIN pages p ON p.id=f.rowid WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                q = "%" + query + "%"
                rows = self.db.execute(
                    "SELECT id,title,url,html_path,saved_at FROM pages WHERE title LIKE ? OR text LIKE ? LIMIT ?",
                    (q, q, limit),
                ).fetchall()
        return [dict(x) for x in rows]

    def list_pages(self, limit=1000):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,title,url,html_path,state,status,saved_at,completeness,completeness_notes FROM pages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(x) for x in rows]

    def list_failures(self, limit=1000):
        with self.lock:
            return [dict(x) for x in self.db.execute(
                "SELECT url,error,depth,created FROM failures ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def stats(self):
        with self.lock:
            return tuple(self.db.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
                         for t in ("pages", "assets", "failures"))

    def verify(self):
        problems = []
        with self.lock:
            rows = self.db.execute("SELECT id,html_path,sha256 FROM pages").fetchall()
            for row in rows:
                path = self.root / row["html_path"]
                if not path.is_file():
                    problems.append(f"Missing page file: {row['html_path']}")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                    problems.append(f"Hash mismatch: {row['html_path']}")
            assets = self.db.execute("SELECT path,sha256 FROM assets").fetchall()
            for row in assets:
                path = self.root / row["path"]
                if not path.is_file():
                    problems.append(f"Missing asset: {row['path']}")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                    problems.append(f"Hash mismatch: {row['path']}")
        return problems

    @staticmethod
    def _url_key(url):
        if not url:
            return ""
        return urldefrag(url.strip())[0]

    @staticmethod
    def _portable_page_path(url, page_id):
        parsed = urlparse(url)
        host = (parsed.hostname or "unknown-host").lower()
        host = re.sub(r"[^a-z0-9.-]+", "_", host)
        parts = [p for p in parsed.path.split("/") if p]
        safe_parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", p)[:80] or "item" for p in parts]
        if not safe_parts or parsed.path.endswith("/"):
            safe_parts.append("index")
        leaf = safe_parts[-1]
        if "." in leaf:
            stem, ext = leaf.rsplit(".", 1)
            safe_parts[-1] = f"{stem[:60]}-{page_id:08d}.{ext[:12]}"
        else:
            safe_parts[-1] = f"{leaf[:60]}-{page_id:08d}.html"
        return "pages/" + host + "/" + "/".join(safe_parts)

    def export_zip(self, destination):
        """Create a portable archive with URL-derived paths and rewritten local dependencies."""
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT id,url,requested_url,title,html_path FROM pages ORDER BY title COLLATE NOCASE"
            ).fetchall()]
            assets = [dict(r) for r in self.db.execute("SELECT url,path,mime FROM assets").fetchall()]

        page_paths = {}
        for row in rows:
            target = self._portable_page_path(row["url"], row["id"])
            page_paths[self._url_key(row["url"])] = target
            if row.get("requested_url"):
                page_paths.setdefault(self._url_key(row["requested_url"]), target)
        asset_paths = {self._url_key(a["url"]): a["path"] for a in assets}
        asset_by_url = {self._url_key(a["url"]): a for a in assets}

        def relative(source_path, target_path):
            return posixpath.relpath(target_path, posixpath.dirname(source_path) or ".")

        def resolve_page(base_url, value):
            absolute = self._url_key(urljoin(base_url, value))
            return page_paths.get(absolute)

        def resolve_asset(base_url, value):
            absolute = self._url_key(urljoin(base_url, value))
            return asset_paths.get(absolute)

        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            # Rewrite HTML references relative to each exported page's actual directory.
            for row in rows:
                source = self.root / row["html_path"]
                page_path = page_paths[self._url_key(row["url"])]
                soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html.parser")
                for element in soup.select("a[href],area[href]"):
                    value = element.get("href", "")
                    target = resolve_page(row["url"], value)
                    if target:
                        suffix = ""
                        if "#" in value:
                            suffix = "#" + value.split("#", 1)[1]
                        element["href"] = relative(page_path, target) + suffix
                for element in soup.select("[src],link[href],source[src],video[poster],audio[src],script[src]"):
                    for attr in ("src", "href", "poster"):
                        value = element.get(attr)
                        if value:
                            target = resolve_asset(row["url"], value)
                            if target:
                                element[attr] = relative(page_path, target)
                    srcset = element.get("srcset")
                    if srcset:
                        rewritten = []
                        for candidate in srcset.split(","):
                            bits = candidate.strip().split()
                            if bits:
                                target = resolve_asset(row["url"], bits[0])
                                if target:
                                    bits[0] = relative(page_path, target)
                                rewritten.append(" ".join(bits))
                        element["srcset"] = ", ".join(rewritten)
                for element in soup.select("[style]"):
                    style = element.get("style", "")
                    def replace_inline(match):
                        raw = match.group(1).strip().strip("\\\"'")
                        target = resolve_asset(row["url"], raw)
                        return "url('" + (relative(page_path, target) if target else raw) + "')"
                    element["style"] = re.sub(r"url[(]\\s*['"]?(.*?)['"]?\\s*[)]", replace_inline, style, flags=re.I)
                z.writestr(page_path, str(soup))

            # CSS assets can themselves reference fonts and images; rewrite those too.
            for asset in assets:
                source = self.root / asset["path"]
                if not source.is_file():
                    continue
                output = source.read_bytes()
                if (asset.get("mime") or "").split(";", 1)[0].lower() == "text/css":
                    try:
                        css = output.decode("utf-8", errors="replace")
                        css_path = asset["path"]
                        def replace_css(match):
                        raw = match.group(1).strip().strip("\\\"'")
                            if raw.startswith(("data:", "blob:", "#")):
                                return match.group(0)
                            absolute = self._url_key(urljoin(asset["url"], raw))
                            target = asset_paths.get(absolute)
                            return "url('" + (relative(css_path, target) if target else raw) + "')"
                        css = re.sub(r"url[(]\\s*['"]?(.*?)['"]?\\s*[)]", replace_css, css, flags=re.I)
                        output = css.encode("utf-8")
                    except Exception:
                        pass
                z.writestr(asset["path"], output)

            links = []
            for row in rows:
                target = page_paths[self._url_key(row["url"])]
                label = (row["title"] or row["url"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                links.append(f'<li><a href="{target}">{label}</a><small> — {row["url"]}</small></li>')
            index = (
                "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Offline Archiver Library</title><style>body{font:16px Segoe UI,Arial;max-width:1100px;margin:40px auto;padding:0 20px;color:#172b4d}"
                "li{margin:12px 0}small{color:#667085;display:block}input{width:100%;padding:12px;box-sizing:border-box}</style></head><body>"
                "<h1>Offline Archiver Library</h1><p>Portable snapshot. Server-dependent functions may not work offline.</p>"
                "<input id='q' type='search' placeholder='Filter saved pages by title or URL'><ol id='pages'>"
                + "".join(links) + "</ol><script>const q=document.getElementById('q');q.addEventListener('input',()=>{const v=q.value.toLowerCase();"
                "document.querySelectorAll('#pages li').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(v));});</script></body></html>"
            )
            z.writestr("index.html", index)
            z.writestr("manifest.json", json.dumps({
                "format": "OfflineArchiverPortable", "version": 3, "generated": self.now(),
                "pages": len(rows), "assets": len(assets)
            }, indent=2))

    def queue_items(self, state=None, limit=5000):
        with self.lock:
            if state and state != "all":
                rows = self.db.execute("SELECT url,depth,state,attempts,error,updated FROM crawl_queue WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT url,depth,state,attempts,error,updated FROM crawl_queue ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def queue_stats(self):
        with self.lock:
            counts = {r["state"]: r["n"] for r in self.db.execute("SELECT state,COUNT(*) n FROM crawl_queue GROUP BY state")}
            counts["downloaded"] = self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            counts["assets"] = self.db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            counts["failures"] = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
            counts["incomplete"] = self.db.execute("SELECT COUNT(*) FROM pages WHERE completeness IN ('partial','incomplete')").fetchone()[0]
            counts["attempted"] = self.db.execute("SELECT COUNT(*) FROM crawl_queue WHERE attempts>0 OR state!='pending'").fetchone()[0]
            counts["redownloaded"] = self.db.execute("SELECT COUNT(*) FROM pages WHERE requested_url!=url").fetchone()[0]
        return counts

    def retry_incomplete(self):
        with self.lock, self.db:
            rows = self.db.execute("SELECT url,depth FROM pages WHERE completeness IN ('partial','incomplete')").fetchall()
            for row in rows:
                self.db.execute(
                    "INSERT INTO crawl_queue(url,depth,state,attempts,error,updated) VALUES(?,?,'pending',0,NULL,?) "
                    "ON CONFLICT(url) DO UPDATE SET depth=excluded.depth,state='pending',error=NULL,updated=excluded.updated",
                    (row["url"], row["depth"], self.now())
                )
            return len(rows)

    def add_url(self, url, depth=0):
        return self.enqueue(url, depth)

    def force_redownload(self, url, depth=0):
        with self.lock, self.db:
            self.db.execute("INSERT INTO crawl_queue(url,depth,state,attempts,error,updated) VALUES(?,?,'pending',0,NULL,?) ON CONFLICT(url) DO UPDATE SET depth=excluded.depth,state='pending',error=NULL,updated=excluded.updated", (url, depth, self.now()))
            return True
