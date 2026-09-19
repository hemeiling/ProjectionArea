"""Browser smoke test for the viewer.

CONSTITUTION.md §42: an API returning 200 is not "done" for a feature whose
whole point is that a human can see what was measured. This drives the real
page in a real browser and asserts that the number, the overlay and the audit
panel actually appear.

Skipped automatically when Playwright or a Chromium build is unavailable, so it
never blocks the core suite.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

#: Browsers Playwright may drive. The bundled download is preferred; a
#: system Chrome is accepted so a developer machine needs no extra setup.
_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def _launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        import os

        for path in _CHROME_PATHS:
            if os.path.exists(path):
                return pw.chromium.launch(executable_path=path)
        pytest.skip("no Chromium build available for Playwright")


@pytest.fixture(scope="module")
def viewer_url():
    """Run the real app on a free port for the duration of the module."""
    import uvicorn

    from backend.main import app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        pytest.skip("server did not start")

    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=5)


def test_viewer_measures_a_drawing_end_to_end(viewer_url, drawings):
    """Open the page, load a PDF, analyse it, and read the answer off the UI."""
    truth = drawings["plate_with_holes"]
    errors = []

    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None,
        )

        page.goto(viewer_url)
        page.wait_for_timeout(800)
        page.set_input_files("#file", truth["path"])
        page.wait_for_selector("#sheet", state="visible", timeout=20000)
        page.wait_for_timeout(1200)

        page.click("#srvRun")
        page.wait_for_selector("#srvResultSect", state="visible", timeout=40000)
        page.wait_for_timeout(500)

        page.select_option("#unit", "mm2")
        page.wait_for_timeout(400)

        headline = page.inner_text("#rcMain")
        readout = page.inner_text("#total")
        confidence = page.inner_text("#rcConfPct")
        regions = page.inner_text("#srvRegions")
        scale = page.inner_text("#srvScale")
        overlay_paths = page.eval_on_selector_all("#vec path", "els => els.length")

        page.click("#fxReview")
        page.wait_for_timeout(1200)
        hit_targets = page.eval_on_selector_all("#vec path[data-idx]", "els => els.length")

        browser.close()

    assert errors == [], f"the page logged errors: {errors}"

    measured = float(headline.split("mm")[0].replace(",", "").strip())
    assert measured == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    # The signature readout must carry the authoritative number, not a dash.
    assert readout.replace(",", "").strip().startswith("23")
    assert int(confidence.rstrip("%")) >= 80
    assert "TOP VIEW" in regions
    assert "1 : 2" in scale
    assert overlay_paths > 5, "the audit overlay must actually draw something"
    assert hit_targets > 10, "review mode must expose clickable linework"


def test_viewer_refuses_to_show_an_area_without_scale(viewer_url, drawings):
    """A scanned page has no calibration; the UI must say so, not show a number."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        page.goto(viewer_url)
        page.wait_for_timeout(800)
        page.set_input_files("#file", drawings["raster_plate"]["path"])
        page.wait_for_selector("#sheet", state="visible", timeout=20000)
        page.wait_for_timeout(1200)
        page.click("#srvRun")
        page.wait_for_selector("#srvResultSect", state="visible", timeout=60000)
        page.wait_for_timeout(500)

        headline = page.inner_text("#rcMain")
        alt = page.inner_text("#rcAlt")
        scale = page.inner_text("#srvScale")
        chips = page.inner_text("#srvChips")
        browser.close()

    assert "未标定" in headline, headline
    assert "不是制造尺寸" in alt, alt
    assert "未建立" in scale or "人工标定" in scale
    assert "扫描" in chips or "位图" in chips
