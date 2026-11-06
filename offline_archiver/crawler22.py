#!/usr/bin/env python3
"""
Offline Archiver 2.2 - a standalone program for saving websites you are allowed to keep.

How it works
  * Drives your installed Microsoft Edge (via Playwright). You sign in yourself, once, in a window
    the program opens; the session is kept in a private profile folder. No password is ever read
    or stored by this program.
  * Records the REAL network responses of every page (HTML, CSS, JS, fonts, images, data files),
    so pages are replayed offline exactly as built: same theme, graphics, scripts and menus.
  * Each distinct file is stored once (content hash) and compressed. No base64 bloat.
  * Replay viewer opens the archive in Edge with all original links working offline.
  * Full-text search, topic tree (Specialty > Topic > Subtopic), six-list ledger, pause/resume,
    retries, pass/restore, selective redownload, offline verification, update checker with
    sentence-level before/after, ZIP export.

Always on: robots.txt is respected, requests are rate-limited, and only the sites/domains YOU
configure are visited. Save only content you have the right to keep offline.

Needs: Python 3.10+, `pip install playwright`, and Microsoft Edge installed (no extra download).
"""
import argparse, asyncio, ctypes, getpass, gzip, hashlib, json, os, re, sqlite3, sys, threading, time, zipfile
import difflib, html as H, urllib.robotparser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urldefrag, urljoin, urlparse

def _default_home():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "archives"


HOME = Path(os.environ.get("OA_HOME", _default_home()))
UA_ROBOTS = "OfflineArchiver"
TRACKERS = ("google-analytics.com", "googletagmanager.com", "doubleclick.net", "facebook.net", "connect.facebook.com",
            "hotjar.com", "clarity.ms", "scorecardresearch.com", "adservice.google.com", "googlesyndication.com",
            "segment.io", "segment.com", "mixpanel.com", "newrelic.com", "nr-data.net", "taboola.com", "outbrain.com")
