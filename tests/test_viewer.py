"""Browser smoke test for the viewer.

CONSTITUTION.md §42: an API returning 200 is not "done" for a feature whose
whole point is that a human can see what was measured. This drives the real
page in a real browser and asserts that the number, the overlay and the audit
panel actually appear.

Skipped automatically when Playwright or a Chromium build is unavailable, so it
never blocks the core suite.
"""

from __future__ import annotations

import re
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
    """Any Chromium this machine has, in order of preference.

    Playwright's default wants the ``chrome-headless-shell`` download; a machine
    may instead have the full bundled Chromium, or only a system Chrome. Trying
    all three means the browser tests actually run on a normal dev machine rather
    than silently skipping — a skipped UI test proves nothing (§42).
    """
    import os

    attempts = [
        lambda: pw.chromium.launch(),
        lambda: pw.chromium.launch(channel="chromium"),
    ]
    attempts += [
        (lambda p=path: pw.chromium.launch(executable_path=p))
        for path in _CHROME_PATHS
        if os.path.exists(path)
    ]
    for attempt in attempts:
        try:
            return attempt()
        except Exception:
            continue
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


def _headline_number(text: str) -> float:
    """The numeric value out of the result card, which also carries a label.

    The card reads e.g. "设备几何并集 / 23,057.6mm²" — the label is asserted
    separately, so this only has to find the magnitude.
    """
    match = re.search(r"([\d,]+(?:\.\d+)?)", text.replace("\n", " "))
    assert match, f"no number in the result card: {text!r}"
    return float(match.group(1).replace(",", ""))


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

    assert "已计入几何并集" in headline, (
        "the headline must name which physical region it measured, not just a number"
    )
    measured = _headline_number(headline)
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


# ── Milestone A: the product is usable in a browser without finding a file ───


def _open_demo(page, viewer_url, demo_id="plate_with_holes"):
    """Click the demo drawing through, exactly as a user would."""
    page.goto(viewer_url)
    page.wait_for_selector(f"#demoGrid button[data-demo={demo_id}]", timeout=30000)
    page.click(f"#demoGrid button[data-demo={demo_id}]")
    page.wait_for_selector("#srvResultSect", state="visible", timeout=90000)
    page.wait_for_timeout(900)


def test_demo_drawing_runs_the_real_pipeline_from_one_click(viewer_url, drawings):
    """"Try demo drawing" must produce a measured result, computed not canned.

    The number is compared against the fixture's analytically known area, so a
    hard-coded or stale answer fails here.
    """
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

        # The landing page must offer the demo before anything is loaded.
        page.goto(viewer_url)
        page.wait_for_selector("#demoGrid button[data-demo]", timeout=30000)
        offered = page.eval_on_selector_all("#demoGrid button[data-demo]", "els => els.length")
        first_label = page.inner_text("#demoGrid button[data-demo]")

        _open_demo(page, viewer_url)
        page.select_option("#unit", "mm2")
        page.wait_for_timeout(400)

        headline = page.inner_text("#rcMain")
        sheet_visible = page.is_visible("#sheet")
        overlay_paths = page.eval_on_selector_all("#vec path", "els => els.length")
        browser.close()

    assert errors == [], f"the page logged errors: {errors}"
    assert offered >= 3, "the landing page must offer several demo drawings"
    assert "试用样例图纸" in first_label
    assert sheet_visible, "the drawing itself must render, not just the numbers"
    assert overlay_paths > 5, "the audit overlay must draw"
    assert _headline_number(headline) == pytest.approx(truth["net_area_mm2"], rel=2e-3)


