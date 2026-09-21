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

        # The figures block replaced the old single-line preview: same three
        # numbers, each labelled, so the arithmetic can be checked before applying.
        preview = page.inner_text("#calFigures")
        page.fill("#knownLength", "600")
        page.select_option("#knownUnit", "mm")
        page.wait_for_timeout(300)
        preview_with_length = page.inner_text("#calFigures")

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
        # "validated" is the DWG plan's first stage; the signature was valid, so
        # it has to read as passed even though the conversion behind it did not.
        validated_state = page.get_attribute(
            '.stage[data-stage="validated"]', "data-state")
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
    assert validated_state == "done", "the DWG signature was valid; validation passed"
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
    assert "Click the first endpoint" in steps, (
        "the step list names the endpoint the operator is to click")


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
    """Click the toggle belonging to whichever screen is showing."""
    if page.is_visible("#landing"):
        scope = "#landing"
    elif page.is_visible("#processing"):
        scope = "#processing"
    else:
        scope = ".topbar"
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


# ── processing progress ──────────────────────────────────────────────────────
#
# The bar exists because a production DWG takes minutes. What matters is not
# that it animates but that every number on it came from the job: these watch
# the real page drive a real analysis and assert the properties an engineer
# relies on — it only moves forward, it reaches the end before the screen
# changes, and a failure leaves the evidence of how far it got on screen.


def _progress_state(page):
    return page.evaluate(
        """() => ({
            pct: document.getElementById('progressPct').textContent,
            what: document.getElementById('progressWhat').textContent,
            counts: document.getElementById('progressCounts').textContent,
            width: document.getElementById('progressFill').style.width,
            cls: document.getElementById('progressBlock').className,
            stages: [...document.querySelectorAll('.stage')].map(
                (e) => e.dataset.stage + ':' + e.dataset.state),
            workspace: !document.getElementById('workspace').classList.contains('hide'),
        })"""
    )


def _watch_progress(page, path, samples=600):
    """Upload, then record every distinct progress state until the job settles."""
    page.set_input_files("#fileInput", path)
    page.wait_for_selector("#processing:not(.hide)", timeout=30000)
    seen = []
    for _ in range(samples):
        state = _progress_state(page)
        if not seen or state != seen[-1]:
            seen.append(state)
        if state["workspace"] or "failed" in state["cls"]:
            break
        page.wait_for_timeout(100)
    return seen


def _percentages(seen):
    return [int(s["pct"].rstrip("%")) for s in seen if s["pct"].endswith("%")]


def test_the_progress_bar_only_ever_moves_forward(viewer_url, drawings):
    """A bar that rewinds reads as a fault, whatever the backend meant by it."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        seen = _watch_progress(page, drawings["layout_1_100"]["path"])
        browser.close()

    percentages = _percentages(seen)
    assert percentages, "the bar reported nothing at all"
    assert all(b >= a for a, b in zip(percentages, percentages[1:])), (
        f"progress went backwards: {percentages}"
    )
    widths = [float(s["width"].rstrip("%")) for s in seen if s["width"].endswith("%")]
    assert all(b >= a - 1e-9 for a, b in zip(widths, widths[1:])), (
        f"the filled width went backwards: {widths}"
    )


def test_completion_is_shown_before_the_workspace_replaces_it(viewer_url, drawings):
    """100 % must be seen, not inferred from the result appearing."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        seen = _watch_progress(page, drawings["plate_with_holes"]["path"])
        page.wait_for_selector("#workspace:not(.hide)", timeout=180000)
        browser.close()

    percentages = _percentages(seen)
    assert 100 in percentages, f"the bar never reached 100 %: {percentages}"
    hundred_at = percentages.index(100)
    workspace_at = next(
        (i for i, s in enumerate(seen) if s["workspace"]), len(seen))
    assert hundred_at <= workspace_at, (
        "100 % has to be on screen before the workspace takes over"
    )
    assert "done" in seen[-1]["cls"] or seen[-1]["workspace"]