SKIP_EXT = (".zip", ".rar", ".7z", ".exe", ".msi", ".dmg", ".iso", ".mp4", ".mkv", ".avi", ".mov", ".mp3", ".wav")
LOGIN_PATH = re.compile(r"/(login|logout|signin|sign-in|signout|sign-out|authenticate|session-expired)(/|$)", re.I)
COMPRESSIBLE = re.compile(r"(text/|json|javascript|xml|svg|html|css|x-font-ttf|opentype|vnd.ms-fontobject)", re.I)
NOW = lambda: time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ project / database
class Project:
    def __init__(self, name):
        self.name = name
        self.dir = HOME / name
        self.cfg_path = self.dir / "project.json"
        self.store = self.dir / "store"
        self.cfg = json.loads(self.cfg_path.read_text("utf-8")) if self.cfg_path.exists() else {}
        self.db = None

    def open(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.dir / "state.db", timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, final_url TEXT, status TEXT, depth INT, hops INT DEFAULT 0,
          parent TEXT, via TEXT, title TEXT, spec TEXT, topic TEXT, sub TEXT, text_hash TEXT, etag TEXT,
          attempts INT DEFAULT 0, error TEXT, incomplete TEXT, verified INT DEFAULT 0, reason TEXT,
          redownloads INT DEFAULT 0, discovered TEXT, saved TEXT, updated TEXT, is_update INT DEFAULT 0, seq INTEGER);
        CREATE TABLE IF NOT EXISTS res(key TEXT PRIMARY KEY, url TEXT, method TEXT, status INT, mime TEXT, loc TEXT, hash TEXT, size INT);
        CREATE TABLE IF NOT EXISTS page_res(page_url TEXT, key TEXT, PRIMARY KEY(page_url,key));
        CREATE TABLE IF NOT EXISTS events(t TEXT, url TEXT, kind TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS docs(url TEXT PRIMARY KEY, title TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS changes(t TEXT, url TEXT, title TEXT, added TEXT, removed TEXT);
        CREATE TABLE IF NOT EXISTS redirects(page_url TEXT PRIMARY KEY, chain TEXT, final_url TEXT, t TEXT);
        CREATE TABLE IF NOT EXISTS redirect_hosts(host TEXT PRIMARY KEY, decision TEXT, n INT, example TEXT);
        """)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(url UNINDEXED, title, body)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()
        return self

    def save_cfg(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2), "utf-8")

    def ev(self, url, kind, detail=""):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (NOW(), url, kind, str(detail)[:2000]))

    # content-addressed, compressed blob store
    def put_blob(self, data: bytes, mime: str):
        h = hashlib.sha256(data).hexdigest()
        p = self.store / h[:2] / (h + (".gz" if COMPRESSIBLE.search(mime or "") else ".bin"))
        if not p.exists() and not (self.store / h[:2] / (h + ".gz")).exists() and not (self.store / h[:2] / (h + ".bin")).exists():
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(gzip.compress(data, 6) if p.suffix == ".gz" else data)
        return h

    def get_blob(self, h):
        for suf in (".gz", ".bin"):
            p = self.store / h[:2] / (h + suf)
            if p.exists():
                b = p.read_bytes()
                return gzip.decompress(b) if suf == ".gz" else b
        return None

    def index_doc(self, url, title, body):
        self.db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?)", (url, title, body))
        if self.fts:
            self.db.execute("DELETE FROM fts WHERE url=?", (url,))
            self.db.execute("INSERT INTO fts VALUES(?,?,?)", (url, title, body))

    def search(self, q, limit=40):
        terms = [t for t in re.findall(r"\w+", q)]
        if not terms:
            return []
        if self.fts:
            m = " ".join('"%s"' % t for t in terms)
            try:
                return [dict(r) for r in self.db.execute(
                    "SELECT url,title,snippet(fts,2,'\x01','\x02',' … ',28) AS snip FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?", (m, limit))]
            except sqlite3.OperationalError:
                pass
        out = []
        for r in self.db.execute("SELECT url,title,body FROM docs"):
            low = r["body"].lower()
            if all(t.lower() in low for t in terms):
                i = low.find(terms[0].lower())
                s = r["body"][max(0, i - 100):i + 200]
                out.append({"url": r["url"], "title": r["title"], "snip": s})
                if len(out) >= limit:
                    break
        return out


def must(name):
    p = Project(name)
    if not p.cfg_path.exists():
        sys.exit(f"No project named '{name}'. Create it with: init {name} <start-url>")
    return p.open()


# ------------------------------------------------------------------ url rules
def norm(u, base=None):
    try:
        u = urljoin(base, u) if base else u
        u, _ = urldefrag(u)
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            return None
        q = "&".join(x for x in p.query.split("&") if x and not x.lower().startswith(("utm_", "fbclid", "gclid")))
        return p._replace(query=q, netloc=p.netloc.lower()).geturl()
    except Exception:
        return None


def host_ok(host, allowed):
    return any(host == a or host.endswith("." + a) for a in allowed)


def eligible(cfg, link, cur_host, cur_hops, cur_depth):
    """Decide whether a discovered link may be queued. Returns (ok, hops)."""
    if rule_reason(cfg, link):
        return False, 0
    p = urlparse(link)
    if p.path.lower().endswith(SKIP_EXT) and not cfg.get("files"):
        return False, 0
    if LOGIN_PATH.search(p.path):
        return False, 0
    if cfg.get("exclude") and re.search(cfg["exclude"], link):
        return False, 0
    if cfg.get("include") and not re.search(cfg["include"], link):
        return False, 0
    if cfg["max_depth"] and cur_depth + 1 > cfg["max_depth"]:
        return False, 0
    internal = p.netloc in [cfg["host"]] + cfg.get("also_internal", [])
    if internal:
        return True, 0
    hops = cur_hops + (1 if p.netloc != cur_host else 0)
    if hops > cfg.get("external_hops", 0):
        return False, 0
    allow = cfg.get("allow_domains", [])
    if allow and not host_ok(p.netloc, allow):
        return False, 0
    return True, hops


def is_internal(cfg, host):
    return host in [cfg["host"]] + cfg.get("also_internal", [])


def redirect_decision(cfg, host):
    """allow | block | ask  for a page that redirects (or navigates) to another website."""
    if is_internal(cfg, host):
        return "allow"
    if host_ok(host, cfg.get("redirect_block", [])):
        return "block"
    if host_ok(host, cfg.get("redirect_allow", []) + cfg.get("allow_domains", [])):
        return "allow"
    return {"allow": "allow", "ask": "ask", "listed": "block", "block": "block"}.get(cfg.get("redirects", "allow"), "allow")


def note_redirect_host(proj, host, decision, example):
    proj.db.execute("INSERT INTO redirect_hosts VALUES(?,?,1,?) ON CONFLICT(host) DO UPDATE SET n=n+1, decision=excluded.decision, example=excluded.example",
                    (host, decision, example[:300]))


def rule_norm(r):
    r = r.strip()
    if r.lower().startswith("re:"):
        return r
    if r.lower().startswith(("http://", "https://")):
        return norm(r) or r
    return r.strip("/").lower()          # topic / section name, e.g. "neuro"


def rule_match(rule, url):
    if rule.lower().startswith("re:"):
        try:
            return re.search(rule[3:], url) is not None
        except re.error:
            return False
    if rule.startswith(("http://", "https://")):
        r, u = rule.rstrip("/"), url.rstrip("/")
        return u == r or u.startswith(r + "/") or u.startswith(r + "?")
    segs = [unquote(x).lower() for x in urlparse(url).path.split("/") if x]
    return rule in segs


def rule_reason(cfg, url):
    """None if the page may be downloaded; otherwise the reason it is held back.
    The start page is always downloaded unless it matches a skip rule."""
    if any(rule_match(r, url) for r in cfg.get("skip", [])):
        return "excluded by your skip rule"
    only = cfg.get("only", [])
    if only and url != norm(cfg.get("start", "")) and not any(rule_match(r, url) for r in only):
        return "outside your only-rules"
    return None


def apply_rules(proj):
    """Hold pending pages that now break a rule; release pages held only by rules that were removed."""
    held = 0
    for r in proj.db.execute("SELECT url FROM pages WHERE status IN ('pending','needs_login','needs_decision')").fetchall():
        why = rule_reason(proj.cfg, r["url"])
        if why:
            proj.db.execute("UPDATE pages SET status='passed', reason=? WHERE url=?", (why, r["url"]))
            proj.ev(r["url"], "PASSED", why)
            held += 1
    for r in proj.db.execute("SELECT url FROM pages WHERE status='passed' AND reason IN ('excluded by your skip rule','outside your only-rules')").fetchall():
        if rule_reason(proj.cfg, r["url"]) is None:
            proj.db.execute("UPDATE pages SET status='pending', reason=NULL WHERE url=?", (r["url"],))
            proj.ev(r["url"], "RESTORED", "rule removed")
    proj.db.commit()
    proj.save_cfg()
    return held


def hierarchy(url, crumbs):
    c = [x for x in (crumbs or []) if x and not re.fullmatch(r"(?i)home|start|main", x)]
    if len(c) >= 2:
        return c[0][:60], c[1][:60], (c[2][:60] if len(c) > 2 else "")
    seg = [unquote(s) for s in urlparse(url).path.split("/") if s]
    return (seg[0] if seg else "General")[:60], (seg[1] if len(seg) > 1 else "")[:60], (seg[2] if len(seg) > 2 else "")[:60]


def sentences(t):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if len(s.strip()) > 2]


def diff_sentences(old, new):
    a, b = sentences(old), sentences(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    add, rem = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            rem += a[i1:i2]
        if tag in ("replace", "insert"):
            add += b[j1:j2]
    return add[:40], rem[:40]


# ------------------------------------------------------------------ robots + pacing
class Robots:
    def __init__(self):
        self.c = {}

    def allowed(self, url):
        o = urlparse(url)
        key = f"{o.scheme}://{o.netloc}"
        if key not in self.c:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.set_url(key + "/robots.txt")
                rp.read()
            except Exception:
                rp = None
            self.c[key] = rp
        rp = self.c[key]
        return True if rp is None else rp.can_fetch(UA_ROBOTS, url)


class Pacer:
    def __init__(self, delay):
        self.delay, self.next, self.lock = delay, defaultdict(float), asyncio.Lock()

    async def wait(self, host):
        async with self.lock:
            t = max(time.monotonic(), self.next[host])
            self.next[host] = t + self.delay
        await asyncio.sleep(max(0, t - time.monotonic()))


# ------------------------------------------------------------------ browser capture
EXTRACT_JS = r"""() => {
 const clean = s => (s||"").replace(/\s+/g," ").trim();
 const crumbs=[...new Set([...document.querySelectorAll('nav[aria-label*="readcrumb" i] a,[class*="readcrumb" i] a,[itemtype*="BreadcrumbList"] a')].map(a=>clean(a.innerText)).filter(Boolean))].slice(0,6);
 const links=[...document.querySelectorAll('a[href]')].map(a=>a.href);
 const body=document.body?document.body.cloneNode(true):null;
 if(body) body.querySelectorAll('script,style,noscript,svg,canvas,template').forEach(e=>e.remove());
 const login=[...document.querySelectorAll('input[type=password]')].some(e=>e.offsetParent!==null);
 return {title:clean(document.title)||clean((document.querySelector('h1')||{}).innerText), crumbs, links, text:body?clean(body.innerText||body.textContent):"", login};
}"""
SCROLL_JS = """async () => { const step=Math.max(innerHeight*0.8,400); for(let y=0;y<document.body.scrollHeight;y+=step){scrollTo(0,y);await new Promise(r=>setTimeout(r,120));} scrollTo(0,0); }"""


class LoginRequired(Exception):
    pass


class RedirectBlocked(Exception):
    def __init__(self, host, url, decision):
        super().__init__(f"redirects to {host}")
        self.host, self.url, self.decision = host, url, decision

# ------------------------------------------------------------------ saved sign-in (optional, encrypted by Windows)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


class DPAPI:
    """Windows Data Protection API: only this Windows user, on this PC, can decrypt."""
    def _blob(self, b):
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(self, fn, data, entropy):
        i, k1 = self._blob(data)
        e, k2 = self._blob(entropy)
        o = _Blob()
        if not fn(ctypes.byref(i), None, ctypes.byref(e), None, None, 0, ctypes.byref(o)):
            raise OSError("Windows could not protect/unprotect the credentials")
        try:
            return ctypes.string_at(o.pbData, o.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(o.pbData)

    def protect(self, data, entropy):
        f = ctypes.windll.crypt32.CryptProtectData
        return self._call(lambda *a: f(a[0], a[1], a[2], a[3], a[4], 0x1, a[6]), data, entropy)

    def unprotect(self, data, entropy):
        return self._call(ctypes.windll.crypt32.CryptUnprotectData, data, entropy)


class NoBackend:
    def protect(self, *a):
        raise SystemExit("Saved sign-in needs Windows (it uses Windows' own encryption). Use 'login' to sign in manually instead.")
    unprotect = protect


class CredStore:
    def __init__(self, proj, backend=None):
        self.path = proj.dir / "creds.bin"
        self.backend = backend or (DPAPI() if sys.platform == "win32" else NoBackend())
        self.entropy = ("OfflineArchiver:" + proj.name).encode()

    def save(self, d):
        self.path.write_bytes(self.backend.protect(json.dumps(d).encode(), self.entropy))

    def load(self):
        if not self.path.exists():
            return None
        try:
            return json.loads(self.backend.unprotect(self.path.read_bytes(), self.entropy))
        except Exception:
            return None

    def clear(self):
        if self.path.exists():
            self.path.unlink()


LOGIN_TAG_JS = r"""() => {
 const vis = e => e && e.offsetParent !== null && !e.disabled && getComputedStyle(e).visibility !== 'hidden';
 document.querySelectorAll('[data-oa]').forEach(e => e.removeAttribute('data-oa'));
 const q = s => [...document.querySelectorAll(s)].filter(vis);
 const pass = q('input[type=password]')[0];
 const otp = q('input').some(e => /one-time-code/i.test(e.autocomplete||'') || /otp|2fa|verification|one.?time|authenticator|security.?code|sms.?code/i.test((e.name||'')+(e.id||'')+(e.placeholder||'')));
 const captcha = !!document.querySelector('iframe[src*=recaptcha],iframe[src*=hcaptcha],iframe[src*=turnstile],.g-recaptcha,.h-captcha,.cf-turnstile');
 const form = pass ? pass.closest('form') : null;
 let scope = form || document;
 let user = null;
 const cands = [...scope.querySelectorAll('input')].filter(e => vis(e) && ['text','email','tel',''].includes((e.type||'').toLowerCase()));
 user = cands.find(e => /user|email|login|account|name|id/i.test((e.name||'')+(e.id||'')+(e.autocomplete||'')+(e.placeholder||''))) || cands[0] || null;
 const target = pass || user;
 const fs = (target && target.closest('form')) || document;
 let submit = fs.querySelector('button[type=submit],input[type=submit]') ||
   [...fs.querySelectorAll('button,input[type=button],a[role=button]')].filter(vis).find(b => /sign|log|continue|next|submit|enter/i.test(b.innerText||b.value||''));
 if (pass) pass.setAttribute('data-oa','pass');
 if (user) user.setAttribute('data-oa','user');
 if (submit && vis(submit)) submit.setAttribute('data-oa','submit');
 return {pass: !!pass, user: !!user, submit: !!(submit && vis(submit)), otp, captcha};
}"""
PASS_VISIBLE_JS = "() => [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null)"


def login_host_ok(cfg, creds, host):
    return host in [cfg["host"]] + cfg.get("also_internal", []) + (creds or {}).get("login_domains", [])


async def perform_login(page, cfg, creds):
    """Fill the site's own sign-in form with the saved account. Returns (ok, message). Never types credentials into an unapproved domain."""
    from playwright.async_api import TimeoutError as PWTimeout
    for step in range(2):                       # step 2 handles 'username first, password next' forms
        host = urlparse(page.url).netloc
        if not login_host_ok(cfg, creds, host):
            return False, f"sign-in form is on a domain you have not approved ({host}); credentials were NOT sent"
        info = await page.evaluate(LOGIN_TAG_JS)
        if info["otp"] or info["captcha"]:
            return False, "extra verification (code or CAPTCHA) is required; sign in yourself with the 'login' command"
        if not info["pass"] and not info["user"]:
            return False, "no sign-in form found"
        if info["user"]:
            await page.fill('[data-oa="user"]', creds["u"])
        if info["pass"]:
            await page.fill('[data-oa="pass"]', creds["p"])
        if info["submit"]:
            await page.click('[data-oa="submit"]')
        else:
            await page.keyboard.press("Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        await asyncio.sleep(1)
        if info["pass"] and not await page.evaluate(PASS_VISIBLE_JS):
            return True, "signed in"
        if info["pass"]:
            return False, "the sign-in form is still showing (wrong credentials, or an extra step is needed)"
    return (not await page.evaluate(PASS_VISIBLE_JS)), "two-step sign-in finished"


class AutoLogin:
    def __init__(self, proj):
        self.proj, self.lock, self.times = proj, asyncio.Lock(), []
        self.creds = CredStore(proj).load()

    @property
    def enabled(self):
        return bool(self.creds)

    async def attempt(self, ctx, url):
        if not self.creds:
            return False
        async with self.lock:
            now = time.monotonic()
            self.times = [t for t in self.times if now - t < 600]
            if len(self.times) >= 3:
                return False
            self.times.append(now)
            page = await ctx.new_page()
            try:
                await page.goto(self.creds.get("login_url") or url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.5)
                ok, msg = await perform_login(page, self.proj.cfg, self.creds)
            except Exception as e:
                ok, msg = False, f"auto sign-in error: {e}"
            finally:
                await page.close()
            self.proj.ev(url, "AUTOLOGIN_OK" if ok else "AUTOLOGIN_FAILED", msg)
            self.proj.db.commit()
            print(f"     auto sign-in: {msg}")
            return ok



def rkey(method, url, body=None):
    k = f"{method} {urldefrag(url)[0]}"
    if body:
        k += " #" + hashlib.sha1(body if isinstance(body, bytes) else str(body).encode()).hexdigest()[:12]
    return k


async def capture(ctx, proj, url, cfg):
    from playwright.async_api import TimeoutError as PWTimeout
    page = await ctx.new_page()
    recs, tasks, failed, errors, skipped = {}, [], [], [], []
    maxb = int(cfg.get("max_resource_mb", 25)) * 1048576

    async def on_resp(r):
        try:
            rq = r.request
            if rq.method not in ("GET", "POST") or r.url.startswith(("data:", "blob:")):
                return
            st = r.status
            hd = await r.all_headers()
            ct, loc = hd.get("content-type", ""), hd.get("location", "")
            rt = rq.resource_type
            if st >= 400 and rt in ("script", "stylesheet", "xhr", "fetch", "font", "image", "document"):
                failed.append(f"HTTP {st} {rt}: {r.url[:150]}")
            h, size = None, 0
            if 300 <= st < 400:
                pass
            else:
                try:
                    body = await r.body()
                except Exception:
                    return
                size = len(body)
                if size > maxb:
                    skipped.append(f"too large ({size // 1048576} MB): {r.url[:150]}")
                    return
                h = proj.put_blob(body, ct)
            recs[rkey(rq.method, r.url, rq.post_data_buffer if rq.method == "POST" else None)] = (r.url, rq.method, st, ct, loc, h, size)
        except Exception as e:
            errors.append(f"record: {e}")

    page.on("response", lambda r: tasks.append(asyncio.create_task(on_resp(r))))
    page.on("requestfailed", lambda q: (not any(t in q.url for t in TRACKERS) and "ERR_ABORTED" not in (q.failure or "")
                                        and failed.append(f"{q.failure} {q.resource_type}: {q.url[:150]}")))
    page.on("pageerror", lambda e: errors.append("script error: " + str(e)[:200]))
    chain, state = [], {"first": True, "blocked": None}
    start_host = urlparse(url).netloc

    async def guard(route):
        rq = route.request
        if cfg.get("block_trackers", True) and any(t in rq.url for t in TRACKERS):
            return await route.abort()
        try:
            is_main_nav = rq.is_navigation_request() and rq.frame == page.main_frame
        except Exception:
            is_main_nav = False
        if is_main_nav:
            if not chain or chain[-1] != rq.url:
                chain.append(rq.url)
            host = urlparse(rq.url).netloc
            if not state["first"] and host != start_host:
                d = redirect_decision(cfg, host)
                note_redirect_host(proj, host, d, rq.url)
                if d != "allow":
                    state["blocked"] = (host, rq.url, d)
                    return await route.abort()
            state["first"] = False
        await route.continue_()
    await page.route("**/*", guard)
    try:
        try:
            main = await page.goto(url, wait_until="domcontentloaded", timeout=int(cfg.get("timeout", 60)) * 1000)
        except Exception as e:
            if state["blocked"]:
                raise RedirectBlocked(*state["blocked"]) from e
            raise
        etag = (await main.all_headers()).get("etag", "") if main else ""
        for ms in (15000, 8000):
            try:
                await page.wait_for_load_state("networkidle", timeout=ms)
            except PWTimeout:
                pass
            if ms == 15000:
                await page.evaluate(SCROLL_JS)
        info = await page.evaluate(EXTRACT_JS)
        final = page.url
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await page.close()
    if info["login"]:
        raise LoginRequired(url)
    return {"final": final, "info": info, "recs": recs, "failed": failed, "errors": errors, "skipped": skipped, "etag": etag, "chain": chain}


# ------------------------------------------------------------------ crawl
def queue_link(proj, link, depth, hops, parent, via):
    cfg = proj.cfg
    link = norm(link)
    if not link:
        return False
    cur = proj.db.execute("INSERT OR IGNORE INTO pages(url,status,depth,hops,parent,via,discovered,seq) VALUES(?,?,?,?,?,?,?,"
                          "(SELECT COALESCE(MAX(seq),0)+1 FROM pages))", (link, "pending", depth, hops, parent, via, NOW()))
    return cur.rowcount > 0


def save_result(proj, row, res, update):
    url, info, cfg = row["url"], res["info"], proj.cfg
    old = proj.db.execute("SELECT text_hash,title,final_url FROM pages WHERE url=?", (url,)).fetchone()
    text = info["text"]
    th = hashlib.sha1(text.encode()).hexdigest()
    spec, topic, sub = hierarchy(res["final"], info["crumbs"])
    for key, (u, m, st, ct, loc, h, size) in res["recs"].items():
        proj.db.execute("INSERT OR REPLACE INTO res VALUES(?,?,?,?,?,?,?,?)", (key, u, m, st, ct, loc, h, size))
        proj.db.execute("INSERT OR IGNORE INTO page_res VALUES(?,?)", (url, key))
    problems = res["failed"][:30] + res["errors"][:10] + res["skipped"][:10]
    inc = None
    if problems:
        inc = f"{len(res['failed'])} failed request(s), {len([e for e in res['errors'] if 'script error' in e])} script error(s), {len(res['skipped'])} skipped"
        proj.ev(url, "INCOMPLETE", "\n".join(problems))
    proj.db.execute("""UPDATE pages SET status='saved',final_url=?,title=?,spec=?,topic=?,sub=?,text_hash=?,error=NULL,incomplete=?,
        verified=0,etag=?,saved=COALESCE(saved,?),updated=?,is_update=0 WHERE url=?""",
                    (res["final"], info["title"] or url, spec, topic, sub, th, inc, res.get("etag") or None, NOW(), NOW(), url))
    proj.index_doc(url, info["title"] or url, text)
    chain = [c for c in (res.get("chain") or [])]
    if res["final"] not in chain:
        chain.append(res["final"])
    if len(chain) > 1 or res["final"] != url:
        proj.db.execute("INSERT OR REPLACE INTO redirects VALUES(?,?,?,?)", (url, json.dumps(chain), res["final"], NOW()))
        fh = urlparse(res["final"]).netloc
        if fh != urlparse(url).netloc:
            note_redirect_host(proj, fh, redirect_decision(cfg, fh), res["final"])
        proj.ev(url, "REDIRECT", " -> ".join(chain)[:500])
    if update and old and old["final_url"] and old["final_url"] != res["final"]:
        proj.db.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (NOW(), url, info["title"], json.dumps([f"Now redirects to {res['final']}"]),
                                                                  json.dumps([f"Previously redirected to {old['final_url']}"])))
        proj.ev(url, "REDIRECT_CHANGED", f"{old['final_url']} -> {res['final']}")
    if update and old and old["text_hash"] and old["text_hash"] != th:
        add, rem = diff_sentences(getattr(proj, "_prev", {}).get(url, ""), text)
        proj.db.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (NOW(), url, info["title"], json.dumps(add), json.dumps(rem)))
        proj.ev(url, "CHANGED", f"+{len(add)} / -{len(rem)} sentences")
    proj.ev(url, "SAVED", f"{len(res['recs'])} resources" + (f"; incomplete: {inc}" if inc else ""))
    # discover links
    new = 0
    cur_host = urlparse(res["final"]).netloc
    row_hops = row["hops"] + (0 if cur_host == urlparse(url).netloc or is_internal(cfg, cur_host) else 1)   # a redirect to another site counts as one hop
    for l in info["links"]:
        n = norm(l)
        if not n:
            continue
        why = rule_reason(cfg, n)
        if why:   # keep excluded links visible in the ledger, as passed, with the reason
            proj.db.execute("INSERT OR IGNORE INTO pages(url,status,depth,hops,parent,via,reason,discovered,seq) VALUES(?,?,?,?,?,?,?,?,"
                            "(SELECT COALESCE(MAX(seq),0)+1 FROM pages))", (n, "passed", row["depth"] + 1, 0, url, f"link on {url}", why, NOW()))
            continue
        ok, hops = eligible(cfg, n, cur_host, row_hops, row["depth"])
        if ok and queue_link(proj, n, row["depth"] + 1, hops, url, f"link on {url}"):
            new += 1
    proj.db.commit()
    return new


async def crawl(proj, show=False, workers=2, update=False, only=None):
    from playwright.async_api import async_playwright
    cfg = proj.cfg
    db = proj.db
    db.execute("UPDATE pages SET status='pending' WHERE status IN ('running','needs_login')")
    if update:
        proj._prev = {r["url"]: r["body"] for r in db.execute("SELECT url,body FROM docs")}
        db.execute("UPDATE pages SET status='pending', is_update=1 WHERE status='saved'")
        db.commit()
    robots, pacer = Robots(), Pacer(float(cfg.get("delay", 2)))
    auto = AutoLogin(proj)
    if auto.enabled:
        print("Saved sign-in found: the program will sign in again by itself if the session expires.")
    stop = {"login": None, "halt": False, "walls": 0}
    lock = asyncio.Lock()

    async def claim():
        async with lock:
            if stop["halt"]:
                return None
            lim = int(cfg.get("max_pages", 0))
            if lim and db.execute("SELECT COUNT(*) FROM pages WHERE status='saved' AND is_update=0").fetchone()[0] >= lim:
                return None
            r = db.execute("SELECT * FROM pages WHERE status='pending' ORDER BY depth, seq LIMIT 1").fetchone()
            if r:
                db.execute("UPDATE pages SET status='running' WHERE url=?", (r["url"],))
                db.commit()
            return r

    async def worker(ctx, n):
        idle = 0
        while True:
            row = await claim()
            if row is None:
                running = db.execute("SELECT COUNT(*) FROM pages WHERE status='running'").fetchone()[0]
                if stop["halt"] or not running or idle > 40:
                    return
                idle += 1
                await asyncio.sleep(0.5)
                continue
            idle = 0
            url = row["url"]
            why = rule_reason(cfg, url)
            if why:
                db.execute("UPDATE pages SET status='passed', reason=? WHERE url=?", (why, url))
                proj.ev(url, "PASSED", why)
                db.commit()
                continue
            if not robots.allowed(url):
                db.execute("UPDATE pages SET status='passed', reason='blocked by robots.txt' WHERE url=?", (url,))
                proj.ev(url, "PASSED", "robots.txt")
                db.commit()
                continue
            try:
                await pacer.wait(urlparse(url).netloc)
                if row["is_update"] and row["etag"]:
                    try:
                        r = await ctx.request.get(url, headers={"If-None-Match": row["etag"]}, timeout=30000)
                        if r.status == 304:
                            db.execute("UPDATE pages SET status='saved', is_update=0 WHERE url=?", (url,))
                            db.commit()
                            continue
                    except Exception:
                        pass
                print(f"[{n}] {row['depth']}  {url}")
                res = await capture(ctx, proj, url, cfg)
                new = save_result(proj, row, res, bool(row["is_update"]))
                stop["walls"] = 0
                print(f"     saved  (+{new} new links, {len(res['recs'])} resources" + (", INCOMPLETE" if res["failed"] or res["errors"] else "") + ")")
            except RedirectBlocked as rb:
                st = "needs_decision" if rb.decision == "ask" else "passed"
                why = f"redirects to {rb.host}" + (" - waiting for your decision (allow-domain / block-domain)" if rb.decision == "ask" else " - blocked by redirect policy")
                db.execute("UPDATE pages SET status=?, reason=? WHERE url=?", (st, why, url))
                proj.ev(url, "REDIRECT_" + ("ASK" if rb.decision == "ask" else "BLOCKED"), f"{rb.host}: {rb.url}")
                db.commit()
                print(f"     {why}")
            except LoginRequired:
                if auto.enabled and await auto.attempt(ctx, url):
                    db.execute("UPDATE pages SET status='pending' WHERE url=?", (url,))
                    db.commit()
                    continue
                db.execute("UPDATE pages SET status='needs_login', reason='login required' WHERE url=?", (url,))
                proj.ev(url, "NEEDS_LOGIN", "sign-in form shown")
                db.commit()
                stop["walls"] += 1
                print(f"     needs login - held aside, crawl continues ({url})")
                if stop["walls"] >= 3:
                    stop["login"], stop["halt"] = url, True
                    print("\n  Three login pages in a row: your session has probably expired.\n  Run:  login", proj.name, "  then  crawl", proj.name)
            except Exception as e:
                att = row["attempts"] + 1
                status = "pending" if att < int(cfg.get("retries", 3)) else "failed"
                db.execute("UPDATE pages SET status=?, attempts=?, error=? WHERE url=?", (status, att, str(e)[:300], url))
                proj.ev(url, "FAIL" if status == "failed" else "RETRY", f"attempt {att}: {e}")
                db.commit()
                print(f"     {status} ({e})"[:160])

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(proj.dir / "profile"), channel="msedge", headless=not show,
                                                         service_workers="block", viewport={"width": 1280, "height": 900})
        try:
            await asyncio.gather(*[worker(ctx, i + 1) for i in range(workers)])
        finally:
            await ctx.close()
    if update:
        write_updates(proj)
    pend = db.execute("SELECT COUNT(*) FROM pages WHERE status='pending'").fetchone()[0]
    print(f"\nStopped. {pend} page(s) still pending." if pend else "\nQueue finished.")


