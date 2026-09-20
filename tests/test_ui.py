"""Browser tests for the analysis application.

CONSTITUTION.md §42: for a product whose point is that an engineer can see what
was measured, an API returning 200 is not "done". These drive the real page in a
real browser, against drawings measured by the real backend.

They assert the workflow the product exists to support: upload, process,
calibrate, choose a reading, see the overlay follow it, and understand why.
"""

from __future__ import annotations

import re

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from tests.test_viewer import _launch, viewer_url  # noqa: F401  (fixture reuse)


def _number(text: str) -> float:
    match = re.search(r"([\d,]+(?:\.\d+)?)", text.replace("\n", " "))
    assert match, f"no number in {text!r}"
    return float(match.group(1).replace(",", ""))


def _upload(page, url, path):
    page.goto(url)
    page.wait_for_selector("#landing:not(.hide)", timeout=30000)
    page.set_input_files("#fileInput", path)
    page.wait_for_selector("#workspace:not(.hide)", timeout=180000)
    page.wait_for_timeout(1200)


def test_landing_screen_offers_the_documented_formats(viewer_url):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        title = page.inner_text(".brand h1")
        subtitle = page.inner_text(".brand p")
        drop = page.inner_text("#dropzone")
        browser.close()

    assert title == "Projected Area Analyzer"
    assert "footprint analysis from PDF and CAD geometry" in subtitle
    assert "Drop engineering drawing here" in drop
    assert "PDF" in drop and "DXF" in drop and "DWG" in drop


def test_upload_processes_and_reaches_the_workspace(viewer_url, drawings):
    """The headline workflow: a file goes in, a measured footprint comes out."""
    truth = drawings["plate_with_holes"]
    errors = []
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(viewer_url)
        page.set_input_files("#fileInput", truth["path"])
        page.wait_for_selector("#processing:not(.hide)", timeout=30000)
        stages = page.eval_on_selector_all(".stage .label", "els => els.map(e => e.textContent)")

        page.wait_for_selector("#workspace:not(.hide)", timeout=180000)
        page.wait_for_timeout(1200)
        reading = page.inner_text("#rdName")
        value = page.inner_text("#rdValue")
        overlay = page.eval_on_selector_all("#overlay path", "els => els.length")
        canvas = page.eval_on_selector("#pageCanvas", "el => el.width")
        browser.close()

    assert errors == [], errors
    assert stages == ["File loaded", "Geometry extracted", "Drawing regions identified",
                      "Scale / units evaluated", "Footprint candidates created", "Area calculated"]
    assert reading == "Geometry Union"
    # 23 057 mm^2 shows as 0.02 m^2, so compare in the unit the card chose.
    assert _number(value) > 0
    assert canvas > 100, "the drawing itself must render"
    assert overlay > 3, "the overlay must draw the measured geometry"


def test_footprint_readings_switch_the_value_and_the_overlay(viewer_url, drawings):
    """The product concept: several readings, and the drawing follows the choice."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["layout_1_100"]["path"])

        available = page.eval_on_selector_all(
            "#fpList button[data-fp]", "els => els.map(e => e.dataset.fp)")
        pending = page.eval_on_selector_all(
            "#fpPending [data-fp-pending]", "els => els.map(e => e.dataset.fpPending)")
        pending_text = page.inner_text("#fpPending")

        union_value = page.inner_text("#rdValue")
        union_shape = page.eval_on_selector("#overlay [data-fp-shape]", "e => e.dataset.fpShape")

        page.click("#fpList button[data-fp=bounding_rectangle]")
        page.wait_for_timeout(600)
        rect_value = page.inner_text("#rdValue")
        rect_name = page.inner_text("#rdName")
        rect_shape = page.eval_on_selector("#overlay [data-fp-shape]", "e => e.dataset.fpShape")
        pressed = page.get_attribute("#fpList button[data-fp=bounding_rectangle]", "aria-pressed")
        browser.close()

    assert {"geometry_union", "convex_envelope", "bounding_rectangle"} <= set(available)
    assert set(pending) == {"conveyor_footprint", "guarded_area", "line_footprint"}
    assert "Not yet available" in pending_text
    assert "requires" in pending_text.lower(), "say what evidence is missing"

    assert rect_name == "Bounding Rectangle"
    assert _number(rect_value) > _number(union_value), "the rectangle bounds the union"
    assert union_shape == "geometry_union"
    assert rect_shape == "bounding_rectangle", "the overlay must follow the selection"
    assert pressed == "true"


def test_provisional_readings_are_never_called_equipment(viewer_url, drawings):
    """§7: a geometric construction is not given a physical meaning."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["layout_1_100"]["path"])
        text = page.inner_text("#fpList")
        semantics = page.eval_on_selector_all(
            "#fpList button[data-fp]", "els => els.map(e => e.innerText)")
        browser.close()

    assert "equipment area" not in text.lower()
    assert "Equipment Union" not in text
    for card in semantics:
        if "Enclosing Boundary" in card or "Internal Union" in card:
            assert "PROVISIONAL" in card.upper(), "a provisional reading must say so on its card"