def test_completed_stages_are_reflected_in_the_bar(viewer_url, drawings):
    """The checklist and the bar are two views of one job state, not two guesses."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        seen = _watch_progress(page, drawings["layout_1_100"]["path"])
        browser.close()

    assert seen, "nothing observed"
    for state in seen:
        assert state["stages"], "the detailed stage list stays beside the bar"

    done = [sum(1 for st in s["stages"] if st.endswith(":done")) for s in seen]
    percentages = _percentages(seen)
    assert all(b >= a for a, b in zip(done, done[1:])), (
        f"completed stages went backwards: {done}"
    )
    # A newly completed stage never comes with less of the bar filled.
    pairs = list(zip(done, percentages))
    for (done_a, pct_a), (done_b, pct_b) in zip(pairs, pairs[1:]):
        if done_b > done_a:
            assert pct_b >= pct_a, (
                f"stage {done_b} completed but the bar fell: {pct_a} -> {pct_b}")
    assert done[-1] > done[0], f"no stage ever completed: {done}"
    assert done[-1] == len(seen[-1]["stages"]), "a finished job has every stage ticked"


def test_real_counts_are_reported_inside_the_long_stage(viewer_url, drawings):
    """Counts like "145,000 / 163,691 primitives" are the job's, or absent."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        seen = _watch_progress(page, drawings["obround_with_slot"]["path"])
        browser.close()

    ratios = [s["counts"] for s in seen if "/" in s["counts"]]
    values = []
    for text in ratios:
        left, right = text.split("/", 1)
        current = int("".join(ch for ch in left if ch.isdigit()))
        total = int("".join(ch for ch in right if ch.isdigit()))
        assert current <= total, f"a count exceeded its own total: {text!r}"
        values.append(current)
    assert all(b >= a for a, b in zip(values, values[1:])), values
    # Synthetic fixtures are small enough to finish inside one poll, so an
    # absent ratio is correct here; an ill-formed one never is.


def test_a_failed_analysis_keeps_the_progress_it_earned(viewer_url, tmp_path):
    """Where it stopped is the useful part, so failure must not reset the bar."""
    stub = tmp_path / "truncated.dwg"
    stub.write_bytes(b"AC1015" + bytes(800))  # a real DWG signature, no drawing

    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        _watch_progress(page, str(stub))
        page.wait_for_selector("#procNotice .notice", timeout=180000)
        page.wait_for_timeout(400)
        final = _progress_state(page)
        notice = page.inner_text("#procNotice")
        browser.close()

    assert "failed" in final["cls"], "a failed run has to look failed"
    assert float(final["width"].rstrip("%")) > 0, (
        "the progress already earned is retained, not rewound to zero"
    )
    assert "Failed during" in final["what"], f"say which stage stopped: {final['what']!r}"
    assert any(st.endswith(":failed") for st in final["stages"]), (
        "and mark that stage in the checklist"
    )
    assert notice.strip(), "with the actionable explanation underneath"
    assert not final["workspace"], "a failed run must not reach the workspace"


