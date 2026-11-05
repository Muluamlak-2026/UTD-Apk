import os,sys,threading
from pathlib import Path
from PySide6.QtCore import QThread,Signal,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import *
from .engine import Archiver

def home():
 p=Path(os.environ.get("LOCALAPPDATA",str(Path.home()/"AppData/Local")))/"OfflineArchiver";p.mkdir(parents=True,exist_ok=True);return p
class Task(QThread):
 status=Signal(str); progress=Signal(int,int,str); page=Signal(int,str,str)
 def __init__(self,mode,url,opts=None):
  super().__init__();self.mode=mode;self.url=url;self.opts=opts or {};self.stop_event=threading.Event();self.pause_event=threading.Event()
  self.engine=Archiver(home(),self.emit_event,self.stop_event,self.pause_event)
 def emit_event(self,k,*a):
  if k=="status":self.status.emit(str(a[0]))
  elif k=="progress":self.progress.emit(*a)
  elif k=="page":self.page.emit(*a)
 def run(self):
  try:
   if self.mode=="login":self.engine.open_login(self.url)
   else:self.engine.crawl(self.url,**self.opts)
  except Exception as e:self.status.emit("Error: "+str(e))
  self.status.emit("Task finished.")
 def stop(self):self.stop_event.set();self.pause_event.clear()
class Window(QMainWindow):
 def __init__(self):
  super().__init__();self.setWindowTitle("Offline Archiver");self.resize(1120,760);self.task=None
  self.lib=Archiver(home(),lambda *a:None,threading.Event(),threading.Event()).library
  w=QWidget();v=QVBoxLayout(w);v.setContentsMargins(24,20,24,20)
  h=QHBoxLayout();t=QLabel("Offline Archiver");t.setStyleSheet("font-size:25px;font-weight:700;color:#142b4a");h.addWidget(t);h.addStretch();self.status=QLabel("Ready");h.addWidget(self.status);v.addLayout(h)
  self.url=QLineEdit();self.url.setPlaceholderText("Enter authorized website or topic URL");v.addWidget(self.url)
  opts=QHBoxLayout();self.pages=QSpinBox();self.pages.setRange(1,10000);self.pages.setValue(100);self.depth=QSpinBox();self.depth.setRange(0,20);self.depth.setValue(3);self.delay=QDoubleSpinBox();self.delay.setRange(.2,30);self.delay.setValue(1.5)
  for label,widget in (("Max pages",self.pages),("Depth",self.depth),("Delay sec",self.delay)):opts.addWidget(QLabel(label));opts.addWidget(widget)
  v.addLayout(opts)
  buttons=QHBoxLayout()
  self.login=QPushButton("Open sign-in browser");self.crawl=QPushButton("Start archiving");self.pause=QPushButton("Pause");self.stop=QPushButton("Stop")
  for b in (self.login,self.crawl,self.pause,self.stop):buttons.addWidget(b)
  v.addLayout(buttons);self.bar=QProgressBar();v.addWidget(self.bar)
  self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(["Title","URL","Status","Saved"]);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch);self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch);self.table.cellDoubleClicked.connect(self.open_page);v.addWidget(self.table)
  bottom=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText("Search saved content");self.search_btn=QPushButton("Search");self.export_btn=QPushButton("Export portable ZIP");self.refresh_btn=QPushButton("Refresh")
  for x in (self.search,self.search_btn,self.export_btn,self.refresh_btn):bottom.addWidget(x)
  v.addLayout(bottom);self.setCentralWidget(w)
  self.setStyleSheet("QMainWindow,QWidget{background:#f4f6fa;color:#1e293b;font-family:Segoe UI} QLineEdit,QSpinBox,QDoubleSpinBox{background:white;border:1px solid #cbd5e1;border-radius:6px;padding:8px} QPushButton{background:#e5edf8;border:0;border-radius:6px;padding:10px;font-weight:600} QPushButton:hover{background:#d2e0f3} QProgressBar{height:14px;border:0;background:#e2e8f0;border-radius:6px;text-align:center} QProgressBar::chunk{background:#2563eb;border-radius:6px} QTableWidget{background:white;gridline-color:#e8edf3} QHeaderView::section{background:#edf2f8;padding:8px;border:0;font-weight:600}")
  self.login.clicked.connect(lambda:self.start("login"));self.crawl.clicked.connect(lambda:self.start("crawl"));self.stop.clicked.connect(self.stop_task);self.pause.clicked.connect(self.pause_task);self.refresh_btn.clicked.connect(self.refresh);self.search_btn.clicked.connect(self.search_pages);self.export_btn.clicked.connect(self.export);self.search.returnPressed.connect(self.search_pages);self.refresh()
 def start(self,mode):
  if self.task and self.task.isRunning():QMessageBox.information(self,"Busy","Stop the current task first.");return
  if not self.url.text().startswith(("http://","https://")):QMessageBox.warning(self,"URL","Enter a valid HTTP or HTTPS URL.");return
  opts={"max_pages":self.pages.value(),"max_depth":self.depth.value(),"delay":self.delay.value()}
  self.task=Task(mode,self.url.text().strip(),opts);self.task.status.connect(self.status.setText);self.task.progress.connect(self.on_progress);self.task.page.connect(lambda *a:self.refresh());self.task.start()
 def stop_task(self):
  if self.task:self.task.stop();self.status.setText("Stopping…")
 def pause_task(self):
  if not self.task or not self.task.isRunning():return
  if self.task.pause_event.is_set():self.task.pause_event.clear();self.pause.setText("Pause")
  else:self.task.pause_event.set();self.pause.setText("Resume")
 def on_progress(self,n,total,url):self.bar.setMaximum(max(1,total));self.bar.setValue(n);self.status.setText(url[:90])
 def refresh(self):
  rows=self.lib.list_pages();self.table.setRowCount(len(rows))
  for i,r in enumerate(rows):
   for j,k in enumerate(("title","url","state","saved_at")):self.table.setItem(i,j,QTableWidgetItem(str(r.get(k) or "")))
   self.table.item(i,0).setData(32,r.get("html_path"))
  p,a,f=self.lib.stats();self.status.setText(f"{p} pages · {a} assets · {f} failures")
 def open_page(self,row,col):
  rel=self.table.item(row,0).data(32);p=home()/"library"/rel
  if p.exists():QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
 def search_pages(self):
  rows=self.lib.search(self.search.text());self.table.setRowCount(len(rows))
  for i,r in enumerate(rows):
   for j,k in enumerate(("title","url","saved_at")):self.table.setItem(i,j,QTableWidgetItem(str(r.get(k) or "")))
   self.table.item(i,0).setData(32,r.get("html_path"))
 def export(self):
  p,_=QFileDialog.getSaveFileName(self,"Export ZIP","OfflineArchiver.zip","ZIP (*.zip)")
  if p:
   try:self.lib.export_zip(Path(p));QMessageBox.information(self,"Export","Export complete.")
   except Exception as e:QMessageBox.critical(self,"Export error",str(e))
def main():
 app=QApplication(sys.argv);win=Window();win.show();return app.exec()