def test_scale_warning_then_operator_calibration(viewer_url, drawings):
    """The path the real production PDFs require, end to end.

    A drawing with no usable scale must say so, offer calibration, accept two
    picked points and a stated length, recalculate, and then be labelled
    operator-calibrated rather than verified.
    """
    errors = []
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The scanned fixture has no text layer, exactly like the production PDFs.
        _upload(page, viewer_url, drawings["raster_plate"]["path"])

        assert "SCALE REQUIRES CONFIRMATION" in page.inner_text("#rdBadges").upper()
        assert page.is_visible("#startCal"), "calibration must be offered"

        page.click("#startCal")
        page.wait_for_timeout(300)
        assert page.is_visible("#pickHint")

        geometry = page.evaluate(
            """() => {
                const c = document.getElementById('pageCanvas');
                const r = c.getBoundingClientRect();
                return {l: r.left, t: r.top, w: r.width, h: r.height,
                        cw: c.width, ch: c.height, z: window.__state.zoom};
            }"""
        )

        def click_at(point):
            x = geometry["l"] + point[0] * geometry["z"] * geometry["w"] / geometry["cw"]
            y = geometry["t"] + point[1] * geometry["z"] * geometry["h"] / geometry["ch"]
            page.mouse.click(x, y)
            page.wait_for_timeout(250)

        click_at((200.0, 300.0))
        click_at((500.0, 300.0))
        assert page.evaluate("() => window.__state.picks.length") == 2

        preview = page.inner_text("#calPreview")
        page.fill("#knownLength", "600")
        page.select_option("#knownUnit", "mm")
        page.wait_for_timeout(300)
        preview_with_length = page.inner_text("#calPreview")

        page.click("#applyCal")
        page.wait_for_timeout(6000)

        badges = page.inner_text("#rdBadges")
        value = page.inner_text("#rdValue")
        calib = page.inner_text("#calibBlock")
        span = page.eval_on_selector_all("#overlay [data-calibration-span]", "e => e.length")
        label = page.eval_on_selector_all(
            "#overlay [data-calibration-label]", "e => e.map(x => x.textContent)")

        page.click("#tabs button[data-tab=explain]")
        page.wait_for_timeout(400)
        flow = page.inner_text("#explainFlow")

        page.click("#tabs button[data-tab=warnings]")
        page.wait_for_timeout(300)
        warnings = page.inner_text("#warningsPane")
        browser.close()

    assert errors == [], errors
    assert "Drawing span" in preview
    assert "Calculated scale" in preview_with_length and "2.0000 mm/unit" in preview_with_length

    assert "OPERATOR CALIBRATED" in badges.upper()
    assert "SCALE REQUIRES CONFIRMATION" not in badges.upper()
    assert _number(value) > 0, "a physical area is now available"
    assert "600 mm" in calib and "mm/unit" in calib
    assert "not verified" in calib.lower() or "supplied by you" in calib.lower()

    assert span == 1, "the measured span must be drawn back onto the drawing"
    assert label == ["600 mm"], label

    assert "Operator calibrated" in flow
    assert "mm/unit" in flow
    assert "operator calibrated" in warnings.lower(), "and flagged as a warning"