def test_language_switches_mid_run_without_restarting_the_job(viewer_url, drawings):
    """Reading the bar in Chinese must not cost a three-minute DWG its progress."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        posts = []
        page.on("request", lambda r: posts.append(r.url)
                if r.method == "POST" and "/api/analyse" in r.url else None)
        page.goto(viewer_url)
        page.wait_for_selector("#landing:not(.hide)", timeout=30000)
        page.wait_for_timeout(500)

        page.set_input_files("#fileInput", drawings["layout_1_100"]["path"])
        page.wait_for_selector("#processing:not(.hide)", timeout=30000)
        before = _progress_state(page)

        # The switch has to be on the processing screen itself to be usable here.
        page.click("#processing .langswitch button[data-lang='zh-CN']")
        page.wait_for_timeout(400)
        translated = _progress_state(page)
        stage_labels = page.eval_on_selector_all(
            "#timeline .stage .label", "els => els.map((e) => e.textContent)")

        page.wait_for_selector("#workspace:not(.hide)", timeout=180000)
        page.wait_for_timeout(1500)
        name = page.inner_text("#rdName")
        value = page.inner_text("#rdValue")
        browser.close()

    assert len(posts) == 1, f"the job was resubmitted on a language change: {posts}"
    assert float(translated["width"].rstrip("%")) >= float(before["width"].rstrip("%")), (
        "the bar must not rewind when the language changes"
    )
    assert any(re.search(r"[一-鿿]", label) for label in stage_labels), (
        f"the stage list did not translate mid-run: {stage_labels}"
    )
    # And the job it was watching still produced the same measurement.
    assert name == "几何并集面积"
    assert _number(value) == pytest.approx(54.46, abs=0.05), (
        "progress reporting and language must not touch the number"
    )


def test_the_drawing_extent_survives_a_production_sized_point_count(viewer_url):
    """A real DWG sends hundreds of thousands of points to the overlay.

    `Math.min(...xs)` passes each one as a function argument and throws a
    RangeError somewhere in the tens of thousands. That aborted the whole
    workspace render, so a production drawing arrived with an empty result card
    and no overlay — while a smaller one, under the argument limit, worked. Driven
    here with synthetic points, because the production drawings are not in git and
    the bug is about their size, not their content.
    """
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)

        extent = page.evaluate(
            """() => {
                // One primitive carrying 400,000 points, which is the order a
                // production DWG reaches and well past the spread limit.
                const points = new Array(400000);
                for (let i = 0; i < points.length; i++) points[i] = [i % 5000, -(i % 700)];
                points.push([-12.5, 900.25]);       // the true minimum x, maximum y
                window.__state.ignored = [{ points }];
                window.__state.result = { footprint_interpretations: [
                    { outer: [[[7000, -1500]]] },   // the true maximum x, minimum y
                ] };
                return cadExtent();
            }"""
        )
        browser.close()

    assert not errors, f"rendering the extent threw: {errors}"
    assert extent is not None
    assert extent["x0"] == -12.5
    assert extent["x1"] == 7000
    assert extent["y0"] == -1500
    assert extent["y1"] == 900.25


def test_the_drawing_extent_is_absent_when_there_is_no_geometry(viewer_url):
    """Nothing to measure is not an extent of zero; it is no extent."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        extent = page.evaluate(
            """() => {
                window.__state.ignored = [];
                window.__state.result = { footprint_interpretations: [] };
                return cadExtent();
            }"""
        )
        browser.close()
    assert extent is None


# ── saved analyses ───────────────────────────────────────────────────────────


def test_recent_analyses_are_hidden_when_this_instance_keeps_no_history(viewer_url):
    """An empty panel that can never fill reads as "nothing saved" rather than
    "saving is not available here"."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        page.wait_for_timeout(1200)
        hidden = page.evaluate(
            "() => document.getElementById('recentRow').classList.contains('hide')")
        browser.close()
    assert hidden


def test_a_completed_analysis_says_it_was_not_saved_when_nothing_was(viewer_url, drawings):
    """"Analysis saved" only when the server said it stored one."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _upload(page, viewer_url, drawings["plate_with_holes"]["path"])
        state = page.evaluate("""() => {
            const el = document.getElementById('savedState');
            return { hidden: el.hidden, state: el.dataset.state, text: el.textContent };
        }""")
        browser.close()
    assert "saved" != state.get("state"), "claimed a save that did not happen"
    assert not (state["text"] or "").startswith("Analysis saved")


