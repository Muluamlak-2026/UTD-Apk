from pathlib import Path
import zipfile

from offline_archiver.store import Library


def test_save_search_update_and_export(tmp_path: Path):
    lib = Library(tmp_path / "lib")
    lib.add_site("https://example.org/topic")
    pid = lib.save_page(
        "https://example.org/topic", "https://example.org/topic", "Test topic",
        "<html><body>acute appendicitis</body></html>", "acute appendicitis", 200, 0, []
    )
    assert pid == 1
    assert len(lib.list_pages()) == 1
    assert lib.search("appendicitis")[0]["id"] == pid

    # Updating a page must update, not duplicate, its FTS row.
    lib.save_page(
        "https://example.org/topic", "https://example.org/topic", "Updated topic",
        "<html><body>chronic cholecystitis</body></html>", "chronic cholecystitis", 200, 0, []
    )
    assert len(lib.search("cholecystitis")) == 1
    assert lib.search("appendicitis") == []

    out = tmp_path / "export.zip"
    lib.export_zip(out)
    assert out.exists() and out.stat().st_size > 0
    with zipfile.ZipFile(out) as archive:
        assert "index.html" in archive.namelist()
        assert "pages/00000001.html" in archive.namelist()


def test_persistent_queue_pass_and_retry(tmp_path: Path):
    lib = Library(tmp_path / "lib")
    lib.enqueue("https://example.org/a", 0)
    lib.enqueue("https://example.org/b", 1)
    assert len(lib.pending_queue()) == 2
    lib.set_queue_state("https://example.org/a", "running")
    lib.save_failure("https://example.org/a", "temporary error", 0)
    assert lib.retry_failed() == 1
    assert {r["url"] for r in lib.pending_queue()} == {
        "https://example.org/a", "https://example.org/b"
    }
    lib.pass_url("https://example.org/b")
    assert all(r["url"] != "https://example.org/b" for r in lib.pending_queue())


def test_integrity_verification(tmp_path: Path):
    lib = Library(tmp_path / "lib")
    lib.save_page(
        "https://example.org", "https://example.org", "Home",
        "<html><body>hello</body></html>", "hello", 200, 0, []
    )
    assert lib.verify() == []
    page = lib.pages_dir / "00000001.html"
    page.write_text("tampered", encoding="utf-8")
    assert any("Hash mismatch" in problem for problem in lib.verify())


def test_running_queue_items_recover_after_restart(tmp_path: Path):
    root = tmp_path / "lib"
    lib = Library(root)
    lib.enqueue("https://example.org/interrupted", 2)
    lib.set_queue_state("https://example.org/interrupted", "running")
    lib.db.close()

    reopened = Library(root)
    pending = reopened.pending_queue()
    assert len(pending) == 1
    assert pending[0]["url"] == "https://example.org/interrupted"



def test_queue_ledger_add_pass_reason_and_force_redownload(tmp_path: Path):
    lib = Library(tmp_path / "lib")
    url = "https://example.org/topic"
    assert lib.add_url(url, 0) is True
    assert lib.add_url(url, 0) is False
    lib.pass_url(url)
    lib.set_queue_state(url, "passed", "User skipped this topic")
    row = lib.queue_items("passed")[0]
    assert row["url"] == url
    assert row["error"] == "User skipped this topic"
    assert lib.restore_passed() == 1
    assert lib.pending_queue()[0]["url"] == url
    lib.force_redownload(url, 2)
    row = lib.queue_items("pending")[0]
    assert row["depth"] == 2



def test_page_completeness_and_retry_incomplete(tmp_path: Path):
    lib = Library(tmp_path / "lib")
    url = "https://example.org/calculator"
    lib.enqueue(url, 2)
    lib.save_page(
        url, url, "Calculator", "<html><body>calculator</body></html>",
        "calculator", 200, 2, [], completeness="partial",
        completeness_notes="Fetch/XHR API detected"
    )
    row = lib.list_pages()[0]
    assert row["completeness"] == "partial"
    assert "API detected" in row["completeness_notes"]
    assert lib.queue_stats()["incomplete"] == 1
    assert lib.retry_incomplete() == 1
    assert lib.pending_queue()[0]["url"] == url