# ------------------------------------------------------------------ offline replay (shared by viewer + verify)
class Replayer:
    def __init__(self, proj):
        self.proj = proj
        self.db = sqlite3.connect(proj.dir / "state.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.local = set()
        self.misses = []

    def lookup(self, method, url, body=None):
        for k in (rkey(method, url, body), rkey(method, url)):
            r = self.db.execute("SELECT * FROM res WHERE key=?", (k,)).fetchone()
            if r:
                return r
        return None

    async def handle(self, route):
        rq = route.request
        u = urlparse(rq.url)
        if u.netloc in self.local:
            return await route.continue_()
        r = self.lookup(rq.method, rq.url, rq.post_data_buffer if rq.method == "POST" else None)
        if not r:
            if rq.is_navigation_request():
                self.misses.append(("document", rq.url))
                page = f"<h2>Not in this archive</h2><p>{H.escape(rq.url)}</p><p>It was not downloaded (not allowed, failed, or outside the crawl limits).</p>"
                return await route.fulfill(status=404, content_type="text/html", body=page)
            self.misses.append((rq.resource_type, rq.url))
            return await route.abort()
        hdr = {"access-control-allow-origin": rq.headers.get("origin", "*"), "access-control-allow-credentials": "true"}
        if r["mime"]:
            hdr["content-type"] = r["mime"]
        if 300 <= r["status"] < 400:
            hdr["location"] = r["loc"]
            return await route.fulfill(status=r["status"], headers=hdr)
        data = self.proj.get_blob(r["hash"]) if r["hash"] else b""
        await route.fulfill(status=r["status"], headers=hdr, body=data or b"")


# ------------------------------------------------------------------ local index / viewer server
def page_shell(title, body):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>%s</title>"
            "<style>body{font:15px system-ui;max-width:1100px;margin:1.5rem auto;padding:0 1rem}details{margin:.3rem 0 .3rem 1rem}"
            "details.t{border:1px solid #ddd;border-radius:8px;padding:.3rem .8rem;margin-left:0}summary{cursor:pointer;font-weight:600}"
            "a{color:#075b9a;text-decoration:none}mark{background:#fff0a6}.w{color:#9a5b00;font-size:12px}td,th{border:1px solid #ccc;padding:4px;font-size:13px}"
            "table{border-collapse:collapse}.ad{background:#e6f6e6}.rm{background:#fde8e8}input[type=search]{width:70%%;padding:9px}</style>"
            "<p><a href='/'>Contents</a> · <a href='/updates'>Updates</a> · <a href='/report'>Report</a> · <a href='/stats'>Size</a></p>%s") % (H.escape(title), body)


