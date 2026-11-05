from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import *

from .engine import Archiver


def home():
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OfflineArchiver"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Task(QThread):
    status = Signal(str)
    progress = Signal(int, int, str)
    page = Signal(int, str, str)

    def __init__(self, mode, url, opts=None):
        super().__init__()
        self.mode, self.url, self.opts = mode, url, opts or {}
        self.stop_event, self.pause_event = threading.Event(), threading.Event()
        self.engine = Archiver(home(), self.emit_event, self.stop_event, self.pause_event)

    def emit_event(self, kind, *args):
        if kind == "status":
            self.status.emit(str(args[0]))
        elif kind == "progress":
            self.progress.emit(*args)
        elif kind == "page":
            self.page.emit(*args)

    def run(self):
        try:
            if self.mode == "login":
                self.engine.open_login(self.url)
            else:
                self.engine.crawl(self.url, **self.opts)
        except Exception as exc:
            self.status.emit("Error: " + str(exc))
        finally:
            self.status.emit("Task finished.")

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Offline Archiver")
        self.resize(1240, 820)
        self.task = None
        self.lib = Archiver(home(), lambda *args: None, threading.Event(), threading.Event()).library

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        header = QHBoxLayout()
        title = QLabel("Offline Archiver")
        title.setStyleSheet("font-size:25px;font-weight:700;color:#142b4a")
        header.addWidget(title)
        header.addStretch()
        self.status = QLabel("Ready")
        header.addWidget(self.status)
        layout.addLayout(header)

        self.url = QLineEdit()
        self.url.setPlaceholderText("Authorized website or topic URL")
        layout.addWidget(self.url)

        options = QHBoxLayout()
        self.pages = QSpinBox()
        self.pages.setRange(1, 10000)
        self.pages.setValue(100)
        self.depth = QSpinBox()
        self.depth.setRange(0, 20)
        self.depth.setValue(3)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(.2, 30)
        self.delay.setValue(1.5)
        for label, widget in (("Max pages / run", self.pages), ("Link depth", self.depth), ("Delay (sec)", self.delay)):
            options.addWidget(QLabel(label))
            options.addWidget(widget)
        layout.addLayout(options)

        controls = QHBoxLayout()
        self.login = QPushButton("Open sign-in browser")
        self.crawl = QPushButton("Start / Resume")
        self.pause = QPushButton("Pause")
        self.stop = QPushButton("Stop")
        for button in (self.login, self.crawl, self.pause, self.stop):
            controls.addWidget(button)
        layout.addLayout(controls)

        self.bar = QProgressBar()
        layout.addWidget(self.bar)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Title", "URL", "State", "Saved"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self.open_page)
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search saved content")
        self.search_btn = QPushButton("Search")
        self.refresh_btn = QPushButton("Refresh")
        self.pass_btn = QPushButton("Pass selected URL")
        self.restore_btn = QPushButton("Restore passed")
        self.retry_btn = QPushButton("Retry failed")
        self.verify_btn = QPushButton("Verify library")
        self.export_btn = QPushButton("Export portable ZIP")
        for widget in (self.search, self.search_btn, self.refresh_btn, self.pass_btn,
                       self.restore_btn, self.retry_btn, self.verify_btn, self.export_btn):
            bottom.addWidget(widget)
        layout.addLayout(bottom)
        self.setCentralWidget(root)

        self.setStyleSheet("""
            QMainWindow,QWidget{background:#f4f6fa;color:#1e293b;font-family:Segoe UI}
            QLineEdit,QSpinBox,QDoubleSpinBox{background:white;border:1px solid #cbd5e1;border-radius:6px;padding:8px}
            QPushButton{background:#e5edf8;border:0;border-radius:6px;padding:10px;font-weight:600}
            QPushButton:hover{background:#d2e0f3}
            QProgressBar{height:14px;border:0;background:#e2e8f0;border-radius:6px;text-align:center}
            QProgressBar::chunk{background:#2563eb;border-radius:6px}
            QTableWidget{background:white;gridline-color:#e8edf3}
            QHeaderView::section{background:#edf2f8;padding:8px;border:0;font-weight:600}
        """)

        self.login.clicked.connect(lambda: self.start("login"))
        self.crawl.clicked.connect(lambda: self.start("crawl"))
        self.stop.clicked.connect(self.stop_task)
        self.pause.clicked.connect(self.pause_task)
        self.refresh_btn.clicked.connect(self.refresh)
        self.search_btn.clicked.connect(self.search_pages)
        self.search.returnPressed.connect(self.search_pages)
        self.export_btn.clicked.connect(self.export)
        self.pass_btn.clicked.connect(self.pass_selected)
        self.restore_btn.clicked.connect(self.restore_passed)
        self.retry_btn.clicked.connect(self.retry_failed)
        self.verify_btn.clicked.connect(self.verify_library)
        self.refresh()

    def start(self, mode):
        if self.task and self.task.isRunning():
            QMessageBox.information(self, "Busy", "Stop the current task first.")
            return
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "URL", "Enter a valid HTTP or HTTPS URL.")
            return
        opts = {"max_pages": self.pages.value(), "max_depth": self.depth.value(), "delay": self.delay.value()}
        self.task = Task(mode, url, opts)
        self.task.status.connect(self.status.setText)
        self.task.progress.connect(self.on_progress)
        self.task.page.connect(lambda *_: self.refresh())
        self.task.start()

    def stop_task(self):
        if self.task:
            self.task.stop()
            self.status.setText("Stopping safely…")

    def pause_task(self):
        if not self.task or not self.task.isRunning():
            return
        if self.task.pause_event.is_set():
            self.task.pause_event.clear()
            self.pause.setText("Pause")
        else:
            self.task.pause_event.set()
            self.pause.setText("Resume")

    def on_progress(self, current, total, url):
        self.bar.setMaximum(max(1, total))
        self.bar.setValue(current)
        self.status.setText(url[:100])

    def refresh(self):
        rows = self.lib.list_pages()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("title", "url", "state", "saved_at")):
                self.table.setItem(i, j, QTableWidgetItem(str(row.get(key) or "")))
            self.table.item(i, 0).setData(32, row.get("html_path"))
        pages, assets, failures = self.lib.stats()
        self.status.setText(f"{pages} pages · {assets} assets · {failures} failures")

    def open_page(self, row, col):
        item = self.table.item(row, 0)
        if item:
            path = home() / "library" / (item.data(32) or "")
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def search_pages(self):
        rows = self.lib.search(self.search.text())
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("title", "url", "saved_at")):
                self.table.setItem(i, j, QTableWidgetItem(str(row.get(key) or "")))
            self.table.item(i, 0).setData(32, row.get("html_path"))

    def pass_selected(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 1):
            QMessageBox.information(self, "Select URL", "Select a saved page row first.")
            return
        url = self.table.item(row, 1).text()
        self.lib.pass_url(url)
        self.status.setText("Marked as passed: " + url)
        self.refresh()

    def restore_passed(self):
        count = self.lib.restore_passed()
        QMessageBox.information(self, "Restore", f"Restored {count} passed queue entries.")
        self.refresh()

    def retry_failed(self):
        count = self.lib.retry_failed()
        if count:
            QMessageBox.information(self, "Retry", f"Re-queued {count} failed URLs. Start/Resume to continue.")
        else:
            QMessageBox.information(self, "Retry", "No failed queue entries to retry.")
        self.refresh()

    def verify_library(self):
        problems = self.lib.verify()
        if problems:
            QMessageBox.warning(self, "Integrity check", "\n".join(problems[:30]))
        else:
            QMessageBox.information(self, "Integrity check", "All indexed page and asset files passed SHA-256 verification.")

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export ZIP", "OfflineArchiver.zip", "ZIP (*.zip)")
        if path:
            try:
                self.lib.export_zip(Path(path))
                QMessageBox.information(self, "Export", "Portable ZIP export completed.")
            except Exception as exc:
                QMessageBox.critical(self, "Export error", str(exc))


def main():
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()
