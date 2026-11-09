import json
from pathlib import Path

from offline_archiver.legacy_import import import_legacy_archive
from offline_archiver.store import Library


def test_import_legacy_manifest_and_seed_pending_queue(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "topics").mkdir()
    (legacy / "topics" / "one.html").write_text(
        '<!doctype html><title>One</title><main>Saved topic</main>'
        '<a href="/contents/two">Two</a><a href="https://example.org/out">External</a>',
        encoding="utf-8",
    )
    (legacy / "archive-manifest.json").write_text(json.dumps({
        "format": "OfflineSiteSaver/1",
        "pages": [{"file": "topics/one.html", "url": "https://www.uptodate.com/contents/one"}],
    }), encoding="utf-8")

    library = Library(tmp_path / "new-library")
    result = import_legacy_archive(legacy, library)

    assert result["imported"] == 1
    assert result["queued"] == 1
    assert library.list_pages()[0]["title"] == "One"
    pending = library.pending_queue()
    assert [row["url"] for row in pending] == ["https://www.uptodate.com/contents/two"]
    assert (legacy / "topics" / "one.html").is_file()


def test_legacy_import_is_idempotent(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "one.html").write_text("<title>One</title><p>Body</p>", encoding="utf-8")
    (legacy / "archive-manifest.json").write_text(json.dumps({
        "pages": [{"file": "one.html", "url": "https://www.uptodate.com/contents/one"}],
    }), encoding="utf-8")
    library = Library(tmp_path / "library")

    first = import_legacy_archive(legacy, library)
    second = import_legacy_archive(legacy, library)

    assert first["imported"] == 1
    assert second["status"] == "unchanged"
    assert len(library.list_pages()) == 1


def test_manifest_path_traversal_is_not_imported(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("<title>Outside</title>", encoding="utf-8")
    (legacy / "archive-manifest.json").write_text(json.dumps({
        "pages": [{"file": "../outside.html", "url": "https://www.uptodate.com/outside"}],
    }), encoding="utf-8")
    library = Library(tmp_path / "library")

    result = import_legacy_archive(legacy, library)

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert not library.list_pages()