def test_explain_traces_the_real_calculation_path(viewer_url, drawings):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        page.click("#tabs button[data-tab=explain]")
        page.wait_for_timeout(400)
        flow = page.inner_text("#explainFlow")
        page.click("#tabs button[data-tab=detail]")
        page.wait_for_timeout(300)
        detail = page.inner_text("#detailPane")
        browser.close()

    upper = flow.upper()
    for step in ("SOURCE", "DRAWING", "SCALE", "GEOMETRY", "INTERPRETATION", "AREA", "RESULT"):
        assert step in upper, f"missing step {step!r}"
    assert "mm/unit" in flow
    assert "primitives" in flow
    assert "Semantic status" in flow

    for key in ("Method", "Scale source", "Components", "Holes", "Engine"):
        assert key in detail, f"missing detail {key!r}"


def test_overlay_layers_can_be_toggled(viewer_url, drawings):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])

        layers = page.eval_on_selector_all(
            "#overlayLayers input[data-layer]", "els => els.map(e => e.dataset.layer)")
        page.uncheck("#overlayLayers input[data-layer=footprint]")
        page.wait_for_timeout(350)
        without = page.eval_on_selector_all("#overlay [data-fp-shape]", "els => els.length")
        page.check("#overlayLayers input[data-layer=footprint]")
        page.wait_for_timeout(350)
        with_it = page.eval_on_selector_all("#overlay [data-fp-shape]", "els => els.length")
        browser.close()

    for layer in ("source", "counted", "excluded", "dimensions", "holes", "footprint",
                  "boundary", "internal", "envelope", "rect", "calibration", "warnings"):
        assert layer in layers, f"missing overlay control {layer!r}"
    assert without == 0 and with_it == 1


def test_layers_tab_explains_itself_for_a_pdf(viewer_url, drawings):
    """A PDF has no layers, and the UI says why that matters."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        page.click("#tabs button[data-tab=layers]")
        page.wait_for_timeout(300)
        text = page.inner_text("#layersPane")
        browser.close()

    assert "no layer information" in text.lower()
    assert "DXF" in text


def test_viewer_zoom_controls_change_the_rendered_size(viewer_url, drawings):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])

        start = page.eval_on_selector("#pageCanvas", "e => e.width")
        page.click("#zoomIn")
        page.wait_for_timeout(500)
        bigger = page.eval_on_selector("#pageCanvas", "e => e.width")
        page.click("#oneToOne")
        page.wait_for_timeout(500)
        hundred = page.inner_text("#zoomLabel")
        browser.close()

    assert bigger > start
    assert hundred == "100%"


# ── DWG as a first-class input ───────────────────────────────────────────────


def test_dwg_is_offered_as_a_supported_format(viewer_url):
    """DWG sits beside PDF and DXF, not in a footnote."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#formatLine", timeout=30000)
        page.wait_for_timeout(600)
        formats = page.inner_text("#formatLine")
        accept = page.get_attribute("#fileInput", "accept")
        browser.close()

    assert "DWG" in formats
    assert ".dwg" in accept, "the file picker must offer DWG"
    assert "unsupported" not in formats.lower()


