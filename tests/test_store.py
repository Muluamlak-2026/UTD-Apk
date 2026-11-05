from pathlib import Path
from offline_archiver.store import Library

def test_save_search_and_export(tmp_path: Path):
    lib=Library(tmp_path/"lib")
    lib.add_site("https://example.org/topic")
    pid=lib.save_page("https://example.org/topic","https://example.org/topic","Test topic",
        "<html><body>acute appendicitis</body></html>","acute appendicitis",200,0,[])
    assert pid == 1
    assert len(lib.list_pages()) == 1
    assert lib.stats()[0] == 1
    out=tmp_path/"export.zip"
    lib.export_zip(out)
    assert out.exists() and out.stat().st_size > 0