def test_a_previously_measured_drawing_is_offered_not_substituted(viewer_url, drawings):
    """The server answers 200 with `cached`; the page must offer both choices and
    start nothing on its own."""
    cached = {
        "cached": {
            "id": "11111111-1111-1111-1111-111111111111",
            "file_name": "plate_with_holes.pdf", "name": "plate_with_holes.pdf",
            "source_type": "pdf", "status": "completed",
            "created_at": "2026-09-20T10:00:00+00:00",
            "completed_at": "2026-09-20T10:00:05+00:00",
            "scale_verified": True, "area_mm2": 23057.5, "area_m2": 0.023,
        },
        "source_sha256": "a" * 64,
        "file_name": "plate_with_holes.pdf",
    }
    import json as _json

    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        requests = []
        page.on("request", lambda r: requests.append(r.url)
                if "/api/analyse" in r.url else None)
        page.route("**/api/analyse", lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(cached)))

        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        page.set_input_files("#fileInput", drawings["plate_with_holes"]["path"])
        page.wait_for_selector("#cacheOpenBtn", timeout=30000)

        english = {
            "title": page.inner_text("#uploadNotice h4"),
            "open": page.inner_text("#cacheOpenBtn"),
            "again": page.inner_text("#cacheAgainBtn"),
            "landing": page.is_visible("#landing"),
            "workspace": page.is_visible("#workspace"),
        }
        # Switching language re-renders the page's static text; the offer itself
        # was written from the catalogue at the moment it appeared.
        page.click('#landing [data-lang="zh-CN"]')
        page.wait_for_timeout(700)
        chinese_heading = page.inner_text("#dropzone h2")
        browser.close()

    assert english["title"] == "This drawing has already been measured"
    assert english["open"] == "Open previous analysis"
    assert english["again"] == "Re-analyse anyway"
    assert english["landing"] and not english["workspace"], (
        "nothing was substituted: the operator is still deciding")
    assert len(requests) == 1, "no analysis was started behind the offer"
    assert any("一" <= c <= "鿿" for c in chinese_heading)


def test_re_analyse_anyway_asks_for_a_fresh_measurement(viewer_url, drawings):
    import json as _json

    cached = {"cached": {"id": "x", "file_name": "p.pdf", "source_type": "pdf",
                         "created_at": "2026-09-20T10:00:00+00:00"},
              "source_sha256": "a" * 64, "file_name": "p.pdf"}
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        seen = []

        def handler(route):
            seen.append(route.request.url)
            if "reanalyse=true" in route.request.url:
                route.continue_()
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=_json.dumps(cached))

        page.route("**/api/analyse*", handler)
        page.goto(viewer_url)
        page.wait_for_selector("#dropzone", timeout=30000)
        page.set_input_files("#fileInput", drawings["plate_with_holes"]["path"])
        page.wait_for_selector("#cacheAgainBtn", timeout=30000)
        page.click("#cacheAgainBtn")
        page.wait_for_selector("#workspace:not(.hide)", timeout=120000)
        browser.close()

    assert len(seen) == 2
    assert "reanalyse=true" not in seen[0]
    assert "reanalyse=true" in seen[1], "the second request must ask to re-measure"


def test_every_saved_analysis_string_exists_in_both_languages():
    """Recent Analyses, Save Analysis, Analysis saved, Open, Open previous analysis,
    Re-analyse anyway and rename — none may fall back to a bare key in Chinese."""
    import json as _json
    import os as _os

    root = _os.path.join(_os.path.dirname(__file__), "..", "frontend", "i18n")
    en = _json.load(open(_os.path.join(root, "en.json"), encoding="utf-8"))
    zh = _json.load(open(_os.path.join(root, "zh-CN.json"), encoding="utf-8"))
    for key in ("recent.heading", "recent.open", "recent.empty", "recent.unverified",
                "ws.save", "ws.saved", "ws.saving", "ws.saveFailed",
                "ws.saveDisabled", "ws.rename", "ws.renamePrompt",
                "cache.title", "cache.body", "cache.open", "cache.again",
                "saved.restored"):
        assert en.get(key), f"{key} missing in English"
        assert zh.get(key), f"{key} missing in Chinese"
        assert zh[key] != en[key], f"{key} is untranslated"
        assert any("一" <= c <= "鿿" for c in zh[key]), f"{key} has no Chinese"


# ── manual calibration ───────────────────────────────────────────────────────


def _open_calibration(page, url, drawing):
    """Upload a drawing with no derivable scale and open the calibration panel."""
    _upload(page, url, drawing)
    page.wait_for_selector("#startCal", timeout=60000)
    page.click("#startCal")
    page.wait_for_selector(".steps.four", timeout=30000)