def render_index(proj):
    rows = proj.db.execute("SELECT * FROM pages WHERE status='saved' ORDER BY spec,topic,sub,seq").fetchall()
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in rows:
        tree[r["spec"] or "General"][r["topic"] or ""][r["sub"] or ""].append(r)

    def li(r):
        w = " <span class=w title='%s'>⚠ incomplete</span>" % H.escape(r["incomplete"]) if r["incomplete"] else ""
        v = "" if r["verified"] or r["incomplete"] else " <span class=w>unverified</span>"
        return "<li><a href='%s'>%s</a>%s%s</li>" % (H.escape(r["final_url"] or r["url"]), H.escape(r["title"] or r["url"]), w, v)
    out = []
    for sp in sorted(tree):
        inner = ""
        for to in sorted(tree[sp]):
            subs = ""
            for su in sorted(tree[sp][to]):
                items = "<ul>%s</ul>" % "".join(li(r) for r in tree[sp][to][su])
                subs += ("<details><summary>%s</summary>%s</details>" % (H.escape(su), items)) if su else items
            inner += ("<details><summary>%s</summary>%s</details>" % (H.escape(to), subs)) if to else subs
        out.append("<details class=t open><summary>%s</summary>%s</details>" % (H.escape(sp), inner))
    form = "<form action=/search><input type=search name=q placeholder='Search every saved page…' autofocus> <button>Search</button></form>"
    return page_shell(proj.cfg.get("title", proj.name), "<h1>%s</h1>%s<p>%d page(s)</p>%s" % (H.escape(proj.cfg.get("title", proj.name)), form, len(rows), "".join(out)))