def test_three_footprint_interpretations_are_offered_and_switchable(viewer_url, drawings):
    """The core product concept: three readings, and the overlay follows them.

    Uses the L-shaped layout, where the three differ by 32 % — on a rectangle
    they nearly coincide and a broken switch would not show up.
    """
    errors = []
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        _open_demo(page, viewer_url, "layout_1_100")
        page.select_option("#unit", "m2")
        page.wait_for_timeout(400)

        available = page.eval_on_selector_all(
            "#rcFootprints button[data-fp]", "els => els.map(e => e.dataset.fp)"
        )
        blocked = page.eval_on_selector_all(
            "#rcFootprints button[data-fp-pending]",
            "els => els.map(e => e.dataset.fpPending)",
        )
        blocked_disabled = page.eval_on_selector_all(
            "#rcFootprints button[data-fp-pending]", "els => els.every(e => e.disabled)"
        )
        blocked_text = page.inner_text("#rcFootprints")

        def shape_area():
            """The drawn footprint path: its type and its vertex count.

            Vertex count is the discriminator, not the bounding box — a union and
            its own bounding rectangle share a bounding box by definition, so
            comparing extents would pass even if the switch did nothing.
            """
            return page.evaluate(
                """() => {
                    const el = document.querySelector('#vec path[data-fp-shape]');
                    if (!el) return null;
                    const d = el.getAttribute('d') || '';
                    const b = el.getBBox();
                    return {
                        type: el.dataset.fpShape,
                        vertices: (d.match(/[\\d.]+,[\\d.]+/g) || []).length,
                        w: +b.width.toFixed(2),
                        h: +b.height.toFixed(2),
                    };
                }"""
            )

        union_head = page.inner_text("#rcMain")
        union_shape = shape_area()

        page.click("#rcFootprints button[data-fp=bounding_rectangle]")
        page.wait_for_timeout(500)
        box_head = page.inner_text("#rcMain")
        box_shape = shape_area()
        box_pressed = page.get_attribute(
            "#rcFootprints button[data-fp=bounding_rectangle]", "aria-pressed"
        )

        page.click("#rcFootprints button[data-fp=convex_envelope]")
        page.wait_for_timeout(500)
        hull_head = page.inner_text("#rcMain")
        hull_shape = shape_area()
        browser.close()

    assert errors == [], errors
    assert {"geometry_union", "convex_envelope", "bounding_rectangle"} <= set(available)
    assert set(blocked) == {"conveyor_footprint", "guarded_area", "line_footprint"}
    assert blocked_disabled, "unavailable readings must not look clickable"
    assert "需要 CAD 图层/块语义" in blocked_text, "say why they are unavailable"

    # Each reading names itself and reports its own number.
    assert "已计入几何并集" in union_head and "54.46" in union_head.replace(",", "")
    assert "外接矩形" in box_head and "72.00" in box_head.replace(",", "")
    assert "凸包外廓" in hull_head and "64.00" in hull_head.replace(",", "")
    assert box_pressed == "true", "the selected card must be visibly selected"

    # The overlay is drawn from the selected interpretation's own geometry.
    assert union_shape and box_shape and hull_shape
    assert union_shape["type"] == "geometry_union"
    assert box_shape["type"] == "bounding_rectangle"
    assert hull_shape["type"] == "convex_envelope"
    # The L-shaped plan has a 6-vertex outline; its bounding rectangle has 5
    # points (4 corners plus the closing repeat). If switching did nothing, the
    # vertex count would not change.
    assert box_shape["vertices"] == 5, box_shape
    assert union_shape["vertices"] > box_shape["vertices"], (union_shape, box_shape)
    assert hull_shape["vertices"] != box_shape["vertices"], (hull_shape, box_shape)
    # And the rectangle must still bound the union it was derived from.
    assert box_shape["w"] >= union_shape["w"] - 0.5
    assert box_shape["h"] >= union_shape["h"] - 0.5


def test_explain_calculation_shows_the_real_path_and_evidence(viewer_url, drawings):
    """Explain Calculation must trace the actual stages, from backend fields."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        _open_demo(page, viewer_url)

        collapsed = page.eval_on_selector("#rcExplain", "el => el.open")
        page.click("#rcExplainSummary")
        page.wait_for_timeout(400)
        opened = page.eval_on_selector("#rcExplain", "el => el.open")
        flow = page.inner_text("#rcFlow")

        page.click("#rcEngineering > summary")
        page.wait_for_timeout(300)
        details = page.inner_text("#rcEngineering")
        browser.close()

    assert collapsed is False, "deep detail stays out of the way until asked for"
    assert opened is True

    # The documented calculation path, stage by stage.
    for stage in ("来源", "视图", "比例", "单位", "图元分类", "footprint 定义",
                  "多边形与孔洞", "并集", "单位换算", "结果"):
        assert stage in flow, f"missing stage {stage!r} in the explanation"
    assert "mm/unit" in flow, "the detected scale must be shown"
    assert "1 : 2" in flow, "the implied ratio must be shown"
    assert "该定义的证据" in flow, "interpretation-specific evidence must appear"
    # Evidence comes from FootprintInterpretation, not re-derived in the browser.
    assert "union of" in flow or "并集" in flow

    assert "方法" in details and "几何" in details, details[:200]


def test_overlay_layers_can_be_toggled(viewer_url, drawings):
    """Visual verification: each class of geometry can be shown or hidden."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        _open_demo(page, viewer_url)

        toggles = page.eval_on_selector_all(
            "#rcLayers input[data-layer]", "els => els.map(e => e.dataset.layer)"
        )
        before = page.eval_on_selector_all("#vec path", "els => els.length")

        # Turning the footprint off must remove its shape from the overlay.
        page.uncheck("#rcLayers input[data-layer=footprint]")
        page.wait_for_timeout(350)
        without_footprint = page.eval_on_selector_all(
            "#vec path[data-fp-shape]", "els => els.length"
        )

        page.check("#rcLayers input[data-layer=footprint]")
        page.check("#rcLayers input[data-layer=boundingRect]")
        page.wait_for_timeout(350)
        after = page.eval_on_selector_all("#vec path", "els => els.length")
        browser.close()

    for layer in ("source", "measured", "excluded", "dimensions", "holes",
                  "footprint", "envelope", "boundingRect", "warnings"):
        assert layer in toggles, f"missing overlay toggle {layer!r}"
    assert without_footprint == 0, "unchecking the footprint must hide it"
    assert after > before, "adding the bounding-rectangle layer must draw more"