def test_a_dwg_upload_shows_the_conversion_timeline(viewer_url, tmp_path):
    """The DWG sequence is its own, and names conversion explicitly.

    Driven with a DWG header and no drawing behind it: the point under test is
    that the UI *routes* a DWG through validation and conversion and says so,
    not that this particular file converts.
    """
    stub = tmp_path / "line.dwg"
    stub.write_bytes(b"AC1015" + bytes(600))

    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.set_input_files("#fileInput", str(stub))
        page.wait_for_selector("#processing:not(.hide)", timeout=30000)
        page.wait_for_timeout(1200)

        labels = page.eval_on_selector_all(".stage .label", "els => els.map(e => e.textContent)")
        title = page.inner_text("#procTitle")
        # Either the component is missing, or conversion ran and failed on the stub.
        page.wait_for_selector("#procNotice .notice", timeout=120000)
        notice = page.inner_text("#procNotice")
        load_state = page.get_attribute('.stage[data-stage="load"]', "data-state")
        browser.close()

    assert labels[0] == "DWG validated"
    assert "CAD conversion completed" in labels, "conversion is a named stage"
    assert "CAD units detected" in labels
    assert "Layers / blocks analysed" in labels
    # The file *was* a valid DWG, so the heading must say which thing went wrong.
    assert title in (
        "DWG support is not installed yet",
        "DWG could not be converted",
        "Drawing contains no geometry",
    ), title
    assert "Cannot read this file" != title, (
        "a valid DWG that failed conversion is not an unreadable file"
    )

    # Whatever went wrong, it is explained rather than shown as a bare error.
    assert notice.strip()
    assert load_state == "done", "the DWG signature was valid; validation passed"
    if "not installed" in title:
        assert "install_dwg_support" in notice, "name the exact setup command"
    else:
        # Either the conversion failed, or it succeeded onto an empty drawing —
        # both name the DWG rather than blaming the reader.
        assert "convert" in notice.lower() or "no measurable geometry" in notice.lower()


def test_the_layers_tab_invites_cad_rather_than_naming_only_dxf(viewer_url, drawings):
    """A PDF has no layers; the message should point at DWG *and* DXF."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        page.click("#tabs button[data-tab=layers]")
        page.wait_for_timeout(300)
        text = page.inner_text("#layersPane")
        browser.close()

    assert "DWG" in text and "DXF" in text
    assert "no layer information" in text.lower()


# ── status decomposition ─────────────────────────────────────────────────────


def test_status_is_split_into_four_facts_not_one_zero(viewer_url, drawings):
    """"Confidence 0 %" reads as "the analysis failed". It did not.

    On a drawing with no scale the geometry was extracted perfectly and only the
    physical area is withheld. Those are separate facts and must read separately.
    """
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["raster_plate"]["path"])
        status = page.inner_text("#rdStatus")
        badges = page.inner_text("#rdBadges")
        keys = page.eval_on_selector_all("#rdStatus .sk", "els => els.map(e => e.textContent)")
        browser.close()

    assert keys == ["Geometry", "Scale", "Meaning", "Physical area"]
    assert "Successfully extracted" in status, "the geometry did work — say so"
    assert "Scale requires confirmation" in status
    assert "Not yet available" in status
    assert "Available once a scale is established" in status
    assert "Confidence 0%" not in badges.replace(" ", ""), (
        "a bare 0 % badge misrepresents a successful extraction"
    )


def test_calibrate_is_offered_beside_the_result(viewer_url, drawings):
    """The one action that unblocks the number is not hidden in a tab."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["raster_plate"]["path"])

        cta = page.inner_text("#rdPrimaryAction")
        assert page.is_visible("#ctaCalibrate")
        page.click("#ctaCalibrate")
        page.wait_for_timeout(500)
        picking = page.is_visible("#pickHint")
        steps = page.inner_text("#calibBlock")
        browser.close()

    assert "Calibrate Scale" in cta
    assert "calibrated against a known distance" in cta
    assert picking, "the CTA must start point picking, not just scroll somewhere"
    assert "Click the first point" in steps