def render_search(proj, q):
    hits = proj.search(q)
    body = "<form action=/search><input type=search name=q value='%s'> <button>Search</button></form><p>%d result(s)</p>" % (H.escape(q), len(hits))
    for h in hits:
        s = H.escape(h["snip"]).replace("\x01", "<mark>").replace("\x02", "</mark>")
        body += "<div><a href='%s'>%s</a><br><small>%s</small><div>%s</div><hr></div>" % (H.escape(h["url"]), H.escape(h["title"] or h["url"]), H.escape(h["url"]), s)
    return page_shell("Search", body)


def render_updates(proj):
    body = "<h1>Topic updates</h1>"
    rows = proj.db.execute("SELECT * FROM changes ORDER BY t DESC LIMIT 200").fetchall()
    if not rows:
        body += "<p>No changes recorded yet. Run the update check while online.</p>"
    for r in rows:
        add, rem = json.loads(r["added"] or "[]"), json.loads(r["removed"] or "[]")
        body += "<h3>%s <small>%s</small></h3><p><a href='%s'>%s</a></p>" % (H.escape(r["title"] or r["url"]), r["t"], H.escape(r["url"]), H.escape(r["url"]))
        body += "".join("<div class=rm>− %s</div>" % H.escape(x) for x in rem) + "".join("<div class=ad>+ %s</div>" % H.escape(x) for x in add)
    return page_shell("Updates", body)


