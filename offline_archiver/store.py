from __future__ import annotations
import hashlib, json, mimetypes, sqlite3, threading, zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

class Library:
    def __init__(self, root: Path):
        self.root=root
        self.pages_dir=root/"pages"; self.assets_dir=root/"assets"
        self.pages_dir.mkdir(parents=True,exist_ok=True); self.assets_dir.mkdir(parents=True,exist_ok=True)
        self.db_path=root/"library.sqlite3"; self.lock=threading.RLock()
        self.db=sqlite3.connect(self.db_path,check_same_thread=False); self.db.row_factory=sqlite3.Row
        with self.lock,self.db:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sites(id INTEGER PRIMARY KEY,origin TEXT UNIQUE,created TEXT);
            CREATE TABLE IF NOT EXISTS pages(id INTEGER PRIMARY KEY,url TEXT UNIQUE,requested_url TEXT,title TEXT,html_path TEXT,text TEXT,status INTEGER,depth INTEGER,state TEXT,error TEXT,saved_at TEXT,sha256 TEXT);
            CREATE TABLE IF NOT EXISTS links(source_id INTEGER,target_url TEXT);
            CREATE TABLE IF NOT EXISTS assets(url TEXT PRIMARY KEY,path TEXT,sha256 TEXT,mime TEXT,size INTEGER);
            CREATE TABLE IF NOT EXISTS failures(id INTEGER PRIMARY KEY,url TEXT,error TEXT,depth INTEGER,created TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title,text);
            """)
    @staticmethod
    def now(): return datetime.now(timezone.utc).isoformat()
    def add_site(self,url):
        p=urlparse(url); origin=f"{p.scheme}://{p.netloc}"
        with self.lock,self.db: self.db.execute("INSERT OR IGNORE INTO sites(origin,created) VALUES(?,?)",(origin,self.now()))
    def save_asset(self,url,body,mime):
        digest=hashlib.sha256(body).hexdigest(); ext=mimetypes.guess_extension(mime) or ""
        if ext==".jpe": ext=".jpg"
        name=digest+ext; path=self.assets_dir/name
        if not path.exists(): path.write_bytes(body)
        with self.lock,self.db:
            self.db.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?)",(url,"assets/"+name,digest,mime,len(body)))
        return "../assets/"+name
    def save_page(self,url,requested_url,title,html,text,status,depth,links):
        digest=hashlib.sha256(html.encode()).hexdigest()
        with self.lock:
            row=self.db.execute("SELECT id FROM pages WHERE url=?",(url,)).fetchone()
            if row: pid=row["id"]
            else:
                cur=self.db.execute("INSERT INTO pages(url,requested_url,title,html_path,text,status,depth,state,saved_at,sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (url,requested_url,title,"",text,status,depth,"saved",self.now(),digest)); pid=cur.lastrowid
            filename=f"{pid:08d}.html"; (self.pages_dir/filename).write_text(html,encoding="utf-8")
            self.db.execute("UPDATE pages SET title=?,html_path=?,text=?,status=?,depth=?,state='saved',saved_at=?,sha256=? WHERE id=?",
                (title,"pages/"+filename,text,status,depth,self.now(),digest,pid))
            self.db.execute("DELETE FROM links WHERE source_id=?",(pid,))
            self.db.executemany("INSERT INTO links VALUES(?,?)",[(pid,x) for x in links])
            self.db.execute("INSERT INTO pages_fts(title,text) VALUES(?,?)",(title,text)); self.db.commit()
        return pid
    def save_failure(self,url,error,depth):
        with self.lock,self.db: self.db.execute("INSERT INTO failures(url,error,depth,created) VALUES(?,?,?,?)",(url,error[:2000],depth,self.now()))
    def search(self,query,limit=100):
        if not query.strip(): return []
        with self.lock:
            try: rows=self.db.execute("SELECT p.id,p.title,p.url,p.html_path,p.saved_at FROM pages_fts f JOIN pages p ON p.id=f.rowid WHERE pages_fts MATCH ? LIMIT ?",(query,limit)).fetchall()
            except sqlite3.OperationalError:
                q="%"+query+"%"; rows=self.db.execute("SELECT id,title,url,html_path,saved_at FROM pages WHERE title LIKE ? OR text LIKE ? LIMIT ?",(q,q,limit)).fetchall()
        return [dict(x) for x in rows]
    def list_pages(self,limit=500):
        with self.lock: rows=self.db.execute("SELECT id,title,url,html_path,state,status,saved_at FROM pages ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [dict(x) for x in rows]
    def stats(self):
        with self.lock:
            return tuple(self.db.execute("SELECT COUNT(*) FROM "+t).fetchone()[0] for t in ("pages","assets","failures"))
    def export_zip(self,destination):
        with self.lock:
            rows=[dict(r) for r in self.db.execute("SELECT id,url,title,html_path FROM pages ORDER BY title COLLATE NOCASE").fetchall()]
        local={r["url"]:Path(r["html_path"]).name for r in rows}
        with zipfile.ZipFile(destination,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for folder in (self.pages_dir,self.assets_dir):
                for f in folder.rglob("*"):
                    if f.is_file():
                        rel=f.relative_to(self.root)
                        if folder==self.pages_dir:
                            soup=BeautifulSoup(f.read_text(encoding="utf-8",errors="replace"),"html.parser")
                            for a in soup.select("a[href]"):
                                target=a.get("href","")
                                if target in local:
                                    a["href"]=local[target]
                            z.writestr(str(rel),str(soup))
                        else:
                            z.write(f,rel)
            links=[]
            for row in rows:
                label=(row["title"] or row["url"]).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                links.append(f'<li><a href="pages/{local[row["url"]]}">{label}</a><small> — {row["url"]}</small></li>')
            index="<!doctype html><html><head><meta charset='utf-8'><title>Offline Archiver Library</title><style>body{font:16px Segoe UI,Arial;max-width:1000px;margin:40px auto;padding:0 20px;color:#172b4d}li{margin:12px 0}small{color:#667085}</style></head><body><h1>Offline Archiver Library</h1><p>Portable snapshot export. Some interactive or server-dependent features may require an online connection.</p><ol>"+ "".join(links)+"</ol></body></html>"
            z.writestr("index.html",index)
            z.write(self.db_path,"library.sqlite3")
            z.writestr("manifest.json",json.dumps({"format":"OfflineArchiverPortable","version":1,"generated":self.now(),"pages":len(rows)},indent=2))