def test_a_scanned_demo_still_refuses_to_invent_an_area(viewer_url, drawings):
    """The refusal survives the new UI: no reading may show millimetres."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        _open_demo(page, viewer_url, "raster_plate")
        headline = page.inner_text("#rcMain")
        means = page.inner_text("#rcMeans")
        browser.close()

    assert "未标定" in headline, headline
    assert "毫米" in means or "比例" in means, means


def test_operator_two_point_calibration_marks_the_result_as_manual(viewer_url, drawings):
    """The manual calibration path, driven with real mouse clicks.

    Production drawings printed via "Microsoft Print to PDF" carry no text layer,
    so automatic calibration is impossible and this is the only way to get a
    physical area out of them. The result must then be visibly *operator*
    calibrated — not presented as something the engine verified — and the span
    that was picked must be recoverable from the overlay and the explanation, so
    a reviewer can check the operator measured the right line (§8, §30).
    """
    errors = []
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "console",
            lambda m: errors.append(m.text) if m.type == "error" else None,
        )
        _open_demo(page, viewer_url)

        before = page.inner_text("#rcMain")
        assert page.is_visible("#rcCalBadge") is False, (
            "an automatically calibrated result must not claim to be manual"
        )

        page.select_option("#calMode", "pick")
        page.wait_for_timeout(250)
        page.click("#startPick")

        # Two points on a span that fits on screen, in PDF units.
        a, b = (200.0, 200.0), (500.0, 200.0)
        geom = page.evaluate(
            """() => {
                const c = document.getElementById('overlay');
                const r = c.getBoundingClientRect();
                return {left:r.left, top:r.top, w:r.width, h:r.height,
                        cw:c.width, ch:c.height, scale:S.scale};
            }"""
        )
        for point in (a, b):
            px, py = point[0] * geom["scale"], point[1] * geom["scale"]
            page.mouse.click(
                geom["left"] + px * geom["w"] / geom["cw"],
                geom["top"] + py * geom["h"] / geom["ch"],
            )
            page.wait_for_timeout(250)

        picked = page.evaluate("() => S.calPts.length")
        assert picked == 2, f"two clicks must register two calibration points, got {picked}"

        # 300 PDF units declared as 600 mm => exactly 2 mm per unit.
        page.fill("#knownLen", "600")
        page.select_option("#knownUnit", "mm")
        page.click("#applyPick")
        page.wait_for_timeout(4000)

        badge_visible = page.is_visible("#rcCalBadge")
        badge = " ".join(page.inner_text("#rcCalBadge").split())
        after = page.inner_text("#rcMain")
        label = page.eval_on_selector_all(
            "#vec text[data-calibration-label]", "els => els.map(e => e.textContent)"
        )
        span_drawn = page.eval_on_selector_all(
            "#vec path[stroke='#9A3412']", "els => els.length"
        )
        page.click("#rcExplainSummary")
        page.wait_for_timeout(400)
        flow = page.inner_text("#rcFlow")
        browser.close()

    assert errors == [], errors
    assert badge_visible, "an operator-calibrated result must say so"
    assert "人工标定" in badge and "operator-calibrated" in badge
    assert "600 mm" in badge, "the badge must state the declared length"
    assert "2.0000 mm/unit" in badge or "2.00" in badge, badge

    # The measurement actually changed — the calibration was applied, not ignored.
    assert after != before, "re-calculation must follow the new scale"

    # The picked span is drawn back onto the drawing, with its declared length.
    assert label == ["600 mm"], label
    assert span_drawn >= 1, "the calibration span itself must be drawn"

    # And preserved in Explain Calculation, from the backend's own record.
    assert "人工标定，非自动核验" in flow
    assert "600" in flow
    assert "user_two_point_calibration" in flow or "两点人工标定" in flow