def lists(proj):
    q = lambda w, *a: [dict(r) for r in proj.db.execute(w, a)]
    return {
        "Downloaded": q("SELECT * FROM pages WHERE status='saved'"),
        "Pending": q("SELECT * FROM pages WHERE status IN ('pending','running','needs_login','needs_decision')"),
        "Redirected (chains recorded)": q("SELECT p.* FROM pages p JOIN redirects r ON r.page_url=p.url"),
        "Failed": q("SELECT * FROM pages WHERE status='failed'"),
        "Passed": q("SELECT * FROM pages WHERE status='passed'"),
        "Redownloaded": q("SELECT * FROM pages WHERE redownloads>0"),
        "Incomplete JS/assets": q("SELECT * FROM pages WHERE status='saved' AND incomplete IS NOT NULL"),
        "Saved but not yet verified offline": q("SELECT * FROM pages WHERE status='saved' AND verified=0 AND incomplete IS NULL"),
    }


def render_report(proj):
    body = "<h1>Ledger</h1>"
    for name, rows in lists(proj).items():
        body += "<h2>%s (%d)</h2><table><tr><th>URL</th><th>Parent</th><th>Discovered via</th><th>Saved</th><th>Tries</th><th>Note</th></tr>" % (name, len(rows))
        for r in rows[:1000]:
            note = r["error"] or r["incomplete"] or r["reason"] or ""
            body += "<tr>" + "".join("<td>%s</td>" % H.escape(str(x or "")) for x in (r["url"], r["parent"], r["via"], r["saved"], r["attempts"], note)) + "</tr>"
        body += "</table>"
    return page_shell("Report", body)


def store_stats(proj):
    n = tot = 0
    for p in proj.store.rglob("*"):
        if p.is_file():
            n += 1
            tot += p.stat().st_size
    raw = proj.db.execute("SELECT COALESCE(SUM(r.size),0) FROM page_res pr JOIN res r ON r.key=pr.key").fetchone()[0]
    uniq = proj.db.execute("SELECT COALESCE(SUM(size),0) FROM (SELECT DISTINCT hash,size FROM res)").fetchone()[0]
    return n, tot, raw, uniq


def render_stats(proj):
    n, tot, raw, uniq = store_stats(proj)
    mb = lambda b: "%.1f MB" % (b / 1048576)
    return page_shell("Size", "<h1>Storage</h1><p>%d unique files stored, %s on disk.</p><p>If every page kept its own copy of each file: %s. After storing each file once: %s uncompressed; %s after compression.</p>" % (n, mb(tot), mb(raw), mb(uniq), mb(tot)))


def make_handler(proj):
    class Hd(BaseHTTPRequestHandler):
        db = None

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query).get("q", [""])[0]
            local = Project(proj.name).open()
            try:
                page = {"/": lambda: render_index(local), "/search": lambda: render_search(local, q), "/updates": lambda: render_updates(local),
                        "/report": lambda: render_report(local), "/stats": lambda: render_stats(local)}.get(u.path)
                if not page:
                    self.send_error(404)
                    return
                body = page().encode("utf-8")
            finally:
                local.db.close()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Hd


async def view(proj):
    from playwright.async_api import async_playwright
    if proj.cfg.get("pass_hash"):
        pw = getpass.getpass("Passcode: ")
        if hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(proj.cfg["pass_salt"]), 200000).hex() != proj.cfg["pass_hash"]:
            sys.exit("Wrong passcode.")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proj))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rp = Replayer(proj)
    rp.local = {f"127.0.0.1:{port}"}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(proj.dir / "viewer-profile"), channel="msedge", headless=False,
                                                         service_workers="block", no_viewport=True)
        await ctx.route("**/*", rp.handle)
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto(f"http://127.0.0.1:{port}/")
        print("Offline viewer open. Close the Edge window to exit.")
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        await closed.wait()


async def verify(proj, limit=0, url=None):
    """Replay each saved page with no internet; record script errors and missing files."""
    from playwright.async_api import async_playwright
    rp = Replayer(proj)
    q = "SELECT * FROM pages WHERE status='saved'" + (" AND url=?" if url else "")
    rows = proj.db.execute(q, (url,) if url else ()).fetchall()
    if limit:
        rows = rows[:limit]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(proj.dir / "viewer-profile"), channel="msedge", headless=True, service_workers="block")
        await ctx.route("**/*", rp.handle)
        for r in rows:
            rp.misses.clear()
            errs = []
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: errs.append("script error: " + str(e)[:160]))
            try:
                await pg.goto(r["final_url"] or r["url"], wait_until="load", timeout=45000)
                await asyncio.sleep(1.0)
            except Exception as e:
                errs.append(f"load: {e}")
            await pg.close()
            miss = [m for m in rp.misses if m[0] in ("script", "stylesheet", "document", "xhr", "fetch", "font", "image") and not any(t in m[1] for t in TRACKERS)]
            if errs or miss:
                why = f"offline check: {len(errs)} script error(s), {len(miss)} missing file(s)"
                proj.db.execute("UPDATE pages SET incomplete=?, verified=0 WHERE url=?", (why, r["url"]))
                proj.ev(r["url"], "VERIFY_FAIL", "\n".join(errs[:10] + [f"missing {t}: {u}" for t, u in miss[:20]]))
                print("  ✗", r["url"], "-", why)
            else:
                proj.db.execute("UPDATE pages SET incomplete=NULL, verified=1 WHERE url=?", (r["url"],))
                print("  ✓", r["url"])
            proj.db.commit()
        await ctx.close()


def write_updates(proj):
    out = proj.dir / "updates.html"
    out.write_text(render_updates(proj), "utf-8")
    n = proj.db.execute("SELECT COUNT(*) FROM changes WHERE t>=?", (time.strftime("%Y-%m-%d"),)).fetchone()[0]
    print(f"Updates report: {out}  ({n} changed page(s) today)")


# ------------------------------------------------------------------ commands
def read_rule_file(path):
    if not path:
        return []
    lines = Path(path).read_text("utf-8-sig").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def cmd_init(a):
    p = Project(a.name)
    if p.cfg_path.exists() and not a.force:
        sys.exit("Project exists. Use --force to overwrite its settings.")
    u = norm(a.url)
    if not u:
        sys.exit("Give a full http(s) URL.")
    p.cfg = {"start": u, "host": urlparse(u).netloc, "title": a.title or a.name, "max_depth": a.depth, "max_pages": a.max_pages,
             "delay": a.delay, "retries": 3, "timeout": 60, "include": a.include, "exclude": a.exclude, "external_hops": a.external_hops,
             "allow_domains": a.allow_domain or [], "also_internal": a.also_internal or [], "files": a.files, "block_trackers": not a.keep_trackers,
             "max_resource_mb": a.max_resource_mb, "redirects": a.redirects, "redirect_allow": [], "redirect_block": [],
             "skip": [rule_norm(x) for x in (a.skip or [])], "only": [rule_norm(x) for x in (a.only or []) + read_rule_file(a.only_file)]}
    p.open()
    queue_link(p, u, 0, 0, None, "start URL")
    p.db.commit()
    p.save_cfg()
    print(f"Project '{a.name}' created in {p.dir}\n Next:  login {a.name}   (if the site needs an account)   then   crawl {a.name}")


