from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse, unquote

from PySide6.QtCore import QThread, Signal, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import *
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from .engine import Archiver


def home() -> Path:
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OfflineArchiver"
    path.mkdir(parents=True, exist_ok=True)
    return path


class EmbeddedBrowser(QWidget):
    """Persistent in-app Chromium browser. Cookies are bridged to the capture worker."""
    def __init__(self, data_dir: Path, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.cookies: dict[str, dict] = {}
        self.profile = QWebEngineProfile("OfflineArchiver", self)
        storage = data_dir / "embedded-browser"
        storage.mkdir(parents=True, exist_ok=True)
        self.profile.setPersistentStoragePath(str(storage / "storage"))
        self.profile.setCachePath(str(storage / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.profile.cookieStore().cookieAdded.connect(self._cookie_added)
        self.profile.cookieStore().loadAllCookies()

        layout = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.back = QPushButton("←")
        self.forward = QPushButton("→")
        self.reload = QPushButton("↻")
        self.address = QLineEdit("https://www.uptodate.com/contents/table-of-contents")
        self.address.setPlaceholderText("Enter an authorized website URL")
        self.go = QPushButton("Go")
        self.session = QLabel("Persistent browser profile")
        for w in (self.back, self.forward, self.reload, self.address, self.go):
            nav.addWidget(w)
        nav.addWidget(self.session)
        layout.addLayout(nav)
        actions = QHBoxLayout()
        self.start_download = QPushButton("Start / Resume this page")
        self.pause_download = QPushButton("Pause")
        self.stop_download = QPushButton("Stop safely")
        self.browser_progress = QProgressBar()
        self.browser_status = QLabel("Sign in here, then start archiving without leaving this tab.")
        actions.addWidget(self.start_download)
        actions.addWidget(self.pause_download)
        actions.addWidget(self.stop_download)
        actions.addWidget(self.browser_progress, 1)
        layout.addLayout(actions)
        layout.addWidget(self.browser_status)
        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        layout.addWidget(self.view, 1)
        self.back.clicked.connect(self.view.back)
        self.forward.clicked.connect(self.view.forward)
        self.reload.clicked.connect(self.view.reload)
        self.go.clicked.connect(self.navigate)
        self.address.returnPressed.connect(self.navigate)
        self.view.urlChanged.connect(lambda u: self.address.setText(u.toString()))
        self.view.loadStarted.connect(lambda: self.session.setText("Loading…"))
        self.view.loadFinished.connect(lambda ok: self.session.setText("Browser ready" if ok else "Page load failed"))
        self.view.load(QUrl(self.address.text()))

    def navigate(self):
        value = self.address.text().strip()
        if not urlparse(value).scheme:
            value = "https://" + value
        self.view.load(QUrl(value))

    def _cookie_added(self, cookie):
        try:
            name = bytes(cookie.name()).decode("utf-8", "replace")
            value = bytes(cookie.value()).decode("utf-8", "replace")
            domain = cookie.domain()
            path = cookie.path() or "/"
            item = {"name": name, "value": value, "domain": domain, "path": path,
                    "secure": cookie.isSecure(), "httpOnly": cookie.isHttpOnly()}
            expiry = cookie.expirationDate()
            if expiry.isValid():
                item["expires"] = expiry.toSecsSinceEpoch()
            key = (name, domain, path)
            self.cookies[str(key)] = item
        except Exception:
            pass

    def cookie_snapshot(self):
        return list(self.cookies.values())


class Task(QThread):
    status = Signal(str)
    progress = Signal(int, int, str)
    page = Signal(int, str, str)

    def __init__(self, data_dir, url, opts):
        super().__init__()
        self.data_dir, self.url, self.opts = data_dir, url, opts
        self.stop_event, self.pause_event = threading.Event(), threading.Event()
        self.engine = Archiver(data_dir, self.emit_event, self.stop_event, self.pause_event)

    def emit_event(self, kind, *args):
        if kind == "status":
            self.status.emit(str(args[0]))
        elif kind == "progress":
            self.progress.emit(*args)
        elif kind == "page":
            self.page.emit(*args)

    def run(self):
        try:
            self.engine.crawl(self.url, **self.opts)
        except Exception as exc:
            self.status.emit("Error: " + str(exc))
        finally:
            if not self.engine.halt_reason:
                self.status.emit("Task finished.")

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Offline Archiver")
        self.resize(1480, 940)
        self.app_dir = home()
        self.site_map_path = self.app_dir / "site-storage.json"
        self.site_root = self._site_root_for("https://www.uptodate.com")
        self.lib = Archiver(self.site_root, lambda *a: None, threading.Event(), threading.Event()).library
        self.task: Task | None = None
        self._build_ui()
        self.refresh_all()

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 12)
        header = QHBoxLayout()
        title = QLabel("Offline Archiver")
        title.setStyleSheet("font-size:26px;font-weight:700;color:#16345b")
        header.addWidget(title)
        header.addStretch()
        self.status = QLabel("Ready")
        header.addWidget(self.status)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.browser = EmbeddedBrowser(self.app_dir)
        self.tabs.addTab(self.browser, "Browser & Sign-in")
        self.browser.start_download.clicked.connect(self.start_from_browser)
        self.browser.pause_download.clicked.connect(self.pause_task)
        self.browser.stop_download.clicked.connect(self.stop_task)
        self.tabs.addTab(self._dashboard_tab(), "Dashboard")
        self.tabs.addTab(self._queue_tab(), "Queue & Six-list ledger")
        self.tabs.addTab(self._library_tab(), "Search & Offline library")
        self.tabs.addTab(self._reader_tab(), "Offline Reader")
        self.tabs.addTab(self._diagnostics_tab(), "Diagnostics & Export")

        self.setCentralWidget(root)
        self.setStyleSheet("""
          QMainWindow,QWidget{background:#f4f6fa;color:#1e293b;font-family:Segoe UI}
          QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox{background:white;border:1px solid #cbd5e1;border-radius:6px;padding:7px}
          QPushButton{background:#e5edf8;border:0;border-radius:6px;padding:9px 12px;font-weight:600}
          QPushButton:hover{background:#d2e0f3}
          QTabWidget::pane{background:#f4f6fa;border:1px solid #d9e2ef}
          QTabBar::tab{padding:11px 16px;background:#e8eef7;border-top-left-radius:5px;border-top-right-radius:5px}
          QTabBar::tab:selected{background:#fff;color:#1557a6;font-weight:700}
          QTableWidget{background:white;gridline-color:#e8edf3}
          QHeaderView::section{background:#edf2f8;padding:8px;border:0;font-weight:600}
          QProgressBar{height:16px;border:0;background:#e2e8f0;border-radius:6px;text-align:center}
          QProgressBar::chunk{background:#2563eb;border-radius:6px}
        """)

    def _dashboard_tab(self):
        w = QWidget(); layout = QVBoxLayout(w)
        intro = QLabel("Archive authorized pages using the embedded browser session. Sign in manually; the downloader never submits credentials.")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.metrics = {}
        grid = QGridLayout()
        labels = [("downloaded","Downloaded"),("pending","Pending"),("failed","Failed"),("passed","Passed"),
                  ("redownloaded","Redownloaded"),("incomplete","Incomplete / JS issues"),("attempted","Attempted"),("assets","Assets")]
        for i,(key,label) in enumerate(labels):
            card = QFrame(); card.setStyleSheet("QFrame{background:white;border:1px solid #e1e8f0;border-radius:9px}")
            box = QVBoxLayout(card); value = QLabel("0"); value.setAlignment(Qt.AlignCenter)
            value.setStyleSheet("font-size:25px;font-weight:700;color:#1d4f91")
            caption = QLabel(label); caption.setAlignment(Qt.AlignCenter)
            box.addWidget(value); box.addWidget(caption); self.metrics[key] = value
            grid.addWidget(card, i//4, i%4)
        layout.addLayout(grid)
        form = QGridLayout()
        self.url = QLineEdit("https://www.uptodate.com/contents/table-of-contents")
        self.url.setPlaceholderText("Authorized starting URL")
        self.storage_label = QLabel(str(self.site_root))
        self.storage_btn = QPushButton("Choose site storage folder…")
        self.storage_btn.clicked.connect(self.choose_site_storage)
        self.url.editingFinished.connect(self.activate_site)
        form.addWidget(QLabel("Site storage"),2,0)
        form.addWidget(self.storage_label,2,1,1,4)
        form.addWidget(self.storage_btn,2,5)
        self.pages = QSpinBox(); self.pages.setRange(1,10000); self.pages.setValue(500)
        self.depth = QSpinBox(); self.depth.setRange(0,20); self.depth.setValue(0)
        self.delay = QDoubleSpinBox(); self.delay.setRange(3,120); self.delay.setValue(5.0); self.delay.setSuffix(" sec")
        form.addWidget(QLabel("Start URL"),0,0); form.addWidget(self.url,0,1,1,5)
        form.addWidget(QLabel("Max pages / run"),1,0); form.addWidget(self.pages,1,1)
        form.addWidget(QLabel("Link depth (0 = unlimited)"),1,2); form.addWidget(self.depth,1,3)
        form.addWidget(QLabel("Delay between pages"),1,4); form.addWidget(self.delay,1,5)
        layout.addLayout(form)
        controls=QHBoxLayout()
        self.start_btn=QPushButton("Start / Resume download"); self.pause_btn=QPushButton("Pause after current page")
        self.stop_btn=QPushButton("Stop safely"); self.finish_btn=QPushButton("Finish & refresh index")
        for b in (self.start_btn,self.pause_btn,self.stop_btn,self.finish_btn): controls.addWidget(b)
        layout.addLayout(controls)
        self.progress=QProgressBar(); layout.addWidget(self.progress)
        self.start_btn.clicked.connect(self.start_crawl); self.pause_btn.clicked.connect(self.pause_task)
        self.stop_btn.clicked.connect(self.stop_task); self.finish_btn.clicked.connect(self.refresh_all)
        note=QLabel("Safety: use one account session, low request rates, and only content you are permitted to save. HTTP 401/403/429 pauses the crawl safely and preserves the remaining queue; resolve the account or provider limit before resuming. Use a respectful delay between pages.")
        note.setWordWrap(True); note.setStyleSheet("color:#8a4b12;padding:8px")
        layout.addWidget(note); layout.addStretch()
        return w

    def _queue_tab(self):
        w=QWidget(); layout=QVBoxLayout(w)
        row=QHBoxLayout()
        self.ledger_filter=QComboBox(); self.ledger_filter.addItems(["all","pending","failed","passed","saved","running"])
        self.ledger_filter.currentTextChanged.connect(self.refresh_queue)
        self.queue_search=QLineEdit(); self.queue_search.setPlaceholderText("Filter URL or failure reason")
        self.queue_search.textChanged.connect(self.refresh_queue)
        row.addWidget(QLabel("List")); row.addWidget(self.ledger_filter); row.addWidget(self.queue_search,1)
        layout.addLayout(row)
        self.queue_table=QTableWidget(0,5); self.queue_table.setHorizontalHeaderLabels(["URL","State","Depth","Attempts","Reason / last error"])
        self.queue_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch)
        layout.addWidget(self.queue_table,1)
        add=QGroupBox("Add / pass / redownload URLs"); form=QGridLayout(add)
        self.add_url=QLineEdit(); self.add_url.setPlaceholderText("https://example.com/topic")
        self.add_btn=QPushButton("Add to queue")
        self.pass_url=QLineEdit(); self.pass_url.setPlaceholderText("URL to skip")
        self.pass_reason=QLineEdit(); self.pass_reason.setPlaceholderText("Reason (optional)")
        self.pass_btn=QPushButton("Pass / skip URL")
        self.redownload_url=QLineEdit(); self.redownload_url.setPlaceholderText("URL to force redownload")
        self.redownload_btn=QPushButton("Redownload this page")
        form.addWidget(self.add_url,0,0,1,2); form.addWidget(self.add_btn,0,2)
        form.addWidget(self.pass_url,1,0); form.addWidget(self.pass_reason,1,1); form.addWidget(self.pass_btn,1,2)
        form.addWidget(self.redownload_url,2,0,1,2); form.addWidget(self.redownload_btn,2,2)
        layout.addWidget(add)
        actions=QHBoxLayout()
        self.restore_btn=QPushButton("Restore passed URLs"); self.retry_btn=QPushButton("Retry failed pages")
        self.retry_incomplete_btn=QPushButton("Retry incomplete JS pages")
        for b in (self.restore_btn,self.retry_btn,self.retry_incomplete_btn): actions.addWidget(b)
        layout.addLayout(actions)
        self.add_btn.clicked.connect(self.add_to_queue); self.pass_btn.clicked.connect(self.pass_url_action)
        self.redownload_btn.clicked.connect(self.redownload_action); self.restore_btn.clicked.connect(self.restore_passed)
        self.retry_btn.clicked.connect(self.retry_failed); self.retry_incomplete_btn.clicked.connect(self.retry_incomplete)
        return w

    def _library_tab(self):
        w=QWidget(); layout=QVBoxLayout(w)
        row=QHBoxLayout(); self.search=QLineEdit(); self.search.setPlaceholderText("Search saved pages and topic text")
        self.search_btn=QPushButton("Search"); self.refresh_btn=QPushButton("Show all saved pages")
        row.addWidget(self.search,1); row.addWidget(self.search_btn); row.addWidget(self.refresh_btn); layout.addLayout(row)
        self.page_table=QTableWidget(0,5); self.page_table.setHorizontalHeaderLabels(["Title","URL","HTTP","Saved","Offline completeness"])
        self.page_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.page_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        self.page_table.cellDoubleClicked.connect(self.open_page); layout.addWidget(self.page_table,1)
        self.search_btn.clicked.connect(self.search_pages); self.search.returnPressed.connect(self.search_pages)
        self.refresh_btn.clicked.connect(self.refresh_pages)
        return w

    def _reader_tab(self):
        w = QWidget(); layout = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.reader_bookmark_btn = QPushButton("Bookmark selected topic")
        self.reader_refresh_btn = QPushButton("Refresh topic tree")
        bar.addWidget(self.reader_bookmark_btn); bar.addWidget(self.reader_refresh_btn); layout.addLayout(bar)
        split = QSplitter(Qt.Horizontal)
        self.topic_tree = QTreeWidget(); self.topic_tree.setHeaderLabel("Saved topics and bookmarks")
        self.reader = QWebEngineView(); self.reader.setPage(QWebEnginePage(self.browser.profile, self.reader))
        split.addWidget(self.topic_tree); split.addWidget(self.reader); split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        self.topic_tree.itemDoubleClicked.connect(self.open_topic_tree_item)
        self.reader_bookmark_btn.clicked.connect(self.toggle_selected_bookmark)
        self.reader_refresh_btn.clicked.connect(self.refresh_topic_tree)
        return w

    def refresh_topic_tree(self):
        if not hasattr(self, "topic_tree"): return
        self.topic_tree.clear()
        pages = self.lib.list_pages(limit=10000)
        root = self.topic_tree.invisibleRootItem()
        saved_root = QTreeWidgetItem(["Saved topic hierarchy"]); root.addChild(saved_root)
        for page in sorted(pages, key=lambda p: ((p.get("title") or "").casefold(), p["url"])):
            parsed = urlparse(page["url"])
            segments = [unquote(x) for x in parsed.path.split("/") if x]
            if segments and segments[0].lower() == "contents": segments = segments[1:]
            parent = saved_root
            for segment in segments[:-1]:
                label = segment.replace("-", " ").replace("_", " ").strip().title() or parsed.netloc
                found = None
                for i in range(parent.childCount()):
                    if parent.child(i).text(0) == label and parent.child(i).data(0, Qt.UserRole) is None:
                        found = parent.child(i); break
                if found is None:
                    found = QTreeWidgetItem([label]); parent.addChild(found)
                parent = found
            label = page.get("title") or (segments[-1] if segments else page["url"])
            leaf = QTreeWidgetItem([label]); leaf.setData(0, Qt.UserRole, page["url"]); parent.addChild(leaf)
        bookmark_root = QTreeWidgetItem(["Bookmarks"]); root.addChild(bookmark_root)
        for mark in self.lib.list_bookmarks():
            item = QTreeWidgetItem([mark["title"] or mark["url"]])
            item.setData(0, Qt.UserRole, mark["url"]); bookmark_root.addChild(item)
        self.topic_tree.expandToDepth(1)

    def open_topic_tree_item(self, item, column=0):
        url = item.data(0, Qt.UserRole)
        if not url: return
        page = next((p for p in self.lib.list_pages(limit=10000) if p["url"] == url), None)
        if page:
            path = self.site_root / "library" / page["html_path"]
            if path.is_file(): self.reader.load(QUrl.fromLocalFile(str(path.resolve())))

    def toggle_selected_bookmark(self):
        item = self.topic_tree.currentItem() if hasattr(self, "topic_tree") else None
        url = item.data(0, Qt.UserRole) if item else None
        if not url:
            QMessageBox.information(self, "Bookmark", "Select a saved topic first."); return
        added = self.lib.toggle_bookmark(url, item.text(0))
        self.set_status("Topic bookmarked." if added else "Bookmark removed.")
        self.refresh_topic_tree()

    def _diagnostics_tab(self):
        w=QWidget(); layout=QVBoxLayout(w)
        buttons=QHBoxLayout()
        self.verify_btn=QPushButton("Verify archive integrity"); self.export_btn=QPushButton("Export portable ZIP")
        self.html_export_btn=QPushButton("Export standalone HTML folder")
        self.clear_data_btn=QPushButton("Clear history & downloaded data…")
        self.open_archive_btn=QPushButton("Open archive folder"); self.report_btn=QPushButton("Save diagnostic report")
        for b in (self.verify_btn,self.export_btn,self.html_export_btn,self.open_archive_btn,self.report_btn,self.clear_data_btn): buttons.addWidget(b)
        layout.addLayout(buttons)
        self.log=QPlainTextEdit(); self.log.setReadOnly(True); self.log.setPlaceholderText("Recent activity and diagnostics"); layout.addWidget(self.log,1)
        self.verify_btn.clicked.connect(self.verify_library); self.export_btn.clicked.connect(self.export)
        self.html_export_btn.clicked.connect(self.export_html_folder)
        self.clear_data_btn.clicked.connect(self.clear_history_and_data)
        self.open_archive_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.site_root / "library"))))
        self.report_btn.clicked.connect(self.save_report)
        return w

    def _site_root_for(self, url):
        host = (urlparse(url).hostname or "default").lower()
        safe = re.sub(r"[^a-z0-9.-]+", "_", host)
        try:
            mapping = json.loads(self.site_map_path.read_text(encoding="utf-8")) if self.site_map_path.exists() else {}
        except Exception:
            mapping = {}
        return Path(mapping.get(host) or (self.app_dir / "sites" / safe))

    def activate_site(self):
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")): return
        root = self._site_root_for(url)
        if root.resolve() == self.site_root.resolve(): return
        self.site_root = root
        self.site_root.mkdir(parents=True, exist_ok=True)
        self.lib = Archiver(self.site_root, lambda *a: None, threading.Event(), threading.Event()).library
        self.storage_label.setText(str(self.site_root))
        self.refresh_all()
        self.set_status("Switched to this site's independent archive and queue.")

    def choose_site_storage(self):
        self.activate_site()
        chosen = QFileDialog.getExistingDirectory(self, "Choose storage folder for this website", str(self.site_root))
        if not chosen: return
        host = (urlparse(self.url.text().strip()).hostname or "default").lower()
        try:
            mapping = json.loads(self.site_map_path.read_text(encoding="utf-8")) if self.site_map_path.exists() else {}
        except Exception:
            mapping = {}
        mapping[host] = chosen
        self.site_map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        self.site_root = Path(chosen)
        self.site_root.mkdir(parents=True, exist_ok=True)
        self.lib = Archiver(self.site_root, lambda *a: None, threading.Event(), threading.Event()).library
        self.storage_label.setText(str(self.site_root))
        self.refresh_all()
        self.set_status("Selected independent storage folder for " + host)

    def start_crawl(self):
        if self.task and self.task.isRunning():
            QMessageBox.information(self,"Busy","Stop the current download first."); return
        url=self.url.text().strip()
        if not url.startswith(("http://","https://")):
            QMessageBox.warning(self,"URL","Enter a valid HTTP or HTTPS URL."); return
        host=urlparse(url).hostname
        cookies=[c for c in self.browser.cookie_snapshot() if host and (c.get("domain","").lstrip(".")==host or host.endswith("."+c.get("domain","").lstrip(".")))]
        if not cookies:
            answer=QMessageBox.question(self,"No matching browser cookies",
                "No cookies for this site have been captured from the embedded browser. Continue only if the page is public or your session does not use cookies.")
            if answer != QMessageBox.Yes: self.tabs.setCurrentIndex(0); return
        opts={"max_pages":self.pages.value(),"max_depth":self.depth.value(),"delay":self.delay.value(),
              "cookies":cookies,"include_external":False}
        self.activate_site()
        self.task=Task(self.site_root,url,opts); self.task.status.connect(self.set_status)
        self.task.progress.connect(self.on_progress); self.task.page.connect(self.on_page)
        self.task.start(); self.set_status("Download started with conservative single-page requests.")
        self.browser.browser_status.setText("Archiving this site in the background. Your sign-in tab remains open.")
        self.tabs.setCurrentIndex(0)

    def start_from_browser(self):
        current = self.browser.view.url().toString()
        if current.startswith(("http://", "https://")):
            self.url.setText(current)
        self.start_crawl()

    def set_status(self,text):
        self.status.setText(text); self.log.appendPlainText(text)
        if hasattr(self, "browser"):
            self.browser.browser_status.setText(text)

    def on_progress(self,current,total,url):
        self.progress.setMaximum(max(1,total)); self.progress.setValue(current)
        self.browser.browser_progress.setMaximum(max(1,total)); self.browser.browser_progress.setValue(current)
        self.status.setText(f"{current}/{total} · {url[:110]}")
        self.browser.browser_status.setText(f"Saved / processing {current} of {total}: {url[:120]}")

    def on_page(self,*_):
        self.refresh_all()

    def stop_task(self):
        if self.task and self.task.isRunning():
            self.task.stop(); self.set_status("Stopping safely after current page…")

    def pause_task(self):
        if not self.task or not self.task.isRunning(): return
        if self.task.pause_event.is_set():
            self.task.pause_event.clear(); self.pause_btn.setText("Pause after current page")
        else:
            self.task.pause_event.set(); self.pause_btn.setText("Resume")

    def refresh_all(self):
        counts=self.lib.queue_stats()
        for key in self.metrics:
            self.metrics[key].setText(str(counts.get(key,0)))
        self.refresh_queue(); self.refresh_pages(); self.refresh_topic_tree()

    def refresh_queue(self):
        state=self.ledger_filter.currentText() if hasattr(self,"ledger_filter") else "all"
        rows=self.lib.queue_items(state)
        term=self.queue_search.text().casefold() if hasattr(self,"queue_search") else ""
        rows=[r for r in rows if not term or term in r["url"].casefold() or term in (r.get("error") or "").casefold()]
        self.queue_table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r["url"],r["state"],r["depth"],r["attempts"],r.get("error") or ""]
            for j,v in enumerate(vals): self.queue_table.setItem(i,j,QTableWidgetItem(str(v)))

    def refresh_pages(self):
        rows=self.lib.list_pages(); self._fill_pages(rows)

    def _fill_pages(self,rows):
        self.page_table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r.get("title"),r.get("url"),r.get("status"),r.get("saved_at"),r.get("completeness","unknown")]
            for j,v in enumerate(vals): self.page_table.setItem(i,j,QTableWidgetItem(str(v or "")))
            self.page_table.item(i,0).setData(Qt.UserRole,r.get("html_path"))

    def search_pages(self):
        self._fill_pages(self.lib.search(self.search.text()))

    def open_page(self,row,col):
        item=self.page_table.item(row,0)
        if item:
            path=self.site_root/"library"/(item.data(Qt.UserRole) or "")
            if path.is_file():
                self.reader.load(QUrl.fromLocalFile(str(path.resolve())))
                self.tabs.setCurrentWidget(self.reader)

    def add_to_queue(self):
        url=self.add_url.text().strip()
        if not url.startswith(("http://","https://")):
            QMessageBox.warning(self,"URL","Enter a full HTTP or HTTPS URL."); return
        added=self.lib.add_url(url,0)
        self.set_status("URL added to queue." if added else "URL already exists in the queue.")
        self.add_url.clear(); self.refresh_all()

    def pass_url_action(self):
        url=self.pass_url.text().strip()
        if not url: QMessageBox.warning(self,"URL","Enter the URL to skip."); return
        self.lib.enqueue(url,0); self.lib.pass_url(url)
        reason=self.pass_reason.text().strip()
        if reason: self.lib.set_queue_state(url,"passed",reason)
        self.pass_url.clear(); self.pass_reason.clear(); self.refresh_all()
        self.set_status("URL marked as passed; it remains visible in the ledger.")

    def redownload_action(self):
        url=self.redownload_url.text().strip()
        if not url: QMessageBox.warning(self,"URL","Enter a URL."); return
        self.lib.force_redownload(url,0); self.redownload_url.clear(); self.refresh_all()
        self.set_status("URL queued for forced recapture.")

    def restore_passed(self):
        n=self.lib.restore_passed(); self.set_status(f"Restored {n} passed URLs."); self.refresh_all()

    def retry_failed(self):
        n=self.lib.retry_failed(); self.set_status(f"Re-queued {n} failed URLs. Start / Resume to continue."); self.refresh_all()

    def retry_incomplete(self):
        n=self.lib.retry_incomplete()
        self.set_status(f"Re-queued {n} incomplete pages. Start / Resume to recapture them; server-dependent functions may remain unavailable offline.")
        self.refresh_all()

    def verify_library(self):
        issues=self.lib.verify()
        if issues:
            QMessageBox.warning(self,"Integrity check","\n".join(issues[:50])); self.log.appendPlainText("\n".join(issues))
        else: QMessageBox.information(self,"Integrity check","All indexed page and asset files passed SHA-256 verification.")

    def export(self):
        path,_=QFileDialog.getSaveFileName(self,"Export portable archive","OfflineArchiver.zip","ZIP (*.zip)")
        if path:
            try: self.lib.export_zip(Path(path)); QMessageBox.information(self,"Export","Portable ZIP export completed.")
            except Exception as exc: QMessageBox.critical(self,"Export error",str(exc))

    def export_html_folder(self):
        path=QFileDialog.getExistingDirectory(self,"Choose destination for standalone HTML export",str(self.site_root / "HTML-Export"))
        if not path: return
        try:
            self.lib.export_html_folder(Path(path))
            QMessageBox.information(self,"HTML export","Standalone HTML pages, local assets and searchable index exported to:\n"+path+"\nOpen index.html in Edge, Chrome or Firefox.")
            self.set_status("Standalone HTML folder export completed.")
        except Exception as exc:
            QMessageBox.critical(self,"HTML export error",str(exc))

    def clear_history_and_data(self):
        if self.task and self.task.isRunning():
            QMessageBox.warning(self,"Download active","Stop the current download before clearing archive data."); return
        message = ("This permanently deletes ALL downloaded HTML pages, saved assets, bookmarks, crawl queue, failures and archive history for this website.\n\n"
                   "Your embedded-browser sign-in profile and other websites' archives are preserved.\n\n"
                   "This cannot be undone. Continue?")
        answer=QMessageBox.warning(self,"Permanently clear archive data",message,QMessageBox.Yes | QMessageBox.No,QMessageBox.No)
        if answer != QMessageBox.Yes: return
        try:
            self.lib.clear_history_and_data()
            self.refresh_all()
            self.set_status("Downloaded pages, assets, bookmarks and crawl history were cleared for this website. Browser sign-in was preserved.")
        except Exception as exc:
            QMessageBox.critical(self,"Clear data error",str(exc))

    def save_report(self):
        path,_=QFileDialog.getSaveFileName(self,"Save diagnostics","OfflineArchiver-diagnostics.txt","Text (*.txt)")
        if path:
            counts=self.lib.queue_stats()
            failures=self.lib.list_failures()
            lines=["Offline Archiver diagnostic report",json.dumps(counts,indent=2),"", "Recent failures:"]
            lines.extend(f'{r["created"]} | {r["url"]} | {r["error"]}' for r in failures[:300])
            lines.extend(["","Recent application activity:",self.log.toPlainText()])
            Path(path).write_text("\n".join(lines),encoding="utf-8")
            self.set_status("Diagnostic report saved.")

    def closeEvent(self,event):
        if self.task and self.task.isRunning():
            self.task.stop(); self.task.wait(5000)
        event.accept()


def main():
    # Do not disable Chromium's sandbox. The embedded browser keeps its own persistent profile.
    app=QApplication(sys.argv)
    window=Window(); window.show()
    return app.exec()