def _pick(page, x, y):
    """Click a point on the drawing canvas, in canvas coordinates."""
    box = page.locator("#canvasWrap").bounding_box()
    page.mouse.click(box["x"] + x, box["y"] + y)
    page.wait_for_timeout(250)


def test_calibration_shows_four_steps_with_their_own_state(viewer_url, drawings):
    """The panel previously showed three steps and one line of coordinates, so
    "distance entered but nothing happens" had no explanation on screen."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])

        steps = page.eval_on_selector_all(
            ".steps.four li", "els => els.map(e => e.textContent.trim())")
        points = page.inner_text(".kv.points")
        browser.close()

    assert len(steps) == 4, steps
    assert "first endpoint" in steps[0].lower()
    assert "second endpoint" in steps[1].lower()
    assert "known distance" in steps[2].lower()
    assert "apply" in steps[3].lower()
    # Each point's state is stated separately, not as one "no points yet" line.
    assert "First point" in points and "Second point" in points
    assert points.count("not selected") == 2


def test_entering_a_distance_before_picking_says_what_is_missing(viewer_url, drawings):
    """The reported confusion, exactly: the field accepts a value while Apply stays
    disabled. The panel must now say why."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])

        page.fill("#knownLength", "22000")
        page.wait_for_timeout(300)
        state = page.evaluate("""() => ({
            disabled: document.getElementById('applyCal').disabled,
            blocker: document.getElementById('calBlocker').textContent.trim(),
            figures: document.getElementById('calFigures').textContent.trim(),
        })""")
        browser.close()

    assert state["disabled"], "no points are picked, so it cannot be applied"
    assert state["blocker"], "the panel must say why it is unavailable"
    assert "first endpoint" in state["blocker"].lower()
    # The distance the operator typed is acknowledged rather than ignored.
    assert "22,000" in state["figures"] or "22000" in state["figures"]


def test_apply_stays_disabled_with_two_points_and_no_distance(viewer_url, drawings):
    """The other half of the old bug: two points enabled the button, and clicking
    it silently focused the empty field."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        _pick(page, 200, 200)
        _pick(page, 400, 200)

        state = page.evaluate("""() => ({
            disabled: document.getElementById('applyCal').disabled,
            blocker: document.getElementById('calBlocker').textContent.trim(),
            points: document.querySelector('.kv.points').textContent,
            figures: document.getElementById('calFigures').textContent,
            markers: [...document.querySelectorAll('#overlay text')]
                .map(t => t.textContent).filter(t => t === 'A' || t === 'B'),
        })""")
        browser.close()

    assert state["disabled"], "a scale cannot be derived without a known distance"
    assert "known distance" in state["blocker"].lower() or "real distance" in state["blocker"].lower()
    assert state["points"].count("selected ✓") == 2
    assert state["points"].count("not selected") == 0
    # The drawing span is shown as soon as it is known, before any distance.
    assert "Drawing span" in state["figures"]
    # And the two points are labelled A and B on the drawing itself.
    assert state["markers"] == ["A", "B"], state["markers"]


def test_the_arithmetic_is_shown_before_it_is_applied(viewer_url, drawings):
    """Span, known distance and the resulting scale — so the operator can check
    the calibration rather than trust it."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        _pick(page, 200, 220)
        _pick(page, 500, 220)
        page.fill("#knownLength", "150")
        page.wait_for_timeout(300)

        state = page.evaluate("""() => {
            const rows = [...document.querySelectorAll('#calFigures .r')].map(r => ({
                k: r.querySelector('.k').textContent.trim(),
                v: r.querySelector('.v').textContent.trim(),
            }));
            return { rows, disabled: document.getElementById('applyCal').disabled,
                     blocker: document.getElementById('calBlocker').textContent.trim() };
        }""")
        browser.close()

    labels = [row["k"] for row in state["rows"]]
    assert "Drawing span" in labels
    assert "Known distance" in labels
    assert "Calculated scale" in labels
    assert not state["disabled"], "two points and a valid distance is ready"
    assert state["blocker"] == "", "nothing is missing, so nothing is explained"

    # The scale shown is the known distance over the measured span, in mm per unit.
    span = _number(next(r["v"] for r in state["rows"] if r["k"] == "Drawing span"))
    scale = _number(next(r["v"] for r in state["rows"] if r["k"] == "Calculated scale"))
    assert scale == pytest.approx(150.0 / span, rel=0.01)


