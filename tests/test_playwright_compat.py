from offline_archiver.playwright_compat import ensure_cdp_download_compat


def test_qt_cdp_patch_is_applied_and_idempotent(tmp_path):
    driver = tmp_path / "chromium.js"
    driver.write_text("const persistent = { noDefaultViewport: true };", encoding="utf-8")
    assert ensure_cdp_download_compat(driver) is True
    assert 'acceptDownloads: "internal-browser-default"' in driver.read_text(encoding="utf-8")
    assert ensure_cdp_download_compat(driver) is False


def test_qt_cdp_patch_fails_closed_on_unrecognized_driver(tmp_path):
    driver = tmp_path / "chromium.js"
    driver.write_text("const persistent = { changed: true };", encoding="utf-8")
    try:
        ensure_cdp_download_compat(driver)
    except RuntimeError as exc:
        assert "patch target not found" in str(exc)
    else:
        raise AssertionError("Expected unknown Playwright driver to fail closed")