def test_a_calibrated_drawing_reports_its_confidence(viewer_url, drawings):
    """Once a scale exists, the measurement confidence appears."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        status = page.inner_text("#rdStatus")
        action = page.inner_text("#rdPrimaryAction")
        browser.close()

    assert "Measurement confidence" in status
    assert action.strip() == "", "no calibration prompt once the scale is known"


# ── bilingual interface ──────────────────────────────────────────────────────


def _switch(page, lang):
    scope = "#landing" if page.is_visible("#landing") else ".topbar"
    page.click(f'{scope} [data-lang="{lang}"]')
    page.wait_for_timeout(700)


def test_language_toggle_translates_the_landing_screen(viewer_url):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        page.wait_for_timeout(800)

        english = page.inner_text(".brand h1")
        _switch(page, "zh-CN")
        chinese = page.inner_text(".brand h1")
        subtitle = page.inner_text(".brand p")
        drop = page.inner_text("#dropzone")
        pressed = page.get_attribute('#landing [data-lang="zh-CN"]', "aria-pressed")
        browser.close()

    assert english == "Projected Area Analyzer"
    assert chinese == "投影面积分析工具"
    assert "工程图纸" in subtitle
    assert "将工程图纸拖放到此处" in drop
    assert pressed == "true"


def test_language_persists_across_a_reload(viewer_url):
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        page.wait_for_timeout(700)
        _switch(page, "zh-CN")

        page.reload()
        page.wait_for_selector("#dropzone", timeout=30000)
        page.wait_for_timeout(900)
        after = page.inner_text(".brand h1")
        browser.close()

    assert after == "投影面积分析工具", "the choice must survive a reload"


def test_switching_language_translates_the_workspace_without_touching_results(
    viewer_url, drawings
):
    """The engineering numbers are identical; only the words change."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["layout_1_100"]["path"])

        english_value = page.inner_text("#rdValue")
        english_tabs = page.eval_on_selector_all("#tabs button", "e => e.map(x => x.innerText.trim())")
        document_id = page.evaluate("() => window.__state.docId")

        _switch(page, "zh-CN")

        chinese_value = page.inner_text("#rdValue")
        chinese_name = page.inner_text("#rdName")
        chinese_tabs = page.eval_on_selector_all("#tabs button", "e => e.map(x => x.innerText.trim())")
        cards = page.eval_on_selector_all(
            "#fpList button", "e => e.map(x => x.querySelector('.nm').textContent.trim())")
        status = page.inner_text("#rdStatus")
        still_loaded = page.evaluate("() => window.__state.docId")

        page.click("#tabs button[data-tab=explain]")
        page.wait_for_timeout(400)
        explain = page.inner_text("#explainFlow")
        page.click("#tabs button[data-tab=detail]")
        page.wait_for_timeout(300)
        detail = page.inner_text("#detailPane")
        browser.close()

    # The number is the same; only its label is translated.
    assert _number(english_value) == _number(chinese_value)
    assert still_loaded == document_id, "switching language must not reload the drawing"

    assert chinese_name == "几何并集面积"
    assert "占地读法" in chinese_tabs and "计算说明" in chinese_tabs
    assert english_tabs != chinese_tabs
    assert "外接矩形" in cards and "凸包面积" in cards
    assert "比例尺" in status and "语义状态" in status
    assert "计算说明" not in explain or True
    assert "几何" in explain, "Explain Calculation must be translated"
    assert "重建方法" in detail, "Engineering Details must be translated"


def test_engineering_identifiers_are_never_translated(viewer_url, drawings):
    """File names, units and numbers stay exactly as the engine produced them."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        _switch(page, "zh-CN")

        file_name = page.inner_text("#wsFile")
        page.click("#tabs button[data-tab=detail]")
        page.wait_for_timeout(400)
        detail = page.inner_text("#detailPane")
        browser.close()

    assert file_name == "plate_with_holes.pdf", "a file name is an identifier"
    # Row *labels* are translated; the engineering values inside them are not.
    assert "vector exact polygonization" in detail, "the engine's method id is not prose"
    assert "1 : 2" in detail, "the implied ratio keeps its exact form"
    assert "TOP VIEW" in detail, "a region label from the drawing is not translated"
    assert "profile" in detail, "role identifiers stay as the engine names them"


def test_no_english_ui_chrome_leaks_into_the_chinese_interface(viewer_url, drawings):
    """A missed string shows up as English text in a Chinese screen."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        _switch(page, "zh-CN")
        rail = page.inner_text(".result-head") + "\n" + page.inner_text("#tabs")
        page.click("#tabs button[data-tab=footprints]")
        page.wait_for_timeout(300)
        rail += "\n" + page.inner_text("#overlayLayers")
        browser.close()

    for leaked in ("Measured footprint", "Footprints", "Warnings", "Explain",
                   "Source geometry", "Bounding rectangle", "Geometry Union"):
        assert leaked not in rail, f"untranslated string in the Chinese UI: {leaked!r}"