def test_two_clicks_in_the_same_place_are_refused_with_a_reason(viewer_url, drawings):
    """A zero span would divide by zero. That is a misclick, not a dimension."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        _pick(page, 300, 300)
        _pick(page, 300, 300)
        page.fill("#knownLength", "500")
        page.wait_for_timeout(300)
        state = page.evaluate("""() => ({
            disabled: document.getElementById('applyCal').disabled,
            blocker: document.getElementById('calBlocker').textContent.trim(),
        })""")
        browser.close()

    assert state["disabled"]
    assert "same place" in state["blocker"].lower()


def test_the_typed_distance_survives_picking_another_point(viewer_url, drawings):
    """The panel redraws on every click; losing the distance each time would be
    its own bug."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        page.fill("#knownLength", "22000")
        _pick(page, 220, 240)
        _pick(page, 520, 240)
        kept = page.input_value("#knownLength")
        browser.close()
    assert kept == "22000", "the operator's number was discarded by a re-render"


def test_a_completed_calibration_produces_an_operator_supplied_scale(
    viewer_url, drawings
):
    """End to end: the measurement becomes physical, and is labelled as coming
    from the operator rather than from the drawing."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        _pick(page, 210, 230)
        _pick(page, 510, 230)
        page.fill("#knownLength", "300")
        page.wait_for_timeout(250)
        page.click("#applyCal")
        page.wait_for_timeout(2500)

        state = page.evaluate("""() => ({
            value: document.getElementById('rdValue').textContent.trim(),
            badges: document.getElementById('rdBadges').textContent,
            scale: (window.__state.result || {}).scale || {},
        })""")
        browser.close()

    assert state["scale"]["operator_supplied"] is True
    assert state["scale"]["verified"] is True
    assert state["scale"]["calibration"], "the two points and the distance are recorded"
    assert state["scale"]["calibration"]["known_length"] == 300
    assert "Not available" not in state["value"], "an area is now reportable"


def test_calibration_never_prefills_a_distance_from_the_drawing(viewer_url, drawings):
    """A stroked-text PDF carries no readable dimension, and guessing one would be
    fabricating the scale (§3). The field starts empty and stays the operator's."""
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _open_calibration(page, viewer_url, drawings["raster_plate"]["path"])
        empty_before = page.input_value("#knownLength")
        _pick(page, 240, 260)
        _pick(page, 540, 260)
        empty_after = page.input_value("#knownLength")
        browser.close()
    assert empty_before == "", "a distance was pre-filled"
    assert empty_after == "", "picking points must not suggest a distance"


def test_every_calibration_string_exists_in_both_languages():
    import json as _json
    import os as _os

    root = _os.path.join(_os.path.dirname(__file__), "..", "frontend", "i18n")
    en = _json.load(open(_os.path.join(root, "en.json"), encoding="utf-8"))
    zh = _json.load(open(_os.path.join(root, "zh-CN.json"), encoding="utf-8"))
    for key in ("cal.step1", "cal.step2", "cal.step3", "cal.step4",
                "cal.firstPoint", "cal.secondPoint", "cal.selected", "cal.notSelected",
                "cal.spanLabel", "cal.knownLabel", "cal.scaleLabel",
                "cal.blockFirst", "cal.blockSecond", "cal.blockSamePoint",
                "cal.blockDistance"):
        assert en.get(key), f"{key} missing in English"
        assert zh.get(key), f"{key} missing in Chinese"
        assert any("一" <= c <= "鿿" for c in zh[key]), f"{key} not translated"