def cmd_set(a):
    p = must(a.name)
    for k in ("max_depth", "max_pages", "delay", "include", "exclude", "external_hops", "title", "timeout", "retries", "max_resource_mb", "redirects"):
        v = getattr(a, k, None)
        if v is not None:
            p.cfg[k] = v
    if a.allow_domain:
        p.cfg["allow_domains"] = a.allow_domain
    p.save_cfg()
    print("Settings updated:", json.dumps({k: p.cfg.get(k) for k in ("max_depth", "max_pages", "delay", "external_hops", "allow_domains", "include", "exclude")}))


def cmd_login(a):
    from playwright.sync_api import sync_playwright
    p = must(a.name)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(p.dir / "profile"), channel="msedge", headless=False, no_viewport=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(p.cfg["start"])
        print("Sign in normally in the Edge window. When finished, CLOSE the window.")
        try:
            while ctx.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    print("Session saved in the project's private profile.")


def cmd_crawl(a):
    p = must(a.name)
    asyncio.run(crawl(p, show=a.show, workers=a.workers))


def cmd_update(a):
    p = must(a.name)
    asyncio.run(crawl(p, show=a.show, workers=a.workers, update=True))


def cmd_status(a):
    p = must(a.name)
    L = lists(p)
    for k, v in L.items():
        print(f"{k:38} {len(v)}")
    if a.list:
        key = {x.lower().split()[0]: x for x in L}.get(a.list.lower())
        for r in (L.get(key, []) if key else []):
            print(" -", r["url"], "|", r["error"] or r["incomplete"] or r["reason"] or "")
    n, tot, raw, uniq = store_stats(p)
    print(f"\nStorage: {tot / 1048576:.1f} MB on disk in {n} unique files (would be {raw / 1048576:.1f} MB without sharing).")


def cmd_pass(a):
    p = must(a.name)
    u = norm(a.url)
    p.db.execute("UPDATE pages SET status='passed', reason=? WHERE url=?", (a.reason or "passed manually", u))
    p.ev(u, "PASSED", a.reason)
    p.db.commit()
    print("Passed:", u)


def cmd_restore(a):
    p = must(a.name)
    u = norm(a.url)
    p.db.execute("UPDATE pages SET status='pending', attempts=0, reason=NULL WHERE url=? AND status='passed'", (u,))
    p.ev(u, "RESTORED", "")
    p.db.commit()
    print("Restored to the queue:", u)


def cmd_redownload(a):
    p = must(a.name)
    cond, args = [], []
    if a.url:
        cond.append("url=?"), args.append(norm(a.url))
    if a.failed:
        cond.append("status='failed'")
    if a.incomplete:
        cond.append("(status='saved' AND incomplete IS NOT NULL)")
    if not cond:
        sys.exit("Choose --url URL, --failed or --incomplete.")
    n = p.db.execute(f"UPDATE pages SET status='pending', attempts=0, error=NULL, redownloads=redownloads+1 WHERE {' OR '.join(cond)}", args).rowcount
    p.db.commit()
    print(f"{n} page(s) queued. Run: crawl {a.name}   (only these pages are fetched again)")


def cmd_verify(a):
    asyncio.run(verify(must(a.name), a.limit, norm(a.url) if a.url else None))


def cmd_view(a):
    asyncio.run(view(must(a.name)))


def cmd_search(a):
    p = must(a.name)
    for h in p.search(" ".join(a.q)):
        print("-", h["title"], "\n  ", h["url"], "\n  ", h["snip"].replace("\x01", "[").replace("\x02", "]").replace("\n", " "))


def cmd_lock(a):
    p = must(a.name)
    pw = getpass.getpass("New passcode (6+ chars): ")
    if len(pw) < 6:
        sys.exit("Too short.")
    salt = os.urandom(16)
    p.cfg["pass_salt"], p.cfg["pass_hash"] = salt.hex(), hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000).hex()
    p.save_cfg()
    print("Viewer passcode set. (Files on disk are not encrypted; use an encrypted drive/ZIP for that.)")



def cmd_skip(a):
    p = must(a.name)
    for r in a.rules:
        r = rule_norm(r)
        if r not in p.cfg.setdefault("skip", []):
            p.cfg["skip"].append(r)
    n = apply_rules(p)
    done = [r["url"] for r in p.db.execute("SELECT url FROM pages WHERE status='saved'") if any(rule_match(x, r["url"]) for x in a.rules)]
    print(f"Skip rules: {p.cfg['skip']}\n{n} pending page(s) now held back.")
    if done:
        print(f"{len(done)} page(s) matching this rule were ALREADY downloaded; they are kept on disk and not removed.")
    print(f"Undo any rule with:  unrule {a.name} RULE")


def cmd_only(a):
    p = must(a.name)
    rules = [rule_norm(r) for r in (a.rules or []) + read_rule_file(a.file)]
    if a.replace:
        p.cfg["only"] = []
    for r in rules:
        if r not in p.cfg.setdefault("only", []):
            p.cfg["only"].append(r)
    n = apply_rules(p)
    print(f"Only-rules: {p.cfg['only'] or 'none (everything allowed)'}\n{n} pending page(s) held back. Note: the start page is always kept.")


def cmd_unrule(a):
    p = must(a.name)
    r = rule_norm(a.rule)
    for key in ("skip", "only"):
        p.cfg[key] = [x for x in p.cfg.get(key, []) if x != r]
    apply_rules(p)
    print(f"Removed rule {r}. Held pages it caused are queued again (run: crawl {a.name}).")


def cmd_rules(a):
    p = must(a.name)
    print("Skip (excluded):", p.cfg.get("skip") or "none")
    print("Only (download just these):", p.cfg.get("only") or "none, everything is allowed")
    held = p.db.execute("SELECT reason, COUNT(*) FROM pages WHERE status='passed' AND reason IN ('excluded by your skip rule','outside your only-rules') GROUP BY reason").fetchall()
    for reason, n in held:
        print(f"Held by rules: {n} page(s) - {reason}")


def cmd_redirects(a):
    p = must(a.name)
    print(f"Redirect policy: {p.cfg.get('redirects', 'allow')}   allowed: {p.cfg.get('redirect_allow', [])}   blocked: {p.cfg.get('redirect_block', [])}\n")
    rows = p.db.execute("SELECT * FROM redirect_hosts ORDER BY n DESC").fetchall()
    print("Other websites reached by redirects (host, times seen, current decision, example):")
    for r in rows:
        print(f"  {r['host']:35} {r['n']:5}  {redirect_decision(p.cfg, r['host']):6} {r['example']}")
    if not rows:
        print("  (none seen yet)")
    held = p.db.execute("SELECT url,reason FROM pages WHERE status IN ('needs_decision') OR (status='passed' AND reason LIKE 'redirects to %')").fetchall()
    if held:
        print(f"\n{len(held)} page(s) held because of a redirect decision:")
        for r in held:
            print("  -", r["url"], "|", r["reason"])
        print(f"\nTo include a site:  allow-domain {a.name} HOST      To exclude it:  block-domain {a.name} HOST")
    for r in p.db.execute("SELECT * FROM redirects ORDER BY t DESC LIMIT 15"):
        print("\n", r["page_url"], "\n   =>", " -> ".join(json.loads(r["chain"]))[:300])


