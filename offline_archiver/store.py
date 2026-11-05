from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
                    error TEXT, saved_at TEXT, sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS links(source_id INTEGER, target_url TEXT);
                CREATE TABLE IF NOT EXISTS assets(url TEXT PRIMARY KEY, path TEXT, sha256 TEXT, mime TEXT, size INTEGER);
                CREATE TABLE IF NOT EXISTS failures(id INTEGER PRIMARY KEY, url TEXT, error TEXT, depth INTEGER, created TEXT);
                CREATE TABLE IF NOT EXISTS asset_issues(
                    id INTEGER PRIMARY KEY, page_url TEXT, asset_url TEXT, resource_type TEXT,
                    error TEXT, http_status INTEGER, created TEXT
                );
                CREATE TABLE IF NOT EXISTS crawl_queue(
                    id INTEGER PRIMARY KEY, url TEXT UNIQUE, depth INTEGER, state TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0, error TEXT, updated TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, text);
            """)
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

    def save_asset_issue(self, page_url, asset_url, resource_type, error, http_status=0):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO asset_issues(page_url,asset_url,resource_type,error,http_status,created) VALUES(?,?,?,?,?,?)",
                (page_url, asset_url, resource_type, str(error)[:2000], int(http_status or 0), self.now()),
            )

    def list_asset_issues(self, limit=2000):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT page_url,asset_url,resource_type,error,http_status,created FROM asset_issues ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()]

    def list_queue(self, state=None, limit=2000):
        with self.lock:
            if state:
                rows = self.db.execute(
                    "SELECT url,depth,state,attempts,error,updated FROM crawl_queue WHERE state=? ORDER BY id DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT url,depth,state,attempts,error,updated FROM crawl_queue ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def save_page(self, url, requested_url, title, html, text, status, depth, links, incomplete=False):
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()
            if row:
                pid = row["id"]
            else:
                cur = self.db.execute(
                    """INSERT INTO pages(url,requested_url,title,html_path,text,status,depth,state,saved_at,sha256)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (url, requested_url, title, "", text, status, depth, "incomplete" if incomplete else "saved", self.now(), digest),
                )
                pid = cur.lastrowid
            filename = f"{pid:08d}.html"
            (self.pages_dir / filename).write_text(html, encoding="utf-8")
            self.db.execute(
                """UPDATE pages SET requested_url=?,title=?,html_path=?,text=?,status=?,depth=?,
                   state=?,error=NULL,saved_at=?,sha256=? WHERE id=?""",
                (requested_url, title, "pages/" + filename, text, status, depth,
                 "incomplete" if incomplete else "saved", self.now(), digest, pid),
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
                "SELECT id,title,url,html_path,state,status,saved_at FROM pages ORDER BY id DESC LIMIT ?", (limit,)
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

    def export_zip(self, destination):
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT id,url,title,html_path FROM pages ORDER BY title COLLATE NOCASE"
            ).fetchall()]
            assets = [dict(r) for r in self.db.execute("SELECT url,path FROM assets").fetchall()]
        local = {r["url"]: Path(r["html_path"]).name for r in rows}
        asset_local = {r["url"]: r["path"] for r in assets}
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for row in rows:
                source = self.root / row["html_path"]
                soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html.parser")
                for a in soup.select("a[href]"):
                    target = a.get("href", "")
                    if target in local:
                        a["href"] = local[target]
                for el in soup.select("[src],link[href],source[src],video[poster]"):
                    for attr in ("src", "href", "poster"):
                        value = el.get(attr)
                        if value in asset_local:
                            el[attr] = "../" + asset_local[value]
                z.writestr(row["html_path"], str(soup))
            for folder in (self.assets_dir,):
                for f in folder.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(self.root))
            links = []
            for row in rows:
                label = (row["title"] or row["url"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                links.append(f'<li><a href="pages/{local[row["url"]]}">{label}</a><small> — {row["url"]}</small></li>')
            index = (
                "<!doctype html><html><head><meta charset='utf-8'><title>Offline Archiver Library</title>"
                "<style>body{font:16px Segoe UI,Arial;max-width:1000px;margin:40px auto;padding:0 20px;color:#172b4d}"
                "li{margin:12px 0}small{color:#667085}</style></head><body><h1>Offline Archiver Library</h1>"
                "<p>Portable snapshot. Dynamic server features may not work offline.</p><ol>"
                + "".join(links) + "</ol></body></html>"
            )
            z.writestr("index.html", index)
            z.writestr("manifest.json", json.dumps({
                "format": "OfflineArchiverPortable", "version": 2, "generated": self.now(),
                "pages": len(rows), "assets": len(assets)
            }, indent=2))
