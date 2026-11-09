"""Live searchable local archive dashboard."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlparse


def publish_dashboard(library, destination: Path) -> Path:
    rows = library.list_pages(limit=50000)
    pages = []
    for row in rows:
        parts = [x for x in urlparse(row.get("url", "")).path.split("/") if x]
        pages.append({
            "id": row.get("id"), "title": row.get("title") or row.get("url"),
            "url": row.get("url"), "file": "library/" + (row.get("html_path") or ""),
            "text": row.get("text") or "", "saved": row.get("saved_at") or "",
            "depth": row.get("depth", 0), "status": row.get("status", 0),
            "completeness": row.get("completeness") or "unknown",
            "notes": row.get("completeness_notes") or "", "path": parts,
            "sha256": row.get("sha256") or ""
        })
    data = json.dumps(pages, ensure_ascii=False).replace("</", "<\\\\/")
    manifest = {"format":"OfflineArchiverManifest/1","pageCount":len(pages),
        "pages":[{k:p[k] for k in ("id","url","file","saved","depth","status","completeness","notes","sha256","path")} for p in pages]}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    (destination.parent / "archive-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    html = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline archive</title><style>
body{font:15px system-ui;margin:0;background:#f4f7fb;color:#182638}header{background:#16345b;color:white;padding:18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
main{display:grid;grid-template-columns:280px 1fr;gap:16px;padding:16px;max-width:1500px;margin:auto}aside,section{background:white;border:1px solid #dbe3ed;border-radius:12px;padding:16px;min-width:0}
input{font:inherit;padding:11px;border:1px solid #bdc9d8;border-radius:8px;width:100%;box-sizing:border-box}button{padding:8px 11px;border:1px solid #bdc9d8;border-radius:7px;cursor:pointer}
.item{padding:12px 0;border-bottom:1px solid #edf0f5}.muted,.path{color:#64748b;font-size:13px}.hit{display:block;margin:7px 0;color:#174d86}
details{margin:6px 0 6px 10px}a{color:#174d86}#gate{position:fixed;inset:0;background:#10243bdd;display:grid;place-items:center;padding:20px}
#gate section{max-width:400px;width:100%;background:white;border-radius:12px;padding:24px}
@media(max-width:760px){main{grid-template-columns:1fr}}
</style><header><h1 id="title">Offline archive</h1><span id="count"></span><span style="flex:1"></span><button id="edit">Edit archive title</button><button id="export">Export manifest</button></header>
<main><aside><h3>Saved hierarchy</h3><div id="tree"></div></aside><section><h2>Search saved pages</h2><input id="q" placeholder="Search topics, paragraphs, tables and page text"><p id="summary" class="muted"></p><div id="results"></div><p class="muted">Remote APIs and server-dependent functions may require connectivity and cannot be guaranteed offline.</p></section></main>
<div id="gate"><section><h2>Local archive passcode</h2><p id="prompt">Create a passcode.</p><input id="p1" type="password" placeholder="Passcode"><input id="p2" type="password" placeholder="Repeat passcode"><button id="unlock">Create passcode</button><p class="muted">Access barrier only, not encryption. Files remain readable to users with filesystem access.</p><p id="error"></p></section></div>
<script>
const P=__PAGES__;const $=x=>document.getElementById(x);const esc=s=>String(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function tree(){let root={};P.forEach(p=>{let n=root;(p.path.length?p.path:["Pages"]).forEach(k=>n=n[k]||(n[k]={}));n.items=(n.items||[]).concat(p)});function draw(n){return Object.keys(n).filter(k=>k!=="items").sort().map(k=>"<details open><summary>"+esc(k)+"</summary>"+draw(n[k])+(n[k].items||[]).map(p=>'<div><a href="'+esc(p.file)+'">'+esc(p.title)+'</a></div>').join("")+"</details>").join("")+(n.items||[]).map(p=>'<div><a href="'+esc(p.file)+'">'+esc(p.title)+'</a></div>').join("")} $("tree").innerHTML=draw(root)}
function search(){let q=$("q").value.trim().toLowerCase();if(q.length<2){$("results").innerHTML="";$("summary").textContent="Enter at least two characters.";return}let words=q.split(/\s+/),hits=[];
P.forEach(p=>{let t=p.text||"";if(!words.every(w=>t.toLowerCase().includes(w)||p.title.toLowerCase().includes(w)))return;let s=(t.match(/[^.!?\n]+[.!?]?/g)||[]).map(x=>x.trim()).filter(x=>words.every(w=>x.toLowerCase().includes(w))).slice(0,6);hits.push({p,s})});
$("summary").textContent=hits.length+" matching pages";$("results").innerHTML=hits.slice(0,300).map(x=>'<article class="item"><b><a href="'+esc(x.p.file)+'">'+esc(localStorage.getItem("oa-title-"+x.p.id)||x.p.title)+'</a></b><div class="path">'+esc(x.p.path.join(" › "))+' · '+esc(x.p.completeness)+'</div>'+x.s.map(s=>'<a class="hit" href="'+esc(x.p.file)+'">'+esc(s)+'</a>').join("")+'<button data-id="'+x.p.id+'">Edit title</button></article>').join("")||"<p>No matching text.</p>";
document.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{let p=P.find(x=>x.id==b.dataset.id),v=prompt("Display title",localStorage.getItem("oa-title-"+p.id)||p.title);if(v!==null){localStorage.setItem("oa-title-"+p.id,v);search();tree()}})}
async function hash(pass,salt){let k=await crypto.subtle.importKey("raw",new TextEncoder().encode(pass),"PBKDF2",false,["deriveBits"]);let b=await crypto.subtle.deriveBits({name:"PBKDF2",salt:salt,iterations:100000,hash:"SHA-256"},k,256);return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,"0")).join("")}
function gate(){let h=localStorage.getItem("oa-hash"),s=localStorage.getItem("oa-salt");$("prompt").textContent=h?"Enter passcode.":"Create passcode."; $("p2").hidden=!!h;$("unlock").onclick=async()=>{let a=$("p1").value,b=$("p2").value;if(a.length<6){$("error").textContent="Use at least six characters.";return}if(h){let salt=Uint8Array.from(atob(s),c=>c.charCodeAt(0));if(await hash(a,salt)!==h){$("error").textContent="Incorrect passcode.";return}}else{if(a!==b){$("error").textContent="Passcodes do not match.";return}let salt=crypto.getRandomValues(new Uint8Array(16));localStorage.setItem("oa-salt",btoa(String.fromCharCode(...salt)));localStorage.setItem("oa-hash",await hash(a,salt))}$("gate").remove()}}
$("q").addEventListener("input",search);$("edit").onclick=()=>{let v=prompt("Archive title",localStorage.getItem("oa-title")||"Offline archive");if(v!==null){localStorage.setItem("oa-title",v);$("title").textContent=v}};
$("export").onclick=()=>{let b=new Blob([JSON.stringify({format:"OfflineArchiver/1",pages:P},null,2)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="offline-archive-state.json";a.click();URL.revokeObjectURL(a.href)};
$("title").textContent=localStorage.getItem("oa-title")||"Offline archive";$("count").textContent=P.length+" pages";tree();gate();
</script></html>""".replace("__PAGES__", data)
    destination.write_text(html, encoding="utf-8")
    return destination