def _domain_decision(a, allow):
    p = must(a.name)
    h = a.host.lower()
    p.cfg.setdefault("redirect_allow", [])
    p.cfg.setdefault("redirect_block", [])
    (p.cfg["redirect_allow"] if allow else p.cfg["redirect_block"]).append(h) if h not in (p.cfg["redirect_allow"] if allow else p.cfg["redirect_block"]) else None
    other = p.cfg["redirect_block"] if allow else p.cfg["redirect_allow"]
    if h in other:
        other.remove(h)
    p.save_cfg()
    n = 0
    if allow:
        n = p.db.execute("UPDATE pages SET status='pending', attempts=0, reason=NULL WHERE status IN ('needs_decision','passed') AND reason LIKE ?", (f"redirects to {h}%",)).rowcount
    p.db.commit()
    print(f"{h}: redirects {'ALLOWED' if allow else 'BLOCKED'}." + (f" {n} held page(s) re-queued. Run: crawl {a.name}" if allow else ""))


def cmd_allow_domain(a):
    _domain_decision(a, True)


def cmd_block_domain(a):
    _domain_decision(a, False)


def cmd_creds(a):
    p = must(a.name)
    store = CredStore(p)
    if a.action == "clear":
        store.clear()
        print("Saved sign-in removed.")
    elif a.action == "show":
        c = store.load()
        print("No saved sign-in." if not c else f"Saved sign-in for user '{c['u']}' (password hidden). Allowed sign-in domains: {[p.cfg['host']] + c.get('login_domains', [])}")
    else:
        u = input("Username or email for this site: ").strip()
        pw = getpass.getpass("Password (not shown, not logged): ")
        if not u or not pw:
            sys.exit("Both are required.")
        d = {"u": u, "p": pw, "login_url": a.login_url or "", "login_domains": a.login_domain or []}
        store.save(d)
        print(f"Saved, encrypted by Windows for your account only. Credentials are only ever typed into: {[p.cfg['host']] + d['login_domains']}")


def cmd_export(a):
    p = must(a.name)
    out = Path(a.out)
    with zipfile.ZipFile(out, "w") as z:
        for f in p.dir.rglob("*"):
            if f.is_file() and f.name != "creds.bin" and "profile" not in f.relative_to(p.dir).parts[:1] and "viewer-profile" not in f.relative_to(p.dir).parts[:1]:
                z.write(f, f.relative_to(HOME), compress_type=zipfile.ZIP_STORED if f.suffix in (".gz", ".bin") else zipfile.ZIP_DEFLATED)
    print("Exported", out, f"({out.stat().st_size / 1048576:.1f} MB). Login profile excluded.")


def build_parser():
    ap = argparse.ArgumentParser(prog="oarchiver", description="Offline Archiver 2.0 - save websites you are allowed to keep, browse them offline.")
    sp = ap.add_subparsers(dest="cmd")

    def cmd(name, func, help, *args):
        s = sp.add_parser(name, help=help)
        s.add_argument("name", help="project name")
        for flags, kw in args:
            s.add_argument(*flags, **kw)
        s.set_defaults(f=func)
        return s

    A = lambda *flags, **kw: (flags, kw)
    s = cmd("init", cmd_init, "create a project", A("url"), A("--title"),
            A("--depth", type=int, default=3, help="link levels to follow (0 = unlimited)"),
            A("--max-pages", type=int, default=100, help="0 = unlimited; your choice"),
            A("--delay", type=float, default=2.0, help="seconds between requests per website"),
            A("--include", help="regex: only URLs matching"), A("--exclude", help="regex: skip URLs matching"),
            A("--external-hops", type=int, default=0, help="follow links to other sites this many hops"),
            A("--allow-domain", action="append", help="external site you allow (repeatable)"),
            A("--also-internal", action="append", help="extra hostnames treated as part of the site"),
            A("--files", action="store_true", help="also download large file types (zip, mp4 ...)"),
            A("--keep-trackers", action="store_true", help="do not block analytics/ad hosts"),
            A("--max-resource-mb", type=int, default=25), A("--force", action="store_true"),
            A("--skip", action="append", help="exclude a page, a whole URL section, a topic name (e.g. neuro) or re:REGEX (repeatable)"),
            A("--only", action="append", help="download ONLY URLs under this URL, topic name or re:REGEX (repeatable)"),
            A("--only-file", help="text file with one --only rule per line"),
            A("--redirects", choices=["allow", "ask", "listed", "block"], default="allow",
              help="pages that redirect to other websites: allow (default) | ask (hold for your decision) | listed (only allowed domains) | block"))
    cmd("set", cmd_set, "change project limits", A("--max-depth", dest="max_depth", type=int), A("--max-pages", dest="max_pages", type=int),
        A("--delay", type=float), A("--include"), A("--exclude"), A("--external-hops", dest="external_hops", type=int),
        A("--allow-domain", action="append"), A("--title"), A("--timeout", type=int), A("--retries", type=int),
        A("--max-resource-mb", dest="max_resource_mb", type=int), A("--redirects", choices=["allow", "ask", "listed", "block"]))
    cmd("login", cmd_login, "sign in once, in an Edge window the program opens")
    cmd("view", cmd_view, "open the offline viewer (works with no internet)")
    cmd("lock", cmd_lock, "set a passcode for the viewer")
    cmd("crawl", cmd_crawl, "download or resume", A("--workers", type=int, default=2), A("--show", action="store_true", help="show the browser window"))
    cmd("update", cmd_update, "online: re-check saved pages and report changed topics", A("--workers", type=int, default=2), A("--show", action="store_true"))
    cmd("status", cmd_status, "six-list ledger and storage size", A("--list", help="downloaded|pending|failed|passed|redownloaded|incomplete"))
    cmd("pass", cmd_pass, "skip a page with a reason", A("url"), A("--reason"))
    cmd("restore", cmd_restore, "put a passed page back in the queue", A("url"))
    cmd("redownload", cmd_redownload, "refetch chosen pages only", A("--url"), A("--failed", action="store_true"), A("--incomplete", action="store_true"))
    cmd("verify", cmd_verify, "replay pages offline; flag script errors and missing files", A("--url"), A("--limit", type=int, default=0))
    cmd("search", cmd_search, "full-text search", A("q", nargs="+"))
    cmd("skip", cmd_skip, "exclude pages, sections or topics from the download", A("rules", nargs="+", help="URL, topic name or re:REGEX"))
    cmd("only", cmd_only, "download ONLY these URLs / topics (replaces nothing unless --replace)", A("rules", nargs="*"),
        A("--file", help="text file with one rule per line"), A("--replace", action="store_true", help="clear existing only-rules first"))
    cmd("unrule", cmd_unrule, "remove a skip or only rule", A("rule"))
    cmd("rules", cmd_rules, "show the skip and only rules")
    cmd("redirects", cmd_redirects, "show every other website reached by redirects and what was decided")
    cmd("allow-domain", cmd_allow_domain, "allow redirects to a website and re-queue pages held for it", A("host"))
    cmd("block-domain", cmd_block_domain, "refuse redirects to a website", A("host"))
    cmd("creds", cmd_creds, "save/clear the sign-in used for automatic re-login", A("action", choices=["set", "clear", "show"]),
        A("--login-url", help="page that shows the sign-in form"), A("--login-domain", action="append", help="extra domain allowed to receive this sign-in (e.g. a single-sign-on host)"))
    cmd("export", cmd_export, "ZIP the archive (login profile excluded)", A("out"))
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "f", None):
        ap.print_help()
        return
    HOME.mkdir(parents=True, exist_ok=True)
    a.f(a)


if __name__ == "__main__":
    main()