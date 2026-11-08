from offline_archiver.playwright_compat import ensure_cdp_download_compat


def test_qt_cdp_patch_targets_connect_over_cdp_context(tmp_path):
    driver = tmp_path / "chromium.js"
    driver.write_text(
        "const persistent = { noDefaultViewport: true };\n"
        "validateBrowserContextOptions(persistent, browserOptions);\n",
        encoding="utf-8",
    )
    assert ensure_cdp_download_compat(driver) is True
    source = driver.read_text(encoding="utf-8")
    assert 'acceptDownloads: "internal-browser-default"' in source
    assert ensure_cdp_download_compat(driver) is False


def test_qt_cdp_patch_handles_compiled_validation_anchor(tmp_path):
    driver = tmp_path / "chromium.js"
    driver.write_text(
        "const persistent={ noDefaultViewport:true, other:true };\n"
        "  validateBrowserContextOptions(persistent, browserOptions);\n",
        encoding="utf-8",
    )
    assert ensure_cdp_download_compat(driver) is True
    source = driver.read_text(encoding="utf-8")
    assert 'persistent.acceptDownloads = "internal-browser-default";' in source
    assert source.index("persistent.acceptDownloads") < source.index(
        "validateBrowserContextOptions"
    )


def test_qt_cdp_patch_fails_closed_on_unknown_driver(tmp_path):
    driver = tmp_path / "chromium.js"
    driver.write_text("const persistent = { changed: true };", encoding="utf-8")
    try:
        ensure_cdp_download_compat(driver)
    except RuntimeError as exc:
        assert "connectOverCDP" in str(exc)
    else:
        raise AssertionError("Expected unknown Playwright driver to fail closed")
